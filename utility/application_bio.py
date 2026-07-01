from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord


LOG = logging.getLogger("bot.application_bio")

VARIABLE_RE = re.compile(r"\{n:([a-z0-9_.:-]+)\}", flags=re.IGNORECASE)
SUPPORTED_VARIABLES = {
    "sv",
    "guilds",
    "m",
    "members",
    "m-b",
    "humans",
    "b",
    "bots",
    "u",
    "users",
    "cmd",
    "commands",
    "workers",
    "music",
    "tts",
    "up",
    "uptime",
    "ping",
    "ver",
    "version",
}


class ApplicationBioService:
    """Atualiza a descrição pública da aplicação usando variáveis leves.

    O serviço não é cog e não expõe comandos. Ele importa o template quando a
    descrição atual da aplicação contém variáveis como `{n:sv}` e depois mantém o
    template salvo localmente, porque a descrição pública renderizada perde os
    marcadores.
    """

    API_BASE = "https://discord.com/api/v10"
    DESCRIPTION_LIMIT = 400

    def __init__(self, bot: discord.Client, state_path: Path) -> None:
        self.bot = bot
        self.state_path = Path(state_path)
        self._task: asyncio.Task | None = None
        self._sync_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._next_reason = "boot"
        self._last_event_schedule_at = 0.0

    @property
    def enabled(self) -> bool:
        raw = str(os.getenv("APPLICATION_BIO_ENABLED", "true") or "true").strip().lower()
        return raw not in {"0", "false", "no", "n", "off", "nao", "não"}

    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="application-bio-service")
        self.schedule_sync("boot", immediate=True)

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
            LOG.debug("falha ao encerrar serviço de bio", exc_info=True)

    def schedule_sync(self, reason: str = "event", *, immediate: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not immediate:
            debounce = self._env_float("APPLICATION_BIO_EVENT_DEBOUNCE_SECONDS", 180.0, minimum=30.0, maximum=1800.0)
            if now - self._last_event_schedule_at < debounce:
                return
            self._last_event_schedule_at = now
        self._next_reason = str(reason or "event")[:48]
        self._wake_event.set()

    async def _run_loop(self) -> None:
        startup_delay = self._env_float("APPLICATION_BIO_STARTUP_DELAY_SECONDS", 25.0, minimum=0.0, maximum=900.0)
        sync_interval = self._env_float("APPLICATION_BIO_SYNC_INTERVAL_SECONDS", 3600.0, minimum=900.0, maximum=86400.0)
        event_delay = self._env_float("APPLICATION_BIO_EVENT_DELAY_SECONDS", 180.0, minimum=10.0, maximum=1800.0)

        try:
            if startup_delay > 0:
                await asyncio.sleep(startup_delay)
            await self.sync(reason="boot")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug("sync inicial da bio falhou", exc_info=True)

        while not self.bot.is_closed():
            try:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=sync_interval)
                except asyncio.TimeoutError:
                    reason = "interval"
                else:
                    reason = self._next_reason or "event"
                    await asyncio.sleep(event_delay)

                await self.sync(reason=reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.debug("loop da bio falhou", exc_info=True)
                await asyncio.sleep(300)

    async def sync(self, *, reason: str = "manual") -> bool:
        if not self.enabled:
            return False
        if self._sync_lock.locked():
            return False
        async with self._sync_lock:
            state = self._load_state()
            now = datetime.now(timezone.utc).isoformat()
            min_update_interval = self._env_float("APPLICATION_BIO_MIN_UPDATE_INTERVAL_SECONDS", 1800.0, minimum=300.0, maximum=86400.0)
            portal_check_interval = self._env_float("APPLICATION_BIO_PORTAL_CHECK_INTERVAL_SECONDS", 21600.0, minimum=1800.0, maximum=86400.0)

            current_description: str | None = None
            should_check_portal = self._should_check_portal(state, reason=reason, interval_seconds=portal_check_interval)
            if should_check_portal:
                try:
                    current_description = await self._discord_request("GET", "/applications/@me")
                    if isinstance(current_description, dict):
                        current_description = str(current_description.get("description") or "")
                    else:
                        current_description = ""
                except Exception as exc:
                    state["last_error"] = f"GET {type(exc).__name__}: {str(exc)[:200]}"
                    state["last_checked_at"] = now
                    self._save_state(state)
                    LOG.debug("não consegui ler descrição atual da aplicação: %s", exc)
                    return False

                state["last_checked_at"] = now
                if self._contains_variable(current_description):
                    state["template"] = current_description
                    state["auto_enabled"] = True
                    state["last_imported_at"] = now
                elif state.get("last_rendered") and current_description != state.get("last_rendered"):
                    # Edição manual sem variáveis. Não sobrescreve. O serviço volta
                    # a ativar sozinho quando o Developer Portal receber variáveis de novo.
                    state["auto_enabled"] = False
                    state["last_manual_description"] = current_description
                    state["last_error"] = "bio manual sem variáveis; auto-update pausado"
                    self._save_state(state)
                    return False

            template = str(state.get("template") or "")
            if not template or not self._contains_variable(template):
                state.setdefault("auto_enabled", False)
                state["last_error"] = "template ausente"
                self._save_state(state)
                return False
            if state.get("auto_enabled") is False:
                self._save_state(state)
                return False

            tokens = self._extract_tokens(template)
            values = await self._collect_values(tokens)
            rendered = self._render_template(template, values)
            if len(rendered) > self.DESCRIPTION_LIMIT:
                state["last_error"] = f"bio renderizada passou de {self.DESCRIPTION_LIMIT} caracteres"
                state["last_render_attempt_at"] = now
                state["last_render_length"] = len(rendered)
                self._save_state(state)
                LOG.warning("bio renderizada ignorada: %s caracteres", len(rendered))
                return False

            if rendered == state.get("last_rendered") and not should_check_portal:
                state["last_render_attempt_at"] = now
                state["last_error"] = ""
                self._save_state(state)
                return False

            if rendered == current_description:
                state["last_rendered"] = rendered
                state["last_render_attempt_at"] = now
                state["last_error"] = ""
                self._save_state(state)
                return False

            if not self._may_update_now(state, reason=reason, interval_seconds=min_update_interval):
                state["last_render_attempt_at"] = now
                state["last_error"] = "adiado por intervalo mínimo"
                self._save_state(state)
                return False

            try:
                await self._discord_request("PATCH", "/applications/@me", json_payload={"description": rendered})
            except Exception as exc:
                state["last_error"] = f"PATCH {type(exc).__name__}: {str(exc)[:200]}"
                state["last_render_attempt_at"] = now
                self._save_state(state)
                LOG.debug("não consegui atualizar bio da aplicação: %s", exc)
                return False

            state.update({
                "auto_enabled": True,
                "last_rendered": rendered,
                "last_render_attempt_at": now,
                "last_updated_at": now,
                "last_update_reason": str(reason or "")[:48],
                "last_render_length": len(rendered),
                "last_values": values,
                "last_error": "",
            })
            self._save_state(state)
            LOG.info("bio da aplicação atualizada (%s, %s caracteres)", reason, len(rendered))
            return True

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception:
            LOG.debug("estado de bio inválido; usando vazio", exc_info=True)
        return {"version": 1, "auto_enabled": True}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            state["version"] = 1
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.state_path)
            try:
                os.chmod(self.state_path, 0o600)
            except Exception:
                pass
        except Exception:
            LOG.debug("falha ao salvar estado de bio", exc_info=True)

    def _contains_variable(self, text: str) -> bool:
        return bool(VARIABLE_RE.search(str(text or "")))

    def _extract_tokens(self, template: str) -> set[str]:
        return {str(match.group(1) or "").lower() for match in VARIABLE_RE.finditer(str(template or ""))}

    def _render_template(self, template: str, values: dict[str, str]) -> str:
        def repl(match: re.Match[str]) -> str:
            token = str(match.group(1) or "").lower()
            return values.get(token, match.group(0))

        return VARIABLE_RE.sub(repl, str(template or "")).strip()

    async def _collect_values(self, tokens: set[str]) -> dict[str, str]:
        tokens = {str(token or "").lower() for token in tokens}
        stats = self._collect_guild_stats(tokens)
        if tokens & {"workers"}:
            stats["workers"] = await self._collect_workers_online()
        if tokens & {"music"}:
            stats["music"] = self._collect_music_active()
        if tokens & {"tts"}:
            stats["tts"] = self._collect_tts_total()
        if tokens & {"cmd", "commands"}:
            stats["cmd"] = stats["commands"] = self._collect_command_count()
        if tokens & {"up", "uptime"}:
            uptime = self._format_uptime()
            stats["up"] = stats["uptime"] = uptime
        if tokens & {"ping"}:
            stats["ping"] = self._format_ping()
        if tokens & {"ver", "version"}:
            version = self._collect_version()
            stats["ver"] = stats["version"] = version
        return {key: str(value) for key, value in stats.items()}

    def _collect_guild_stats(self, tokens: set[str]) -> dict[str, str]:
        guilds = list(getattr(self.bot, "guilds", []) or [])
        guild_count = len(guilds)
        total_members = 0
        known_bots = 0
        unique_users: set[int] | None = set() if tokens & {"u", "users"} else None
        member_scan_limit = self._env_int("APPLICATION_BIO_MEMBER_SCAN_LIMIT", 250_000, minimum=0, maximum=2_000_000)
        unique_scan_limit = self._env_int("APPLICATION_BIO_UNIQUE_SCAN_LIMIT", 250_000, minimum=0, maximum=2_000_000)
        scanned_for_bots = 0
        scanned_for_unique = 0

        need_bots = bool(tokens & {"b", "bots", "m-b", "humans"})
        for guild in guilds:
            member_count = getattr(guild, "member_count", None)
            if isinstance(member_count, int) and member_count > 0:
                total_members += member_count
            else:
                total_members += len(getattr(guild, "members", []) or [])

            members = getattr(guild, "members", []) or []
            if need_bots and scanned_for_bots < member_scan_limit:
                for member in members:
                    if scanned_for_bots >= member_scan_limit:
                        break
                    scanned_for_bots += 1
                    if getattr(member, "bot", False):
                        known_bots += 1
            if unique_users is not None and scanned_for_unique < unique_scan_limit:
                for member in members:
                    if scanned_for_unique >= unique_scan_limit:
                        break
                    scanned_for_unique += 1
                    if not getattr(member, "bot", False):
                        try:
                            unique_users.add(int(getattr(member, "id", 0) or 0))
                        except Exception:
                            continue

        estimated_humans = max(0, total_members - known_bots)
        values = {
            "sv": self._format_int(guild_count),
            "guilds": self._format_int(guild_count),
            "m": self._format_int(total_members),
            "members": self._format_int(total_members),
            "b": self._format_int(known_bots),
            "bots": self._format_int(known_bots),
            "m-b": self._format_int(estimated_humans),
            "humans": self._format_int(estimated_humans),
        }
        if unique_users is not None:
            values["u"] = values["users"] = self._format_int(len(unique_users))
        return values

    async def _collect_workers_online(self) -> str:
        def read_online() -> int:
            try:
                from utility.commands.workers_registry import get_core_workers_registry

                snapshot = get_core_workers_registry().snapshot(lock_timeout_seconds=0.03)
                summary = snapshot.get("summary") if isinstance(snapshot, dict) else {}
                return int((summary or {}).get("online") or 0)
            except Exception:
                LOG.debug("falha ao ler contador de workers", exc_info=True)
                return 0

        return self._format_int(await asyncio.to_thread(read_online))

    def _collect_music_active(self) -> str:
        try:
            router = getattr(self.bot, "audio_router", None)
            counter = getattr(router, "_active_player_count", None)
            if callable(counter):
                return self._format_int(int(counter()))
        except Exception:
            LOG.debug("falha ao ler players ativos", exc_info=True)
        return "0"

    def _collect_tts_total(self) -> str:
        try:
            cog = getattr(self.bot, "get_cog", lambda *_: None)("TTSVoice")
            snapshot_fn = getattr(cog, "get_tts_metrics_snapshot", None)
            if callable(snapshot_fn):
                snapshot = snapshot_fn()
                return self._format_int(int((snapshot or {}).get("queue_enqueued") or 0))
        except Exception:
            LOG.debug("falha ao ler métricas TTS", exc_info=True)
        return "0"

    def _collect_command_count(self) -> str:
        total = 0
        try:
            total += len(list(getattr(self.bot, "commands", []) or []))
        except Exception:
            pass
        try:
            tree = getattr(self.bot, "tree", None)
            walk = getattr(tree, "walk_commands", None)
            if callable(walk):
                total += len(list(walk()))
        except Exception:
            pass
        return self._format_int(total)

    def _format_uptime(self) -> str:
        try:
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

    def _format_ping(self) -> str:
        try:
            return f"{round(float(getattr(self.bot, 'latency', 0.0) or 0.0) * 1000)}ms"
        except Exception:
            return "0ms"

    def _collect_version(self) -> str:
        for name in ("BOT_VERSION", "APP_VERSION", "GIT_SHA", "SOURCE_VERSION", "RENDER_GIT_COMMIT"):
            value = str(os.getenv(name) or "").strip()
            if value:
                return value[:12]
        return "local"

    def _should_check_portal(self, state: dict[str, Any], *, reason: str, interval_seconds: float) -> bool:
        if str(reason or "").lower() in {"boot", "startup"}:
            return True
        last = self._parse_iso_ts(state.get("last_checked_at"))
        return last <= 0 or (time.time() - last) >= interval_seconds

    def _may_update_now(self, state: dict[str, Any], *, reason: str, interval_seconds: float) -> bool:
        if str(reason or "").lower() in {"boot", "startup"}:
            return True
        last = self._parse_iso_ts(state.get("last_updated_at"))
        return last <= 0 or (time.time() - last) >= interval_seconds

    def _parse_iso_ts(self, value: object) -> float:
        try:
            text = str(value or "").strip()
            if not text:
                return 0.0
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    async def _discord_request(self, method: str, path: str, *, json_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = str(os.getenv("DISCORD_TOKEN") or getattr(getattr(self.bot, "http", None), "token", "") or "").strip()
        if not token:
            raise RuntimeError("token ausente")
        url = f"{self.API_BASE}{path}"
        timeout = self._env_float("APPLICATION_BIO_HTTP_TIMEOUT_SECONDS", 8.0, minimum=2.0, maximum=30.0)

        async def sleep_retry_after(payload: dict[str, Any]) -> bool:
            try:
                retry_after = float(payload.get("retry_after") or 0.0)
            except Exception:
                retry_after = 0.0
            if retry_after <= 0 or retry_after > 20:
                return False
            await asyncio.sleep(retry_after + 0.25)
            return True

        for attempt in range(2):
            status, payload, body = await asyncio.to_thread(
                self._discord_request_sync,
                method,
                url,
                token,
                json_payload,
                timeout,
            )
            if 200 <= status < 300 and isinstance(payload, dict):
                return payload
            if status == 429 and isinstance(payload, dict) and attempt == 0:
                if await sleep_retry_after(payload):
                    continue
            error_text = ""
            if isinstance(payload, dict):
                error_text = str(payload.get("message") or payload)[:300]
            else:
                error_text = body[:300]
            raise RuntimeError(f"HTTP {status}: {error_text}")
        raise RuntimeError("falha HTTP inesperada")

    def _discord_request_sync(
        self,
        method: str,
        url: str,
        token: str,
        json_payload: dict[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any] | None, str]:
        data = None
        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "SataAndagiBot application-bio-service",
            "Accept": "application/json",
        }
        if json_payload is not None:
            data = json.dumps(json_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=str(method or "GET").upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw.strip() else {}
                return int(response.status), payload if isinstance(payload, dict) else None, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except Exception:
                payload = None
            return int(exc.code), payload if isinstance(payload, dict) else None, raw

    def _format_int(self, value: int) -> str:
        try:
            return f"{int(value):,}".replace(",", ".")
        except Exception:
            return "0"

    def _env_float(self, name: str, default: float, *, minimum: float, maximum: float) -> float:
        try:
            value = float(str(os.getenv(name, default)).strip().replace(",", "."))
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _env_int(self, name: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(str(os.getenv(name, default)).strip())
        except Exception:
            value = default
        return max(minimum, min(maximum, value))
