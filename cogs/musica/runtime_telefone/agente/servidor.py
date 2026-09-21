#!/usr/bin/env python3
"""Core Music Agent for the phone worker.

Same-bot music plane: the VPS remains the UI/status plane while
this process owns Discord voice/FFmpeg/yt-dlp on the phone worker.

The agent intentionally does not register Discord commands and does not handle
message events. It exposes a small localhost HTTP API that phone_worker.py can
proxy through its authenticated /task endpoint.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import importlib
import base64
import hashlib
import io
import queue
from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
import signal
import sys
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

def _enable_music_domain_path() -> None:
    """Locate the canonical cogs.musica package in repo or extracted worker release."""
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / "cogs" / "musica" / "runtime_telefone").is_dir():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            return


_enable_music_domain_path()

from cogs.musica.runtime_telefone.agente.ciclo_vida import (  # noqa: E402
    cancel_tasks,
    remove_owned_task,
    stop_player_instance,
)
from cogs.musica.runtime_telefone.agente.configuracao import (  # noqa: E402
    bootstrap_env,
    env_float,
    env_int,
    truthy,
)
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState  # noqa: E402
from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin  # noqa: E402
from cogs.musica.runtime_telefone.agente.reproducao import ReproducaoMixin  # noqa: E402
from cogs.musica.runtime_telefone.agente.tts import TTSMixin, _TimedTTSSource  # noqa: E402
from cogs.musica.runtime_telefone.agente.utilitarios import (  # noqa: E402
    DEFAULT_YTDLP_AUDIO_FORMAT,
    _float_or_none,
    _metadata_text,
    safe_id,
    short_text,
)

try:
    from aiohttp import web
except Exception as exc:  # pragma: no cover - startup dependency error
    raise SystemExit(f"aiohttp ausente no Music Agent: {exc}")

try:
    import discord
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"discord.py ausente no Music Agent: {exc}")

from cogs.musica.runtime_telefone.agente.mixer_pcm import AgentMixedAudioSource  # noqa: E402








AGENT_VERSION = "0.3.44"
STARTED_AT = time.time()


bootstrap_env()









class MusicAgent(TTSMixin, ReproducaoMixin, ResolucaoMixin):
    def __init__(self) -> None:
        self.host = os.getenv("MUSIC_AGENT_HOST", "127.0.0.1")
        self.port = env_int("MUSIC_AGENT_PORT", 8780)
        self.token = os.getenv("MUSIC_AGENT_TOKEN") or os.getenv("PHONE_WORKER_TOKEN") or ""
        self.discord_token = os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
        self.ytdlp_format = os.getenv("MUSIC_AGENT_YTDLP_FORMAT") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_FORMAT") or DEFAULT_YTDLP_AUDIO_FORMAT
        self.ytdlp_timeout = env_int("MUSIC_AGENT_YTDLP_TIMEOUT_SECONDS", 35)
        self.cookies_file = os.getenv("MUSIC_AGENT_YTDLP_COOKIES_FILE") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE") or str(Path.home() / "phone-worker" / "secrets" / "youtube-cookies.txt")
        self.js_runtimes = os.getenv("MUSIC_AGENT_YTDLP_JS_RUNTIMES") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES") or "node"
        self.default_search = os.getenv("MUSIC_AGENT_YTDLP_DEFAULT_SEARCH") or "ytsearch3"
        self.direct_audio_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_AUDIO_ENABLED"), True)
        self.direct_youtube_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_YOUTUBE_ENABLED"), True)
        self.ffmpeg_executable = os.getenv("MUSIC_AGENT_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
        self.ffmpeg_before_options = os.getenv(
            "MUSIC_AGENT_FFMPEG_BEFORE_OPTIONS",
            "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,408,429,5xx -reconnect_delay_max 5 -rw_timeout 15000000",
        )
        self.ffmpeg_options = os.getenv("MUSIC_AGENT_FFMPEG_OPTIONS", "-vn -sn -dn -loglevel warning")
        # Resampling de qualidade só entra quando a fonte conhecida não é 48 kHz.
        # Mantemos o filter_size padrão do SWR para não aumentar CPU; a melhoria
        # vem de evitar interpolação linear entre fases e usar a razão exata.
        self.resample_quality_enabled = truthy(os.getenv("MUSIC_AGENT_RESAMPLE_QUALITY_ENABLED"), True)
        self.resample_filter_size = max(16, min(64, env_int("MUSIC_AGENT_RESAMPLE_FILTER_SIZE", 32)))
        self.resample_phase_shift = max(8, min(12, env_int("MUSIC_AGENT_RESAMPLE_PHASE_SHIFT", 10)))
        # Telemetria de qualidade mede apenas o tempo de leitura do source.
        # Nenhum sample PCM é analisado, então o custo no hot path é mínimo.
        self.audio_telemetry_enabled = truthy(os.getenv("MUSIC_AGENT_AUDIO_TELEMETRY_ENABLED"), True)
        self.audio_stall_threshold_ms = max(20.0, min(2000.0, env_float("MUSIC_AGENT_AUDIO_STALL_THRESHOLD_MS", 80.0)))
        self.ffmpeg_bitrate = max(16, min(512, env_int("MUSIC_AGENT_FFMPEG_OPUS_BITRATE_KBPS", 128)))
        # O encoder Opus interno do discord.py só é usado para sources PCM.
        # Ajuste o bitrate à capacidade real do canal e à qualidade da fonte,
        # evitando tanto o teto fixo de 128 kbps quanto desperdício em 384 kbps
        # para uma fonte ~128-160 kbps.
        self.discord_opus_default_bitrate = max(16, min(512, env_int("MUSIC_AGENT_DISCORD_OPUS_DEFAULT_BITRATE_KBPS", 160)))
        self.discord_opus_min_bitrate = max(16, min(512, env_int("MUSIC_AGENT_DISCORD_OPUS_MIN_BITRATE_KBPS", 96)))
        self.discord_opus_max_bitrate = max(self.discord_opus_min_bitrate, min(512, env_int("MUSIC_AGENT_DISCORD_OPUS_MAX_BITRATE_KBPS", 256)))
        self.discord_opus_source_headroom = max(0, min(128, env_int("MUSIC_AGENT_DISCORD_OPUS_SOURCE_HEADROOM_KBPS", 32)))
        self.default_volume_percent = max(0, min(150, env_int("MUSIC_AGENT_DEFAULT_VOLUME_PERCENT", 55)))
        self.duck_volume_percent = max(0, min(100, env_int("MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT", 8)))
        # PCMVolumeTransformer lets the worker-owned direct voice path duck TTS and restore volume.
        self.direct_pcm_volume_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_PCM_VOLUME_ENABLED"), True)
        self.prepare_timeout = env_float("MUSIC_AGENT_PREPARING_TIMEOUT_SECONDS", 30.0)
        normal_idle = env_float("MUSIC_IDLE_DISCONNECT_SECONDS", 120.0)
        min_idle = max(15.0, env_float("MUSIC_AGENT_MIN_IDLE_DISCONNECT_SECONDS", normal_idle))
        self.idle_disconnect_seconds = max(min_idle, env_float("MUSIC_AGENT_IDLE_DISCONNECT_SECONDS", normal_idle))
        # Cache separado: metadata pode viver muito mais que URL tocável. URLs
        # diretas do YouTube/googlevideo expiram, então o cache de stream é curto
        # e é invalidado em erro de playback.
        legacy_cache_ttl = env_float("MUSIC_AGENT_RESOLVE_CACHE_TTL_SECONDS", 300.0)
        self.metadata_cache_ttl = max(0.0, env_float("MUSIC_AGENT_METADATA_CACHE_TTL_SECONDS", 21600.0))
        self.stream_cache_ttl = max(0.0, env_float("MUSIC_AGENT_STREAM_CACHE_TTL_SECONDS", min(max(legacy_cache_ttl, 1.0), 300.0)))
        self.resolve_cache_ttl = self.stream_cache_ttl
        self.prefetch_enabled = truthy(os.getenv("MUSIC_AGENT_PREFETCH_ENABLED"), True)
        self.prefetch_timeout = max(3.0, env_float("MUSIC_AGENT_PREFETCH_TIMEOUT_SECONDS", 18.0))
        # Prefetch de seleção é especulativo: quando a guild está ociosa ele
        # recebe prioridade maior para reduzir clique -> primeiro áudio. Com
        # música ativa, continua em background para não competir com playback.
        self.selection_prefetch_idle_priority = max(-5, min(20, env_int("MUSIC_AGENT_SELECTION_PREFETCH_IDLE_PRIORITY", 5)))
        self.selection_prefetch_active_priority = max(5, min(40, env_int("MUSIC_AGENT_SELECTION_PREFETCH_ACTIVE_PRIORITY", 20)))
        self.stream_recovery_enabled = truthy(os.getenv("MUSIC_AGENT_STREAM_RECOVERY_ENABLED"), True)
        self.stream_recovery_max_attempts = max(0, min(3, env_int("MUSIC_AGENT_STREAM_RECOVERY_MAX_ATTEMPTS", 1)))
        self.stream_recovery_backtrack_seconds = max(0.0, min(3.0, env_float("MUSIC_AGENT_STREAM_RECOVERY_BACKTRACK_SECONDS", 0.35)))
        default_refresh_age = min(max(self.stream_cache_ttl * 0.75, 30.0), 150.0) if self.stream_cache_ttl > 0 else 120.0
        self.stream_refresh_before_play_seconds = max(15.0, env_float("MUSIC_AGENT_STREAM_REFRESH_BEFORE_PLAY_SECONDS", default_refresh_age))
        self._idle_disconnect_tasks: dict[int, asyncio.Task] = {}
        self._tts_direct_locks: dict[int, asyncio.Lock] = {}
        self._tts_direct_lock_users: dict[int, int] = {}
        self._metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._resolve_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._resolve_locks: dict[str, asyncio.Lock] = {}
        self._resolve_lock_users: dict[str, int] = {}
        self.resolve_max_concurrency = max(1, env_int("MUSIC_AGENT_RESOLVE_MAX_CONCURRENCY", 1))
        self._resolve_active = 0
        self._resolve_waiters: list[tuple[int, int, asyncio.Future]] = []
        self._resolve_waiter_sequence = 0
        self._resolve_scheduler_lock = asyncio.Lock()
        self._resolve_thread_local = threading.local()
        # Manutenção leve e limitada: limpa caches expirados e estados de guild
        # realmente ociosos para o agente poder ficar dias online sem crescer
        # indefinidamente em servidores que já não usam música.
        self.state_idle_ttl_seconds = max(60.0, env_float("MUSIC_AGENT_STATE_IDLE_TTL_SECONDS", 3600.0))
        self.maintenance_interval_seconds = max(30.0, env_float("MUSIC_AGENT_MAINTENANCE_INTERVAL_SECONDS", 60.0))
        self._last_maintenance_monotonic = 0.0
        self._voice_dependencies_cache: tuple[float, dict[str, Any]] | None = None
        self._voice_dependencies_cache_ttl = max(0.0, env_float("MUSIC_AGENT_DEPENDENCY_CACHE_TTL_SECONDS", 30.0))
        self._prefetch_tasks: dict[str, asyncio.Task] = {}
        self._active_resolve_tasks: dict[int, asyncio.Task] = {}
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        self.states: dict[int, GuildMusicState] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: Any = None
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_complete = False
        self._app = web.Application()
        self._app.add_routes([
            web.get("/health", self.handle_health),
            web.post("/command", self.handle_command),
        ])
        self._wire_discord_events()

    def log(self, event: str, *, guild_id: int = 0, **fields: Any) -> None:
        details = " ".join(f"{key}={short_text(value, 220)!r}" for key, value in fields.items() if value is not None and value != "")
        gid = f" guild={guild_id}" if guild_id else ""
        print(f"[music-agent] {event}{gid}{(' ' + details) if details else ''}", flush=True)

    def _wire_discord_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:  # type: ignore[no-untyped-def]
            self.log("discord_ready", user=str(self.client.user), version=AGENT_VERSION, playback="direct")

    @contextlib.asynccontextmanager
    async def _registry_lock(self, locks: dict[Any, asyncio.Lock], users: dict[Any, int], key: Any):
        """Serializa uma chave sem manter locks ociosos para sempre.

        O contador inclui tanto o dono quanto quem está aguardando. Assim a
        entrada só é removida quando nenhum coroutine ainda pode reutilizá-la.
        """
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        users[key] = int(users.get(key, 0) or 0) + 1
        try:
            async with lock:
                yield lock
        finally:
            remaining = max(0, int(users.get(key, 1) or 1) - 1)
            if remaining:
                users[key] = remaining
            else:
                users.pop(key, None)
                if locks.get(key) is lock:
                    locks.pop(key, None)







    def _auth_ok(self, request: web.Request) -> bool:
        if not self.token:
            return True
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}" or request.headers.get("X-Music-Agent-Token") == self.token

    def _cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self._idle_disconnect_tasks.pop(int(guild_id or 0), None)
        if task and not task.done():
            task.cancel()

    def _schedule_idle_disconnect(self, guild_id: int) -> None:
        guild_id = int(guild_id or 0)
        if not guild_id:
            return
        self._cancel_idle_disconnect(guild_id)
        delay = max(15.0, float(self.idle_disconnect_seconds or 120.0))
        self._idle_disconnect_tasks[guild_id] = asyncio.create_task(self._idle_disconnect_later(guild_id, delay))

    async def _idle_disconnect_later(self, guild_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
            if st.current is not None or st.queue or st.status not in {"idle", "stopped"}:
                return
            player = st.player
            if player is None:
                return
            st.player = None
            st.transport = ""
            st.paused = False
            self._set_status(st, "idle", event="idle_timeout_disconnect")
            with contextlib.suppress(Exception):
                if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                    player.stop()
                await player.disconnect(force=True)
            self.log("idle_timeout_disconnect", guild_id=guild_id, delay=round(delay, 1))
        except asyncio.CancelledError:
            return
        finally:
            remove_owned_task(self._idle_disconnect_tasks, guild_id, asyncio.current_task())

    def _set_status(self, st: GuildMusicState, status: str, *, event: str = "", error: str = "") -> None:
        now = time.time()
        st.status = status
        st.updated_at = now
        if event:
            st.last_event = event
        if error:
            st.last_error = short_text(error, 320)
        elif status not in {"failed", "error"}:
            st.last_error = ""
        if status in {"preparing", "starting"}:
            st.preparing_since = now
            st.playing_since = 0.0
            st.paused_monotonic = 0.0
        elif status == "playing":
            if not st.playing_since:
                st.playing_since = now
            if not st.started_monotonic:
                st.started_monotonic = time.monotonic()
            st.preparing_since = 0.0
        elif status in {"idle", "failed", "error"}:
            st.preparing_since = 0.0
            if status != "playing":
                st.playing_since = 0.0
                st.started_monotonic = 0.0
                st.paused_monotonic = 0.0

    async def handle_health(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        query = getattr(request, "query", {}) or {}
        guild_id = safe_id(query.get("guild_id")) if hasattr(query, "get") else 0
        compact = truthy(query.get("compact"), bool(guild_id)) if hasattr(query, "get") else False
        known_revision = str(query.get("known_revision") or "").strip() if hasattr(query, "get") else ""
        return web.json_response(self.status_payload(guild_id=guild_id, compact=compact, known_revision=known_revision))

    async def handle_command(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            result = await self.dispatch(body)
            return web.json_response(result)
        except Exception as exc:
            self.log("command_error", action=body.get("action"), error=f"{type(exc).__name__}: {exc}")
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {short_text(exc, 300)}", "status": self.status_payload()}, status=400)

    def voice_dependencies_payload(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._voice_dependencies_cache
        if (
            not force
            and cached is not None
            and self._voice_dependencies_cache_ttl > 0
            and now - cached[0] <= self._voice_dependencies_cache_ttl
        ):
            payload = cached[1]
            return {
                "ok": bool(payload.get("ok")),
                "missing": list(payload.get("missing") or []),
                "optional_missing": list(payload.get("optional_missing") or []),
                "checks": {name: dict(info) for name, info in dict(payload.get("checks") or {}).items()},
            }

        checks: dict[str, dict[str, Any]] = {}
        modules = {
            "discord.py": "discord",
            "PyNaCl": "nacl",
            "davey": "davey",
            "yt-dlp": "yt_dlp",
            "aiohttp": "aiohttp",
            "gTTS": "gtts",
            "edge-tts": "edge_tts",
        }
        optional_modules: set[str] = {"gTTS", "edge-tts"}
        for label, module in modules.items():
            try:
                importlib.import_module(module)
                checks[label] = {"ok": True, "optional": label in optional_modules}
            except Exception as exc:
                checks[label] = {"ok": False, "optional": label in optional_modules, "error": f"{type(exc).__name__}: {short_text(exc, 120)}"}
        for binary in ("ffmpeg", "ffprobe"):
            path = shutil.which(binary)
            checks[binary] = {"ok": bool(path), "path": path or ""}
        missing = [name for name, info in checks.items() if not bool(info.get("ok"))]
        missing_critical = [name for name in missing if not bool(checks.get(name, {}).get("optional"))]
        optional_missing = [name for name in missing if bool(checks.get(name, {}).get("optional"))]
        payload = {"ok": not missing_critical, "missing": missing_critical, "optional_missing": optional_missing, "checks": checks}
        self._voice_dependencies_cache = (now, payload)
        return {
            "ok": bool(payload["ok"]),
            "missing": list(payload["missing"]),
            "optional_missing": list(payload["optional_missing"]),
            "checks": {name: dict(info) for name, info in payload["checks"].items()},
        }

    @staticmethod
    def _prune_expired_cache_entries(cache: dict[str, tuple[float, dict[str, Any]]], ttl: float, now: float) -> int:
        if ttl <= 0 or not cache:
            return 0
        expired = [key for key, item in cache.items() if now - float(item[0]) > ttl]
        for key in expired:
            cache.pop(key, None)
        return len(expired)

    def _maybe_run_maintenance(self) -> None:
        now_mono = time.monotonic()
        if now_mono - self._last_maintenance_monotonic < self.maintenance_interval_seconds:
            return
        self._last_maintenance_monotonic = now_mono

        metadata_removed = self._prune_expired_cache_entries(self._metadata_cache, self.metadata_cache_ttl, now_mono)
        stream_removed = self._prune_expired_cache_entries(self._resolve_cache, self.stream_cache_ttl, now_mono)

        state_removed = 0
        now_wall = time.time()
        for guild_id, st in list(self.states.items()):
            if now_wall - float(getattr(st, "updated_at", now_wall) or now_wall) <= self.state_idle_ttl_seconds:
                continue
            if st.current is not None or st.queue or st.player is not None:
                continue
            if str(getattr(st, "status", "idle") or "idle").lower() not in {"idle", "stopped", "failed", "error"}:
                continue
            if guild_id in self._idle_disconnect_tasks or guild_id in self._active_resolve_tasks:
                continue
            if int(self._tts_direct_lock_users.get(guild_id, 0) or 0) > 0:
                continue
            prefix = f"{int(guild_id)}:"
            if any(str(key).startswith(prefix) and task is not None and not task.done() for key, task in self._prefetch_tasks.items()):
                continue
            if self.states.get(guild_id) is st:
                self.states.pop(guild_id, None)
                state_removed += 1

        if metadata_removed or stream_removed or state_removed:
            self.log(
                "maintenance_pruned",
                metadata=metadata_removed,
                streams=stream_removed,
                guild_states=state_removed,
            )

    def status_payload(self, *, guild_id: int = 0, compact: bool = False, known_revision: str = "") -> dict[str, Any]:
        self._maybe_run_maintenance()
        guild_id = int(guild_id or 0)
        base = {
            "ok": True,
            "available": bool(self.client.is_ready()),
            "version": AGENT_VERSION,
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "discord_ready": bool(self.client.is_ready()),
            "user": str(self.client.user) if self.client.user else "",
            "playback_backend": "discord-voice-direct",
            "direct_audio_enabled": self.direct_audio_enabled,
        }
        if compact and guild_id > 0:
            state = self.states.get(guild_id)
            if state is None:
                base["guilds"] = {}
                return base
            revision = state.state_revision()
            base["state_revision"] = revision
            if known_revision and str(known_revision) == revision:
                # Resposta condicional pequena: o monitor da VPS já possui o
                # snapshot completo desta revisão. Não serialize fila/faixa e
                # telemetria volátil novamente até ocorrer uma mudança real.
                base["unchanged"] = True
                base["guilds"] = {}
                return base
            base["guilds"] = {str(guild_id): state.public()}
            return base

        base.update({
            "idle_disconnect_seconds": self.idle_disconnect_seconds,
            "cache": {
                "metadata_entries": len(self._metadata_cache),
                "stream_entries": len(self._resolve_cache),
                "metadata_ttl_seconds": self.metadata_cache_ttl,
                "stream_ttl_seconds": self.stream_cache_ttl,
            },
            "voice_dependencies": self.voice_dependencies_payload(),
            "guilds": {str(gid): state.public() for gid, state in self.states.items()},
        })
        return base

    async def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        self._maybe_run_maintenance()
        action = str(body.get("action") or body.get("command") or "status").strip().lower().replace("-", "_")
        if action in {"status", "get_state"}:
            guild_id = safe_id(body.get("guild_id"))
            return self.status_payload(
                guild_id=guild_id,
                compact=truthy(body.get("compact"), bool(guild_id)),
                known_revision=str(body.get("known_revision") or "").strip(),
            )
        if action in {"play", "enqueue", "play_direct", "enqueue_many", "queue_many", "add_many", "playlist"}:
            body = dict(body)
            body["_agent_action"] = action
            return await self.cmd_play(body)
        if action == "pause":
            return await self.cmd_pause(body)
        if action in {"resume", "unpause"}:
            return await self.cmd_resume(body)
        if action in {"stop", "disconnect"}:
            return await self.cmd_stop(body)
        if action in {"skip", "next"}:
            return await self.cmd_skip(body)
        if action in {"previous", "back", "prev", "anterior", "voltar"}:
            return await self.cmd_previous(body)
        if action == "volume":
            return await self.cmd_volume(body)
        if action in {"shuffle", "shuffle_queue", "mix_queue"}:
            return await self.cmd_shuffle(body)
        if action in {"loop", "repeat", "cycle_loop", "repeat_mode"}:
            return await self.cmd_loop(body)
        if action in {"seek", "set_position", "select_moment"}:
            return await self.cmd_seek(body)
        if action in {"duck", "duck_volume", "tts_duck"}:
            return await self.cmd_duck(body)
        if action in {"unduck", "restore_volume", "tts_restore"}:
            return await self.cmd_unduck(body)
        if action in {"voice_tts", "voice_tts_direct", "direct_tts", "tts_direct"}:
            return await self._run_tts_request(body, direct=True)
        if action in {"tts", "tts_play", "speak"}:
            return await self._run_tts_request(body, direct=False)
        if action == "cancel_tts":
            return await self.cmd_cancel_tts(body)
        if action in {"prefetch", "prepare", "preload"}:
            return await self.cmd_prefetch(body)
        raise ValueError("ação do Music Agent não suportada")

































































    async def _cleanup_http_runner(self) -> None:
        runner = self._runner
        if runner is None:
            return
        self._runner = None
        with contextlib.suppress(Exception):
            await runner.cleanup()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return

            active_tts = list(self._tts_requests().values())
            self._active_tts_requests.clear()
            await cancel_tasks(active_tts)

            background = list(self._idle_disconnect_tasks.values()) + list(self._prefetch_tasks.values())
            self._idle_disconnect_tasks.clear()
            self._prefetch_tasks.clear()
            await cancel_tasks(background)

            players: list[Any] = []
            seen_players: set[int] = set()
            for st in self.states.values():
                self._bump_playback_generation(st, reason="shutdown")
                player = st.player
                st.player = None
                st.paused = False
                st.ducked = False
                st.transport = ""
                self._set_status(st, "stopped", event="shutdown")
                if player is not None and id(player) not in seen_players:
                    seen_players.add(id(player))
                    players.append(player)
            for player in players:
                await self._stop_player_instance(player, disconnect=True)

            close = getattr(self.client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            await self._cleanup_http_runner()
            self._shutdown_complete = True

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        runner = web.AppRunner(self._app)
        self._runner = runner
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
            self.log("api_ready", url=f"http://{self.host}:{self.port}", token="sim" if self.token else "não")
            if not self.discord_token:
                raise RuntimeError("defina MUSIC_AGENT_BOT_TOKEN, DISCORD_TOKEN ou BOT_TOKEN no worker")
            await self.client.start(self.discord_token)
        finally:
            if self._runner is runner:
                await self._cleanup_http_runner()


async def amain() -> None:
    agent = MusicAgent()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    run_task = asyncio.create_task(agent.run())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _pending = await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if run_task in done:
            await run_task
            return
        await agent.shutdown()
        if not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
    finally:
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await agent.shutdown()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        for sig in signals:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)


if __name__ == "__main__":
    asyncio.run(amain())
