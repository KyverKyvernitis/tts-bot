from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord


LOG = logging.getLogger("bot.application_presence")

VARIABLE_RE = re.compile(r"\{n:([a-z0-9_.:-]+)\}", flags=re.IGNORECASE)

DEFAULT_STATUS_TEMPLATES = (
    "「👥 」_𝗵𝗲𝗹𝗽 • {n:m} usuários",
    "「🌐」_𝗵𝗲𝗹𝗽 • {n:sv} servidores",
)


class ApplicationPresenceService:
    """Mantém o custom status do bot com variáveis leves.

    Não é cog e não expõe comandos. O serviço usa apenas dados já conhecidos pelo
    bot, como `bot.guilds` e `guild.member_count`, para não buscar membros nem
    criar carga extra em servidores grandes.
    """

    def __init__(self, bot: discord.Client, update_state_path: Path | None = None) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._index = 0
        self._last_text = ""
        self._last_update_at = 0.0
        self._last_event_schedule_at = 0.0
        self._maintenance_active = False
        self._update_state_path = update_state_path or Path(
            os.getenv(
                "DISCORD_AUTO_UPDATE_RUNTIME_STATE_FILE",
                "/home/ubuntu/bot-update-staging/candidates/runtime-state.json",
            )
        )

    @property
    def enabled(self) -> bool:
        raw = str(os.getenv("APPLICATION_PRESENCE_ENABLED", "true") or "true").strip().lower()
        return raw not in {"0", "false", "no", "n", "off", "nao", "não"}

    @property
    def maintenance_active(self) -> bool:
        return self._maintenance_active

    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="application-presence-service")
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(),
                name="application-presence-maintenance",
            )
        self.schedule_refresh(immediate=True)

    async def close(self) -> None:
        tasks = [task for task in (self._task, self._maintenance_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOG.debug("falha ao encerrar serviço de presença", exc_info=True)

    def schedule_refresh(self, *, immediate: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not immediate:
            debounce = self._env_float("APPLICATION_PRESENCE_EVENT_DEBOUNCE_SECONDS", 60.0, minimum=10.0, maximum=1800.0)
            if now - self._last_event_schedule_at < debounce:
                return
            self._last_event_schedule_at = now
        self._wake_event.set()

    async def _run_loop(self) -> None:
        startup_delay = self._env_float("APPLICATION_PRESENCE_STARTUP_DELAY_SECONDS", 5.0, minimum=0.0, maximum=300.0)
        interval = self._env_float("APPLICATION_PRESENCE_INTERVAL_SECONDS", 60.0, minimum=30.0, maximum=3600.0)
        event_delay = self._env_float("APPLICATION_PRESENCE_EVENT_DELAY_SECONDS", 10.0, minimum=0.0, maximum=600.0)

        try:
            if startup_delay > 0:
                await asyncio.sleep(startup_delay)
            await self.apply_next_status(reason="boot")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug("status inicial falhou", exc_info=True)

        while not self.bot.is_closed():
            try:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
                    if event_delay > 0:
                        await asyncio.sleep(event_delay)
                except asyncio.TimeoutError:
                    pass
                await self.apply_next_status(reason="interval")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.debug("loop de presença falhou", exc_info=True)
                await asyncio.sleep(min(300.0, interval))

    async def apply_next_status(self, *, reason: str = "interval") -> bool:
        if not self.enabled or self._maintenance_active:
            return False
        templates = self._status_templates()
        if not templates:
            return False

        template = templates[self._index % len(templates)]
        text = self._render_template(template).strip()
        if not text:
            return False

        now = time.monotonic()
        min_interval = self._env_float("APPLICATION_PRESENCE_MIN_UPDATE_INTERVAL_SECONDS", 30.0, minimum=15.0, maximum=3600.0)
        if self._last_update_at > 0 and now - self._last_update_at < min_interval:
            return False
        if text == self._last_text:
            return False

        activity = self._build_custom_activity(text)
        try:
            await self.bot.change_presence(status=discord.Status.online, activity=activity)
        except Exception:
            LOG.debug("falha ao alterar custom status", exc_info=True)
            return False

        self._index = (self._index + 1) % max(1, len(templates))
        self._last_text = text
        self._last_update_at = now
        LOG.info("custom status atualizado (%s): %s", reason, text)
        return True


    async def _maintenance_loop(self) -> None:
        interval = self._env_float(
            "APPLICATION_PRESENCE_UPDATE_POLL_SECONDS",
            2.0,
            minimum=1.0,
            maximum=30.0,
        )
        while not self.bot.is_closed():
            try:
                active = self._read_update_state()
                if active and not self._maintenance_active:
                    await self._set_maintenance_presence()
                elif not active and self._maintenance_active:
                    await self._restore_regular_presence()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.debug("falha ao acompanhar atualização na presença", exc_info=True)
            await asyncio.sleep(interval)

    def _read_update_state(self) -> bool:
        path = self._update_state_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return False
        if not isinstance(payload, dict) or not bool(payload.get("active")):
            return False

        stale_after = self._env_float(
            "APPLICATION_PRESENCE_UPDATE_STALE_SECONDS",
            180.0,
            minimum=30.0,
            maximum=1800.0,
        )
        heartbeat = payload.get("heartbeat_epoch")
        try:
            heartbeat_value = float(heartbeat)
        except (TypeError, ValueError):
            heartbeat_text = str(payload.get("heartbeat_at") or "").strip()
            try:
                heartbeat_value = datetime.fromisoformat(heartbeat_text.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                return False

        age = time.time() - heartbeat_value
        return -30.0 <= age <= stale_after

    async def _set_maintenance_presence(self) -> None:
        activity = self._build_custom_activity("Atualizando")
        try:
            await self.bot.change_presence(status=discord.Status.idle, activity=activity)
        except Exception:
            LOG.debug("falha ao ativar presença de atualização", exc_info=True)
            return
        self._maintenance_active = True
        LOG.info("presença de atualização ativada")

    async def _restore_regular_presence(self) -> None:
        self._maintenance_active = False
        self._last_text = ""
        self._last_update_at = 0.0
        await self.apply_next_status(reason="update_done")

    def _status_templates(self) -> tuple[str, ...]:
        raw = str(os.getenv("APPLICATION_PRESENCE_TEMPLATES", "") or "").strip()
        if raw:
            values = [item.strip() for item in re.split(r"\s*\|\|\s*", raw) if item.strip()]
            if values:
                return tuple(values)
        return DEFAULT_STATUS_TEMPLATES

    def _build_custom_activity(self, text: str) -> discord.BaseActivity | discord.Activity:
        custom_activity = getattr(discord, "CustomActivity", None)
        if custom_activity is not None:
            try:
                return custom_activity(name=text)
            except TypeError:
                try:
                    return custom_activity(text)
                except Exception:
                    pass
            except Exception:
                pass
        activity_type = getattr(discord.ActivityType, "custom", None)
        if activity_type is not None:
            try:
                return discord.Activity(type=activity_type, name=text, state=text)
            except TypeError:
                return discord.Activity(type=activity_type, name=text)
        return discord.Game(name=text)

    def _render_template(self, template: str) -> str:
        tokens = {str(match.group(1) or "").lower() for match in VARIABLE_RE.finditer(str(template or ""))}
        values = self._collect_values(tokens)

        def repl(match: re.Match[str]) -> str:
            token = str(match.group(1) or "").lower()
            return values.get(token, match.group(0))

        return VARIABLE_RE.sub(repl, str(template or ""))

    def _collect_values(self, tokens: set[str]) -> dict[str, str]:
        guilds = list(getattr(self.bot, "guilds", []) or [])
        guild_count = len(guilds)
        total_members = 0
        for guild in guilds:
            member_count = getattr(guild, "member_count", None)
            if isinstance(member_count, int) and member_count > 0:
                total_members += member_count
            else:
                total_members += len(getattr(guild, "members", []) or [])

        values = {
            "sv": self._format_int(guild_count),
            "guilds": self._format_int(guild_count),
            "m": self._format_int(total_members),
            "members": self._format_int(total_members),
        }
        if tokens & {"ping"}:
            values["ping"] = self._format_ping()
        if tokens & {"up", "uptime"}:
            values["up"] = values["uptime"] = self._format_uptime()
        return values

    def _format_ping(self) -> str:
        try:
            return f"{round(float(getattr(self.bot, 'latency', 0.0) or 0.0) * 1000)}ms"
        except Exception:
            return "0ms"

    def _format_uptime(self) -> str:
        try:
            from datetime import datetime

            started_at = getattr(self.bot, "started_at", None)
            if started_at is None:
                return "0m"
            seconds = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
        except Exception:
            return "0m"
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def _format_int(self, value: Any) -> str:
        try:
            return f"{int(value):,}".replace(",", ".")
        except Exception:
            return "0"

    def _env_float(self, name: str, default: float, *, minimum: float, maximum: float) -> float:
        try:
            value = float(os.getenv(name, str(default)) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))
