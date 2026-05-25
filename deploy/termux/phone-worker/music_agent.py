#!/usr/bin/env python3
"""Core Music Agent for the phone worker.

Same-bot music plane: the VPS remains the UI/status plane while
this process owns Discord voice/Lavalink/yt-dlp on the phone worker.

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
import os
import re
import shutil
import signal
import subprocess
import secrets
import tempfile
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from aiohttp import web
except Exception as exc:  # pragma: no cover - startup dependency error
    raise SystemExit(f"aiohttp ausente no Music Agent: {exc}")

try:
    import discord
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"discord.py ausente no Music Agent: {exc}")

try:
    import wavelink
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"wavelink ausente no Music Agent: {exc}")

AGENT_VERSION = "0.3.12"
STARTED_AT = time.time()


def load_env_file(path: Path, *, override: bool = False) -> None:
    try:
        lines = path.expanduser().read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if override or key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def bootstrap_env() -> None:
    worker_dir = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    load_env_file(Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"), override=False)
    env_file = Path(os.getenv("MUSIC_AGENT_ENV") or worker_dir / "secrets" / "music-agent.env").expanduser()
    load_env_file(env_file, override=False)
    if not str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip():
        token = secrets.token_urlsafe(32)
        os.environ["MUSIC_AGENT_TOKEN"] = token
        try:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            old = env_file.read_text("utf-8", errors="replace") if env_file.exists() else ""
            lines: list[str] = []
            replaced = False
            for line in old.splitlines():
                if re.match(r"^\s*MUSIC_AGENT_TOKEN\s*=", line):
                    if not replaced:
                        lines.append("MUSIC_AGENT_TOKEN=" + token)
                        replaced = True
                    continue
                lines.append(line)
            if not replaced:
                lines.append("MUSIC_AGENT_TOKEN=" + token)
            env_file.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
            with contextlib.suppress(Exception):
                os.chmod(env_file, 0o600)
        except Exception:
            pass


bootstrap_env()


_LOCAL_SEARCH_PREFIXES = ("ytsearch", "ytmsearch")
_LAVALINK_PREFIXES = ("scsearch:", "spsearch:", "amsearch:", "dzsearch:")


def truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return text in {"1", "true", "yes", "y", "on", "sim"}


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def short_text(value: object, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def safe_id(value: object) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return 0


def read_lavalink_password_from_yaml() -> str:
    path = Path(os.getenv("MUSIC_AGENT_LAVALINK_CONFIG") or Path.home() / "lavalink" / "application.yml").expanduser()
    if not path.exists():
        return ""
    try:
        for line in path.read_text(errors="ignore").splitlines():
            if line.strip().lower().startswith("password:"):
                return line.split(":", 1)[1].strip().strip('"\'')
    except Exception:
        return ""
    return ""


def _looks_like_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value) != "" else None
    except Exception:
        return None


def _metadata_text(value: Any, *, limit: int = 180) -> str:
    text = short_text(value, limit)
    lower = text.lower()
    if lower in {"youtube", "link", "música", "musica", "worker-agent", "music-agent-ytdlp", "worker-ytdlp", "desconhecida", "unknown"}:
        return ""
    if "desconhecida" in lower and ("youtube" in lower or "worker" in lower):
        return ""
    return text


def _duration_from_ytdlp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL"}:
        return None
    try:
        return float(text)
    except Exception:
        pass
    parts = text.split(":")
    try:
        total = 0
        for part in parts:
            total = total * 60 + int(float(part))
        return float(total)
    except Exception:
        return None


PCM_FRAME_BYTES = 3840


class AgentMixedAudioSource(discord.AudioSource):
    """PCM mixer used by the worker-owned voice session.

    It keeps music and TTS inside the same Discord voice connection, so TTS does
    not make the VPS bot steal/disconnect the Music Agent session.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop, music_source: discord.AudioSource, music_volume: float, duck_factor: float = 0.08) -> None:
        self.loop = loop
        self.music_source = music_source
        self.normal_music_volume = max(0.0, min(2.0, float(music_volume)))
        self.duck_factor = max(0.0, min(1.0, float(duck_factor)))
        self._overlays: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._closed = False
        self._music_ended = False

    def is_opus(self) -> bool:
        return False

    def set_music_volume(self, volume: float) -> None:
        self.normal_music_volume = max(0.0, min(2.0, float(volume)))

    def set_duck_factor(self, factor: float) -> None:
        self.duck_factor = max(0.0, min(1.0, float(factor)))

    def add_tts(self, source: discord.AudioSource, *, volume: float = 1.0) -> asyncio.Future:
        future = self.loop.create_future()
        with self._lock:
            self._overlays.append({"source": source, "volume": max(0.0, min(2.0, float(volume))), "future": future, "ended": False})
        return future

    def _future_result(self, future: asyncio.Future, value: object = None) -> None:
        def _set() -> None:
            if not future.done():
                future.set_result(value)
        self.loop.call_soon_threadsafe(_set)

    def _future_exception(self, future: asyncio.Future, error: Exception) -> None:
        def _set() -> None:
            if not future.done():
                future.set_exception(error)
        self.loop.call_soon_threadsafe(_set)

    def _limit(self, value: int) -> int:
        return max(-32768, min(32767, int(value)))

    def _samples(self, frame: bytes, volume: float) -> array:
        samples = array("h")
        samples.frombytes(frame)
        if abs(volume - 1.0) > 0.001:
            for i, sample in enumerate(samples):
                samples[i] = self._limit(int(sample * volume))
        return samples

    def _mix_into(self, base: array, frame: bytes, volume: float) -> None:
        if not frame:
            return
        other = self._samples(frame, volume)
        if len(other) < len(base):
            other.extend([0] * (len(base) - len(other)))
        elif len(other) > len(base):
            del other[len(base):]
        for i, sample in enumerate(other):
            base[i] = self._limit(int(base[i]) + int(sample))

    def read(self) -> bytes:
        if self._closed:
            return b""
        with self._lock:
            overlays = list(self._overlays)
        music_frame = b""
        if not self._music_ended:
            music_frame = self.music_source.read()
            if not music_frame:
                self._music_ended = True
                with contextlib.suppress(Exception):
                    self.music_source.cleanup()
        if not music_frame and not overlays:
            self.cleanup()
            return b""
        music_volume = self.normal_music_volume * (self.duck_factor if overlays else 1.0)
        if music_frame:
            base = self._samples(music_frame, music_volume)
        else:
            base = array("h", [0] * (PCM_FRAME_BYTES // 2))
        ended: list[dict[str, Any]] = []
        for overlay in overlays:
            source = overlay.get("source")
            future = overlay.get("future")
            try:
                frame = source.read() if source is not None else b""
            except Exception as exc:
                if isinstance(future, asyncio.Future):
                    self._future_exception(future, exc)
                ended.append(overlay)
                continue
            if frame:
                self._mix_into(base, frame, float(overlay.get("volume") or 1.0))
            else:
                with contextlib.suppress(Exception):
                    source.cleanup()
                if isinstance(future, asyncio.Future):
                    self._future_result(future, None)
                ended.append(overlay)
        if ended:
            with self._lock:
                self._overlays = [ov for ov in self._overlays if ov not in ended]
        return base.tobytes()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.music_source.cleanup()
        with self._lock:
            overlays = list(self._overlays)
            self._overlays.clear()
        for overlay in overlays:
            with contextlib.suppress(Exception):
                overlay.get("source").cleanup()
            future = overlay.get("future")
            if isinstance(future, asyncio.Future):
                self._future_result(future, None)


def _select_stream_url(entry: dict[str, Any]) -> str:
    for item in entry.get("requested_downloads") or []:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    url = str(entry.get("url") or "").strip()
    if url.startswith(("http://", "https://")) and "youtube.com/watch" not in url and "youtu.be/" not in url:
        return url
    best_url = ""
    best_score = -1.0
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        candidate = str(fmt.get("url") or "").strip()
        if not candidate.startswith(("http://", "https://")):
            continue
        acodec = str(fmt.get("acodec") or "").lower()
        if acodec in {"", "none"}:
            continue
        score = float(fmt.get("abr") or fmt.get("tbr") or 0)
        if str(fmt.get("vcodec") or "").lower() in {"", "none"}:
            score += 10000
        if score > best_score:
            best_url = candidate
            best_score = score
    return best_url


@dataclass
class AgentTrack:
    title: str = "Música"
    requester_id: int = 0
    requester_name: str = ""
    query: str = ""
    webpage_url: str = ""
    stream_url: str = ""
    duration: float | None = None
    uploader: str = ""
    thumbnail: str = ""
    source: str = "worker-agent"
    transport_hint: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "requester_id": self.requester_id,
            "requester_name": self.requester_name,
            "query": self.query,
            "webpage_url": self.webpage_url,
            "duration": self.duration,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
            "source": self.source,
            "transport_hint": self.transport_hint,
        }


@dataclass
class GuildMusicState:
    guild_id: int
    voice_channel_id: int = 0
    text_channel_id: int = 0
    current: AgentTrack | None = None
    queue: list[AgentTrack] = field(default_factory=list)
    status: str = "idle"
    paused: bool = False
    last_error: str = ""
    last_action: str = ""
    last_event: str = ""
    transport: str = ""
    preparing_since: float = 0.0
    playing_since: float = 0.0
    started_monotonic: float = 0.0
    updated_at: float = field(default_factory=time.time)
    player: Any = None
    volume_percent: int = 55
    normal_volume_percent: int = 55
    ducked: bool = False

    def public(self) -> dict[str, Any]:
        player = self.player
        voice_connected = False
        playing = False
        position_ms = 0
        if player is not None:
            with contextlib.suppress(Exception):
                connected_attr = getattr(player, "connected", None)
                voice_connected = bool(connected_attr) if connected_attr is not None else voice_connected
            with contextlib.suppress(Exception):
                checker = getattr(player, "is_connected", None)
                if callable(checker):
                    voice_connected = bool(checker())
            with contextlib.suppress(Exception):
                checker = getattr(player, "is_playing", None)
                if callable(checker):
                    playing = bool(checker())
            with contextlib.suppress(Exception):
                playing = bool(playing or getattr(player, "playing", False))
            with contextlib.suppress(Exception):
                position_ms = int(float(getattr(player, "position", 0) or 0))
        status_age = max(0.0, time.time() - float(self.updated_at or time.time()))
        return {
            "guild_id": self.guild_id,
            "voice_channel_id": self.voice_channel_id,
            "text_channel_id": self.text_channel_id,
            "status": self.status,
            "paused": self.paused,
            "last_error": self.last_error,
            "last_action": self.last_action,
            "last_event": self.last_event,
            "transport": self.transport,
            "preparing_since": self.preparing_since,
            "playing_since": self.playing_since,
            "status_age_seconds": round(status_age, 2),
            "voice_connected": voice_connected,
            "player_present": player is not None,
            "player_playing": playing,
            "position_ms": position_ms,
            "confirmed_playing": bool(
                self.status == "playing"
                and player is not None
                and voice_connected
                and (playing or position_ms > 0)
            ),
            "updated_at": self.updated_at,
            "current": self.current.public() if self.current else None,
            "queue_size": len(self.queue),
            "volume_percent": self.volume_percent,
            "normal_volume_percent": self.normal_volume_percent,
            "ducked": self.ducked,
            "queue": [item.public() for item in self.queue[:10]],
        }


class MusicAgent:
    def __init__(self) -> None:
        self.host = os.getenv("MUSIC_AGENT_HOST", "127.0.0.1")
        self.port = env_int("MUSIC_AGENT_PORT", 8780)
        self.token = os.getenv("MUSIC_AGENT_TOKEN") or os.getenv("PHONE_WORKER_TOKEN") or ""
        self.discord_token = os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
        self.lavalink_uri = os.getenv("MUSIC_AGENT_LAVALINK_URI") or os.getenv("LAVALINK_URI") or "http://127.0.0.1:2333"
        self.lavalink_password = os.getenv("MUSIC_AGENT_LAVALINK_PASSWORD") or os.getenv("LAVALINK_PASSWORD") or read_lavalink_password_from_yaml()
        self.lavalink_node_name = os.getenv("MUSIC_AGENT_LAVALINK_NODE_NAME", "phone-agent")
        self.ytdlp_format = os.getenv("MUSIC_AGENT_YTDLP_FORMAT") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_FORMAT") or "bestaudio[acodec=opus]/bestaudio/best"
        self.ytdlp_timeout = env_int("MUSIC_AGENT_YTDLP_TIMEOUT_SECONDS", 35)
        self.cookies_file = os.getenv("MUSIC_AGENT_YTDLP_COOKIES_FILE") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE") or str(Path.home() / "phone-worker" / "secrets" / "youtube-cookies.txt")
        self.js_runtimes = os.getenv("MUSIC_AGENT_YTDLP_JS_RUNTIMES") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES") or "node"
        self.default_search = os.getenv("MUSIC_AGENT_YTDLP_DEFAULT_SEARCH") or "ytsearch5"
        self.direct_audio_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_AUDIO_ENABLED"), True)
        self.direct_youtube_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_YOUTUBE_ENABLED"), True)
        self.lavalink_for_direct_streams = truthy(os.getenv("MUSIC_AGENT_LAVALINK_FOR_DIRECT_STREAMS"), False)
        self.ffmpeg_executable = os.getenv("MUSIC_AGENT_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
        self.ffmpeg_before_options = os.getenv(
            "MUSIC_AGENT_FFMPEG_BEFORE_OPTIONS",
            "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,408,429,5xx -reconnect_delay_max 5 -rw_timeout 15000000",
        )
        self.ffmpeg_options = os.getenv("MUSIC_AGENT_FFMPEG_OPTIONS", "-vn -sn -dn -loglevel warning")
        self.ffmpeg_bitrate = env_int("MUSIC_AGENT_FFMPEG_OPUS_BITRATE_KBPS", 128)
        self.default_volume_percent = max(0, min(150, env_int("MUSIC_AGENT_DEFAULT_VOLUME_PERCENT", 55)))
        self.duck_volume_percent = max(0, min(100, env_int("MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT", 8)))
        # PCMVolumeTransformer lets the worker-owned direct voice path duck TTS and restore volume.
        self.direct_pcm_volume_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_PCM_VOLUME_ENABLED"), True)
        self.prepare_timeout = env_float("MUSIC_AGENT_PREPARING_TIMEOUT_SECONDS", 30.0)
        self.idle_disconnect_seconds = max(15.0, env_float("MUSIC_AGENT_IDLE_DISCONNECT_SECONDS", env_float("MUSIC_IDLE_DISCONNECT_SECONDS", 120.0)))
        self._idle_disconnect_tasks: dict[int, asyncio.Task] = {}
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        self.states: dict[int, GuildMusicState] = {}
        self._pool_connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
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
            self.log("discord_ready", user=str(self.client.user), version=AGENT_VERSION)
            with contextlib.suppress(Exception):
                await self.ensure_lavalink_pool()

        @self.client.event
        async def on_wavelink_track_start(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                if st.transport == "lavalink":
                    self._set_status(st, "playing", event="lavalink_track_start")
                    self.log("play_started", guild_id=guild_id, transport="lavalink", title=getattr(st.current, "title", ""))

        @self.client.event
        async def on_wavelink_track_end(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                self.log("play_ended", guild_id=guild_id, transport="lavalink")
                await self._finish_current(guild_id, error=None, event="lavalink_track_end")

        @self.client.event
        async def on_wavelink_track_exception(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                err = short_text(getattr(payload, "exception", "erro no Lavalink"), 260)
                st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                self._set_status(st, "failed", event="lavalink_track_exception", error=err)
                self.log("play_failed", guild_id=guild_id, transport="lavalink", error=err)
                if st.queue:
                    await self._play_next(guild_id)

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
                if isinstance(player, getattr(wavelink, "Player", ())):
                    await player.disconnect()
                else:
                    if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                        player.stop()
                    await player.disconnect(force=True)
            self.log("idle_timeout_disconnect", guild_id=guild_id, delay=round(delay, 1))
        except asyncio.CancelledError:
            return
        finally:
            self._idle_disconnect_tasks.pop(guild_id, None)

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

    async def handle_health(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response(self.status_payload())

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

    def voice_dependencies_payload(self) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        modules = {
            "discord.py": "discord",
            "PyNaCl": "nacl",
            "davey": "davey",
            "yt-dlp": "yt_dlp",
            "wavelink": "wavelink",
            "aiohttp": "aiohttp",
            "gTTS": "gtts",
            "edge-tts": "edge_tts",
            "google-cloud-texttospeech": "google.cloud.texttospeech_v1",
        }
        optional_modules = {"google-cloud-texttospeech"}
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
        return {"ok": not missing_critical, "missing": missing_critical, "optional_missing": optional_missing, "checks": checks}

    def status_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "available": bool(self.client.is_ready()),
            "version": AGENT_VERSION,
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "discord_ready": bool(self.client.is_ready()),
            "user": str(self.client.user) if self.client.user else "",
            "lavalink_uri": self.lavalink_uri,
            "lavalink_node": self.lavalink_node_name,
            "pool_connected": self._pool_connected,
            "direct_audio_enabled": self.direct_audio_enabled,
            "voice_dependencies": self.voice_dependencies_payload(),
            "guilds": {str(gid): state.public() for gid, state in self.states.items()},
        }

    async def ensure_lavalink_pool(self) -> None:
        if self._pool_connected:
            return
        if not self.lavalink_uri or not self.lavalink_password:
            raise RuntimeError("Lavalink do worker não configurado para o Music Agent")
        self.log("lavalink_pool_connecting", uri=self.lavalink_uri, node=self.lavalink_node_name)
        node = wavelink.Node(uri=self.lavalink_uri, password=self.lavalink_password, identifier=self.lavalink_node_name)
        try:
            await wavelink.Pool.connect(nodes=[node], client=self.client, cache_capacity=100)
        except TypeError:
            await wavelink.Pool.connect(nodes=[node], client=self.client)
        except Exception:
            self._pool_connected = False
            raise
        self._pool_connected = True
        self.log("lavalink_pool_ready", node=self.lavalink_node_name)

    async def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or body.get("command") or "status").strip().lower().replace("-", "_")
        if action in {"status", "get_state"}:
            return self.status_payload()
        if action in {"play", "enqueue", "play_direct"}:
            return await self.cmd_play(body)
        if action == "pause":
            return await self.cmd_pause(body)
        if action in {"resume", "unpause"}:
            return await self.cmd_resume(body)
        if action in {"stop", "disconnect"}:
            return await self.cmd_stop(body)
        if action in {"skip", "next"}:
            return await self.cmd_skip(body)
        if action == "volume":
            return await self.cmd_volume(body)
        if action in {"duck", "duck_volume", "tts_duck"}:
            return await self.cmd_duck(body)
        if action in {"unduck", "restore_volume", "tts_restore"}:
            return await self.cmd_unduck(body)
        if action in {"tts", "tts_play", "speak"}:
            return await self.cmd_tts(body)
        raise ValueError("ação do Music Agent não suportada")

    async def cmd_play(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        voice_channel_id = safe_id(body.get("voice_channel_id"))
        text_channel_id = safe_id(body.get("text_channel_id"))
        if not guild_id or not voice_channel_id:
            raise ValueError("guild_id e voice_channel_id são obrigatórios")
        query = str(body.get("query") or body.get("url") or body.get("webpage_url") or body.get("original_url") or "").strip()
        track_meta = body.get("track") if isinstance(body.get("track"), dict) else {}
        if not query:
            query = str(track_meta.get("webpage_url") or track_meta.get("original_url") or track_meta.get("stream_url") or track_meta.get("title") or "").strip()
        if not query:
            raise ValueError("query/url vazia")
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.voice_channel_id = voice_channel_id
        st.text_channel_id = text_channel_id
        if not st.normal_volume_percent:
            st.normal_volume_percent = self.default_volume_percent
        if not st.volume_percent:
            st.volume_percent = st.normal_volume_percent
        st.last_action = "play"
        self._cancel_idle_disconnect(guild_id)
        self.log("play_received", guild_id=guild_id, voice=voice_channel_id, query=query)
        track = await self.resolve_track(query, track_meta=track_meta, body=body)
        if st.current and st.status in {"playing", "starting", "preparing", "paused"}:
            st.queue.append(track)
            st.updated_at = time.time()
            self.log("queued", guild_id=guild_id, title=track.title, queue_size=len(st.queue))
            return {"ok": True, "queued": True, "state": st.public()}
        st.queue.append(track)
        await self._play_next(guild_id)
        if st.status in {"failed", "error"}:
            return {"ok": False, "queued": False, "error": st.last_error or "falha ao iniciar playback", "state": st.public()}
        return {"ok": True, "queued": False, "state": st.public()}

    async def cmd_pause(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.states.setdefault(safe_id(body.get("guild_id")), GuildMusicState(guild_id=safe_id(body.get("guild_id"))))
        player = st.player
        if player:
            if isinstance(player, getattr(wavelink, "Player", ())):
                await player.pause(True)
            elif hasattr(player, "pause"):
                player.pause()
            st.paused = True
            self._set_status(st, "paused", event="pause")
        return {"ok": True, "state": st.public()}

    async def cmd_resume(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.states.setdefault(safe_id(body.get("guild_id")), GuildMusicState(guild_id=safe_id(body.get("guild_id"))))
        player = st.player
        if player:
            if isinstance(player, getattr(wavelink, "Player", ())):
                await player.pause(False)
            elif hasattr(player, "resume"):
                player.resume()
            st.paused = False
            self._set_status(st, "playing", event="resume")
        return {"ok": True, "state": st.public()}

    async def cmd_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "stop"
        self._cancel_idle_disconnect(guild_id)
        st.queue.clear()
        st.current = None
        self._set_status(st, "idle", event="stop")
        st.paused = False
        player = st.player
        st.player = None
        if player:
            with contextlib.suppress(Exception):
                if isinstance(player, getattr(wavelink, "Player", ())):
                    await player.stop()
                    await player.disconnect()
                else:
                    if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                        player.stop()
                    await player.disconnect(force=True)
        return {"ok": True, "state": st.public()}

    async def cmd_skip(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "skip"
        player = st.player
        st.current = None
        if player:
            with contextlib.suppress(Exception):
                if isinstance(player, getattr(wavelink, "Player", ())):
                    await player.stop()
                elif getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                    player.stop()
        await self._play_next(guild_id)
        return {"ok": True, "state": st.public()}

    async def _apply_player_volume(self, st: GuildMusicState, volume: int) -> bool:
        volume = max(0, min(1000, int(volume)))
        player = st.player
        if player is None:
            st.volume_percent = volume
            return False
        applied = False
        setter = getattr(player, "set_volume", None)
        if callable(setter):
            maybe = setter(volume)
            if asyncio.iscoroutine(maybe):
                await maybe
            applied = True
        # discord.VoiceClient exposes the active source; when it is a
        # PCMVolumeTransformer, changing source.volume is immediate.
        source = getattr(player, "source", None)
        if source is not None and hasattr(source, "set_music_volume"):
            with contextlib.suppress(Exception):
                source.set_music_volume(max(0.0, min(10.0, volume / 100.0)))
                applied = True
        elif source is not None and hasattr(source, "volume"):
            with contextlib.suppress(Exception):
                source.volume = max(0.0, min(10.0, volume / 100.0))
                applied = True
        st.volume_percent = volume
        st.updated_at = time.time()
        return applied

    async def cmd_volume(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        volume = max(0, min(1000, int(float(body.get("volume") or body.get("volume_percent") or self.default_volume_percent))))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.normal_volume_percent = volume
        if not st.ducked:
            await self._apply_player_volume(st, volume)
        return {"ok": True, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "ducked": st.ducked, "state": st.public()}

    async def cmd_duck(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        requested = body.get("volume") if body.get("volume") is not None else body.get("volume_percent")
        if requested is None:
            requested = self.duck_volume_percent
        duck_volume = max(0, min(100, int(float(requested))))
        if not st.normal_volume_percent:
            st.normal_volume_percent = max(0, min(150, int(st.volume_percent or self.default_volume_percent)))
        st.ducked = True
        await self._apply_player_volume(st, duck_volume)
        self.log("tts_duck", guild_id=guild_id, volume=duck_volume, normal=st.normal_volume_percent)
        return {"ok": True, "ducked": True, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "state": st.public()}

    async def cmd_unduck(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        restore = max(0, min(150, int(float(body.get("volume") or body.get("volume_percent") or st.normal_volume_percent or self.default_volume_percent))))
        st.ducked = False
        st.normal_volume_percent = restore
        await self._apply_player_volume(st, restore)
        self.log("tts_restore", guild_id=guild_id, volume=restore)
        return {"ok": True, "ducked": False, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "state": st.public()}

    def _normalize_tts_language(self, value: Any) -> str:
        raw = str(value or "pt-br").strip().lower().replace("_", "-")
        if raw in {"pt", "ptbr", "pt-br", "br"}:
            return "pt-br"
        if raw in {"en", "en-us", "us"}:
            return "en"
        return raw or "pt-br"

    def _normalize_edge_rate(self, value: Any) -> str:
        raw = str(value or "+0%").strip() or "+0%"
        if re.match(r"^[+-]?\d+%$", raw):
            return raw if raw.startswith(("+", "-")) else "+" + raw
        return "+0%"

    def _normalize_edge_pitch(self, value: Any) -> str:
        raw = str(value or "+0Hz").strip() or "+0Hz"
        if re.match(r"^[+-]?\d+Hz$", raw, re.I):
            return raw if raw.startswith(("+", "-")) else "+" + raw
        return "+0Hz"

    async def _synthesize_tts_file(self, body: dict[str, Any], target: Path) -> str:
        text = short_text(body.get("text") or body.get("content") or "", 1600)
        if not text:
            raise ValueError("texto TTS vazio")
        engine = str(body.get("engine") or "gtts").strip().lower().replace("-", "_")
        if engine in {"google", "google_tts", "googlecloud", "google_cloud"}:
            engine = "gcloud"
        if engine == "edge":
            try:
                import edge_tts  # type: ignore
            except Exception as exc:
                raise RuntimeError(f"edge-tts ausente no worker: {type(exc).__name__}") from exc
            voice = str(body.get("voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
            rate = self._normalize_edge_rate(body.get("rate"))
            pitch = self._normalize_edge_pitch(body.get("pitch"))
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
            await communicate.save(str(target))
            return "edge"
        if engine == "gcloud":
            try:
                from google.cloud import texttospeech_v1 as google_texttospeech  # type: ignore
                language = self._normalize_tts_language(body.get("language")).replace("pt-br", "pt-BR")
                voice_name = str(body.get("voice") or "").strip()
                client = google_texttospeech.TextToSpeechClient()
                voice_kwargs = {"language_code": language}
                if voice_name:
                    voice_kwargs["name"] = voice_name
                response = client.synthesize_speech(
                    request=google_texttospeech.SynthesizeSpeechRequest(
                        input=google_texttospeech.SynthesisInput(text=text),
                        voice=google_texttospeech.VoiceSelectionParams(**voice_kwargs),
                        audio_config=google_texttospeech.AudioConfig(audio_encoding=google_texttospeech.AudioEncoding.MP3),
                    )
                )
                target.write_bytes(response.audio_content)
                return "gcloud"
            except Exception as exc:
                self.log("tts_gcloud_fallback_gtts", error=f"{type(exc).__name__}: {short_text(exc, 120)}")
                engine = "gtts"
        try:
            from gtts import gTTS  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"gTTS ausente no worker: {type(exc).__name__}") from exc
        language = self._normalize_tts_language(body.get("language"))
        tts = gTTS(text=text, lang=language)
        with open(target, "wb") as handle:
            await asyncio.to_thread(tts.write_to_fp, handle)
        return "gtts"

    async def cmd_tts(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        player = st.player
        if not guild_id or player is None or st.current is None:
            return {"ok": False, "error": "sem sessão musical ativa no worker", "state": st.public()}
        source = getattr(player, "source", None)
        if not isinstance(source, AgentMixedAudioSource):
            return {"ok": False, "error": "sessão atual não suporta TTS no worker sem interromper música", "state": st.public()}
        timeout = max(1.0, min(90.0, float(body.get("timeout_seconds") or 30.0)))
        started = time.monotonic()
        engine = "worker"
        st.ducked = True
        st.updated_at = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="music-agent-tts-") as tmp:
                path = Path(tmp) / "tts.mp3"
                audio_url = str(body.get("audio_url") or body.get("url") or "").strip()
                audio_b64 = str(body.get("audio_b64") or body.get("audioBase64") or "").strip()
                tts_input = ""
                if audio_url.startswith(("http://", "https://", "file://")):
                    tts_input = audio_url
                    engine = str(body.get("engine") or "prebuilt-url").strip() or "prebuilt-url"
                elif audio_b64:
                    try:
                        raw = base64.b64decode(audio_b64.encode("ascii"), validate=True)
                    except Exception as exc:
                        raise ValueError(f"audio_b64 inválido: {type(exc).__name__}") from exc
                    max_bytes = max(1024, int(env_float("MUSIC_AGENT_TTS_MAX_B64_BYTES", 8 * 1024 * 1024)))
                    if len(raw) > max_bytes:
                        raise ValueError("áudio TTS grande demais para o Music Agent")
                    path.write_bytes(raw)
                    tts_input = str(path)
                    engine = str(body.get("engine") or "prebuilt-b64").strip() or "prebuilt-b64"
                else:
                    engine = await asyncio.wait_for(self._synthesize_tts_file(body, path), timeout=max(3.0, timeout * 0.75))
                    if not path.exists() or path.stat().st_size <= 0:
                        raise RuntimeError("TTS não gerou áudio")
                    tts_input = str(path)
                tts_source = discord.FFmpegPCMAudio(tts_input, executable=self.ffmpeg_executable, before_options="-nostdin", options="-vn -sn -dn -loglevel warning")
                future = source.add_tts(tts_source, volume=max(0.0, min(2.0, env_float("MUSIC_AGENT_TTS_VOLUME", 1.0))))
                self.log("tts_overlay_start", guild_id=guild_id, engine=engine, chars=len(str(body.get("text") or "")), prebuilt=bool(audio_url or audio_b64))
                await asyncio.wait_for(future, timeout=timeout)
        finally:
            st.ducked = False
            st.updated_at = time.time()
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        self.log("tts_overlay_done", guild_id=guild_id, elapsed_ms=round(elapsed_ms, 1))
        return {"ok": True, "engine": engine, "playback_ms": round(elapsed_ms, 1), "state": st.public()}

    async def _play_next(self, guild_id: int) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        self._cancel_idle_disconnect(guild_id)
        if not st.queue:
            st.current = None
            st.paused = False
            self._set_status(st, "idle", event="queue_empty")
            self._schedule_idle_disconnect(guild_id)
            return
        st.current = st.queue.pop(0)
        st.paused = False
        st.transport = ""
        st.ducked = False
        st.normal_volume_percent = max(0, min(150, int(st.normal_volume_percent or self.default_volume_percent)))
        st.volume_percent = st.normal_volume_percent
        self._set_status(st, "preparing", event="play_preparing")
        self.log("track_loading", guild_id=guild_id, title=getattr(st.current, "title", ""), source=getattr(st.current, "source", ""))
        try:
            use_direct = self._should_use_direct_voice(st.current)
            if use_direct:
                await asyncio.wait_for(self._play_direct_voice(guild_id, st.current), timeout=max(5.0, self.prepare_timeout))
            else:
                await asyncio.wait_for(self._play_lavalink(guild_id, st.current), timeout=max(5.0, self.prepare_timeout))
        except Exception as exc:
            self._set_status(st, "failed", event="play_failed", error=f"{type(exc).__name__}: {short_text(exc, 260)}")
            self.log("play_failed", guild_id=guild_id, transport=st.transport or "unknown", error=st.last_error)

    def _should_use_direct_voice(self, track: AgentTrack) -> bool:
        if not self.direct_audio_enabled or self.lavalink_for_direct_streams:
            return False
        raw = " ".join([track.source, track.query, track.webpage_url, track.stream_url, track.transport_hint]).lower()
        if raw.startswith(_LAVALINK_PREFIXES) or any(prefix in raw for prefix in ("spotify.com", "soundcloud.com", "scsearch:", "spsearch:")):
            return False
        if track.stream_url and self.direct_youtube_enabled:
            if any(marker in raw for marker in ("youtube", "youtu.be", "googlevideo", "yt-dlp", "ytdlp", "music-agent-ytdlp", "worker-ytdlp")):
                return True
        return False

    async def _resolve_guild_and_channel(self, guild_id: int, voice_channel_id: int) -> tuple[Any, Any]:
        guild = self.client.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(f"guild {guild_id} não encontrada no player remoto")
        channel = guild.get_channel(voice_channel_id) or self.client.get_channel(voice_channel_id)
        if channel is None:
            raise RuntimeError(f"canal de voz {voice_channel_id} não encontrado")
        return guild, channel

    async def _play_direct_voice(self, guild_id: int, track: AgentTrack) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if not track.stream_url:
            raise RuntimeError("track sem stream_url direto")
        guild, channel = await self._resolve_guild_and_channel(guild_id, st.voice_channel_id)
        self.log("voice_connecting", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
        existing = guild.voice_client
        if existing is not None and isinstance(existing, getattr(wavelink, "Player", ())):
            with contextlib.suppress(Exception):
                await existing.disconnect()
            existing = None
        if existing is None or not getattr(existing, "is_connected", lambda: False)():
            voice_client = await channel.connect(self_deaf=True)
        else:
            voice_client = existing
            if getattr(getattr(voice_client, "channel", None), "id", None) != st.voice_channel_id:
                await voice_client.move_to(channel)
        if getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)():
            voice_client.stop()
        source = self._build_ffmpeg_source(track.stream_url, volume_percent=st.volume_percent)
        st.player = voice_client
        st.transport = "direct"
        self._set_status(st, "starting", event="direct_player_starting")
        self.log("player_play_called", guild_id=guild_id, transport="direct", title=track.title)

        def after(error: Exception | None) -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(self._direct_after(guild_id, error), loop)

        voice_client.play(source, after=after)
        # Confirmação curta: evita segurar a UI/reação depois que discord.py já
        # iniciou o FFmpeg, mas ainda confirma que a voz realmente ficou tocando.
        confirm_delay = max(0.25, min(1.2, env_float("MUSIC_AGENT_DIRECT_CONFIRM_SECONDS", 0.55)))
        await asyncio.sleep(confirm_delay)
        if not getattr(voice_client, "is_connected", lambda: False)():
            raise RuntimeError("conectei no canal, mas a voz caiu antes do áudio")
        if not getattr(voice_client, "is_playing", lambda: False)() and not getattr(voice_client, "is_paused", lambda: False)():
            raise RuntimeError("ffmpeg iniciou, mas o áudio não ficou tocando")
        self._set_status(st, "playing", event="direct_track_start_confirmed")
        self.log("play_started", guild_id=guild_id, transport="direct", title=track.title, confirm_delay=confirm_delay)

    def _build_ffmpeg_source(self, stream_url: str, *, volume_percent: int | None = None) -> discord.AudioSource:
        volume = max(0.0, min(10.0, float(volume_percent if volume_percent is not None else self.default_volume_percent) / 100.0))
        if self.direct_pcm_volume_enabled:
            pcm = discord.FFmpegPCMAudio(
                stream_url,
                executable=self.ffmpeg_executable,
                before_options=self.ffmpeg_before_options,
                options=self.ffmpeg_options,
            )
            loop = self._loop or asyncio.get_running_loop()
            return AgentMixedAudioSource(loop=loop, music_source=pcm, music_volume=volume, duck_factor=max(0.0, min(1.0, self.duck_volume_percent / 100.0)))
        opus_cls = getattr(discord, "FFmpegOpusAudio", None)
        if opus_cls is not None:
            return opus_cls(
                stream_url,
                executable=self.ffmpeg_executable,
                before_options=self.ffmpeg_before_options,
                options=self.ffmpeg_options,
                bitrate=self.ffmpeg_bitrate,
            )
        pcm = discord.FFmpegPCMAudio(
            stream_url,
            executable=self.ffmpeg_executable,
            before_options=self.ffmpeg_before_options,
            options=self.ffmpeg_options,
        )
        return discord.PCMVolumeTransformer(pcm, volume=volume)

    async def _direct_after(self, guild_id: int, error: Exception | None) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if st.current is None and not st.queue:
            return
        played_for = time.monotonic() - float(st.started_monotonic or 0.0) if st.started_monotonic else 0.0
        min_ok = max(0.5, env_float("MUSIC_AGENT_EARLY_END_SECONDS", 2.5))
        if error:
            self._set_status(st, "failed", event="direct_after_error", error=f"{type(error).__name__}: {short_text(error, 260)}")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error)
            if st.queue:
                await self._play_next(guild_id)
            return
        if played_for < min_ok and st.current is not None:
            self._set_status(st, "failed", event="direct_after_early_end", error=f"áudio encerrou cedo demais ({played_for:.1f}s)")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error, title=getattr(st.current, "title", ""))
            if st.queue:
                await self._play_next(guild_id)
            return
        self.log("play_ended", guild_id=guild_id, transport="direct", title=getattr(st.current, "title", ""))
        await self._finish_current(guild_id, error=None, event="direct_track_end")

    async def _play_lavalink(self, guild_id: int, track: AgentTrack) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        await self.ensure_lavalink_pool()
        guild, channel = await self._resolve_guild_and_channel(guild_id, st.voice_channel_id)
        player_cls = getattr(wavelink, "Player", None)
        if player_cls is None:
            raise RuntimeError("Wavelink não expõe Player")
        existing = guild.voice_client
        if existing is not None and not isinstance(existing, player_cls):
            with contextlib.suppress(Exception):
                if getattr(existing, "is_playing", lambda: False)() or getattr(existing, "is_paused", lambda: False)():
                    existing.stop()
                await existing.disconnect(force=True)
            existing = None
        self.log("voice_connecting", guild_id=guild_id, channel=st.voice_channel_id, transport="lavalink")
        player = existing
        if player is None or not isinstance(player, player_cls) or not getattr(player, "connected", False):
            player = await channel.connect(cls=player_cls, self_deaf=True)
        elif getattr(getattr(player, "channel", None), "id", None) != st.voice_channel_id:
            await player.move_to(channel)
        st.player = player
        st.transport = "lavalink"
        with contextlib.suppress(Exception):
            maybe = player.set_volume(max(0, min(150, int(st.volume_percent or self.default_volume_percent))))
            if asyncio.iscoroutine(maybe):
                await maybe
        playable = await self._playable_for_track(track)
        self.log("player_play_called", guild_id=guild_id, transport="lavalink", title=track.title)
        self._set_status(st, "starting", event="lavalink_player_play_called")
        await player.play(playable)
        await asyncio.sleep(0.25)
        # Não marque como tocando só porque o comando foi despachado.
        # O estado definitivo vem do evento TrackStart do Wavelink/Lavalink.
        self._set_status(st, "starting", event="lavalink_play_dispatched")
        self.log("play_dispatched", guild_id=guild_id, transport="lavalink", title=track.title)

    async def _finish_current(self, guild_id: int, *, error: str | None, event: str) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if error:
            self._set_status(st, "failed", event=event, error=error)
            return
        st.current = None
        st.paused = False
        if st.queue:
            await self._play_next(guild_id)
            return
        self._set_status(st, "idle", event=event)
        # Fim normal de fila não é desconexão externa: mantenha a sessão de voz
        # viva e deixe o mesmo timeout AFK/idle decidir quando sair da call.
        self._schedule_idle_disconnect(guild_id)

    async def _playable_for_track(self, track: AgentTrack) -> Any:
        identifier = track.stream_url or track.webpage_url or track.query
        if not identifier:
            raise RuntimeError("track sem URL tocável")
        self.log("track_loading", title=track.title, transport="lavalink", identifier=identifier[:80])
        search = await wavelink.Playable.search(identifier)
        if isinstance(search, list):
            if not search:
                raise RuntimeError("Lavalink não retornou playable")
            return search[0]
        tracks = getattr(search, "tracks", None)
        if tracks:
            return tracks[0]
        if hasattr(search, "__iter__"):
            items = list(search)
            if items:
                return items[0]
        raise RuntimeError("Lavalink não retornou playable")

    async def resolve_track(self, query: str, *, track_meta: dict[str, Any], body: dict[str, Any]) -> AgentTrack:
        direct = str(track_meta.get("stream_url") or body.get("stream_url") or "").strip()
        title_hint = _metadata_text(track_meta.get("title") or body.get("title"), limit=160)
        requester_id = safe_id(body.get("requester_id") or track_meta.get("requester_id"))
        requester_name = short_text(body.get("requester_name") or track_meta.get("requester_name"), 80)
        source = short_text(track_meta.get("source") or body.get("source") or "worker-agent", 80)
        webpage_url = str(track_meta.get("webpage_url") or track_meta.get("original_url") or query).strip()
        if direct.startswith(("http://", "https://")):
            return AgentTrack(
                title=title_hint or short_text(query, 160) or "Música",
                requester_id=requester_id,
                requester_name=requester_name,
                query=query,
                webpage_url=webpage_url,
                stream_url=direct,
                duration=_float_or_none(track_meta.get("duration")),
                uploader=_metadata_text(track_meta.get("uploader"), limit=120),
                thumbnail=short_text(track_meta.get("thumbnail"), 500),
                source=source or "worker-ytdlp",
                transport_hint="direct",
            )
        resolved = await asyncio.to_thread(self._resolve_with_ytdlp, query)
        resolved_title = _metadata_text(resolved.get("title"), limit=160)
        resolved_uploader = _metadata_text(resolved.get("uploader"), limit=120)
        meta_uploader = _metadata_text(track_meta.get("uploader"), limit=120)
        meta_duration = _float_or_none(track_meta.get("duration"))
        resolved_duration = _float_or_none(resolved.get("duration"))
        return AgentTrack(
            title=title_hint or resolved_title or short_text(query, 160) or "Música",
            requester_id=requester_id,
            requester_name=requester_name,
            query=query,
            webpage_url=str(resolved.get("webpage_url") or webpage_url or query),
            stream_url=str(resolved.get("stream_url") or ""),
            duration=meta_duration if meta_duration is not None else resolved_duration,
            uploader=meta_uploader or resolved_uploader,
            thumbnail=short_text(track_meta.get("thumbnail") or resolved.get("thumbnail"), 500),
            source="music-agent-ytdlp",
            transport_hint="direct",
        )

    def _resolve_with_ytdlp(self, query: str) -> dict[str, Any]:
        target = query
        lowered = query.lower().strip()
        if not _looks_like_url(query) and not lowered.startswith(_LOCAL_SEARCH_PREFIXES):
            target = f"{self.default_search.rstrip(':')}:{query}"
        base_cmd = [shutil.which("python") or "python", "-m", "yt_dlp"]
        cookies = Path(self.cookies_file).expanduser()
        if cookies.exists() and cookies.stat().st_size > 0:
            base_cmd += ["--cookies", str(cookies)]
        if self.js_runtimes:
            base_cmd += ["--js-runtimes", self.js_runtimes]
        base_cmd += ["--no-playlist", "--no-warnings", "--socket-timeout", "12"]
        self.log("yt_dlp_resolve", query=query, target=target, js=self.js_runtimes)
        if _looks_like_url(query):
            fast_cmd = base_cmd + [
                "-f", self.ytdlp_format,
                "--print", "__title__:%(title)s",
                "--print", "__uploader__:%(uploader,channel,creator)s",
                "--print", "__duration__:%(duration)s",
                "--print", "__thumbnail__:%(thumbnail)s",
                "--print", "__webpage_url__:%(webpage_url,original_url)s",
                "-g", target,
            ]
            fast_started = time.time()
            fast = subprocess.run(fast_cmd, cwd=str(Path.home() / "phone-worker"), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(5, min(self.ytdlp_timeout, 18)))
            lines = [line.strip() for line in (fast.stdout or "").splitlines() if line.strip()]
            urls = [line for line in lines if line.startswith(("http://", "https://")) and not line.startswith(("https://i.ytimg.com", "http://i.ytimg.com"))]
            def marker(name: str) -> str:
                prefix = f"__{name}__:"
                return next((line.split(":", 1)[1].strip() for line in lines if line.startswith(prefix)), "")
            title_hint = marker("title")
            uploader_hint = marker("uploader")
            duration_hint = marker("duration")
            thumbnail_hint = marker("thumbnail")
            webpage_hint = marker("webpage_url")
            if fast.returncode == 0 and urls:
                self.log(
                    "yt_dlp_fast_url_ok",
                    elapsed_ms=round((time.time() - fast_started) * 1000.0, 1),
                    titled=bool(title_hint),
                    uploader=bool(uploader_hint),
                    duration=bool(duration_hint),
                )
                return {
                    "title": title_hint or query,
                    "uploader": uploader_hint,
                    "duration": _duration_from_ytdlp(duration_hint),
                    "thumbnail": thumbnail_hint,
                    "webpage_url": webpage_hint or query,
                    "stream_url": urls[0],
                }
            self.log("yt_dlp_fast_url_fallback", rc=fast.returncode, error=short_text(fast.stderr, 160))
        cmd = base_cmd + ["-f", self.ytdlp_format, "-J", target]
        proc = subprocess.run(cmd, cwd=str(Path.home() / "phone-worker"), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.ytdlp_timeout)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(short_text(proc.stderr or f"yt-dlp rc={proc.returncode}", 300))
        data = json.loads(proc.stdout or "{}")
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            data = next((item for item in data.get("entries") or [] if isinstance(item, dict)), {})
        if not isinstance(data, dict) or not data:
            raise RuntimeError("yt-dlp não retornou mídia")
        stream_url = _select_stream_url(data)
        if not stream_url:
            raise RuntimeError("yt-dlp não retornou stream_url")
        return {
            "title": data.get("title") or data.get("fulltitle") or query,
            "uploader": data.get("uploader") or data.get("channel") or data.get("creator") or "",
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail") or "",
            "webpage_url": data.get("webpage_url") or data.get("original_url") or query,
            "stream_url": stream_url,
        }

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self.log("api_ready", url=f"http://{self.host}:{self.port}", token="sim" if self.token else "não")
        if not self.discord_token:
            raise RuntimeError("defina MUSIC_AGENT_BOT_TOKEN, DISCORD_TOKEN ou BOT_TOKEN no worker")
        await self.client.start(self.discord_token)


async def amain() -> None:
    agent = MusicAgent()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(agent.run())
    done, pending = await asyncio.wait({task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set():
        await agent.client.close()
    for item in pending:
        item.cancel()
    for item in done:
        exc = item.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    asyncio.run(amain())
