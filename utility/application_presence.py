from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import discord


LOG = logging.getLogger("bot.application_presence")

VARIABLE_RE = re.compile(r"\{n:([a-z0-9_.:-]+)\}", flags=re.IGNORECASE)

DEFAULT_STATUS_TEMPLATES = (
    "「🌐」{n:sv} servers",
    "「👥 」{n:m} usuários",
)


class ApplicationPresenceService:
    """Mantém o custom status do bot com variáveis leves.

    Não é cog e não expõe comandos. O serviço usa apenas dados já conhecidos pelo
    bot, como `bot.guilds` e `guild.member_count`, para não buscar membros nem
    criar carga extra em servidores grandes.
    """

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._index = 0
        self._last_text = ""
        self._last_update_at = 0.0
        self._last_event_schedule_at = 0.0

    @property
    def enabled(self) -> bool:
        raw = str(os.getenv("APPLICATION_PRESENCE_ENABLED", "true") or "true").strip().lower()
        return raw not in {"0", "false", "no", "n", "off", "nao", "não"}

    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="application-presence-service")
        self.schedule_refresh(immediate=True)

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
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
        if not self.enabled:
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
            from datetime import datetime, timezone

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
