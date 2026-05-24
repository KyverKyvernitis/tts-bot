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
import os
import re
import shutil
import signal
import subprocess
import time
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

AGENT_VERSION = "0.3.2"
STARTED_AT = time.time()


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
            "confirmed_playing": bool(self.status == "playing" and player is not None and voice_connected),
            "updated_at": self.updated_at,
            "current": self.current.public() if self.current else None,
            "queue_size": len(self.queue),
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
        self.default_search = os.getenv("MUSIC_AGENT_YTDLP_DEFAULT_SEARCH") or "ytsearch1"
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
        self.prepare_timeout = env_float("MUSIC_AGENT_PREPARING_TIMEOUT_SECONDS", 30.0)
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
        st.last_action = "play"
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

    async def cmd_volume(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        volume = max(0, min(1000, int(float(body.get("volume") or body.get("volume_percent") or 55))))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if st.player:
            setter = getattr(st.player, "set_volume", None)
            if callable(setter):
                maybe = setter(volume)
                if asyncio.iscoroutine(maybe):
                    await maybe
        return {"ok": True, "volume": volume, "state": st.public()}

    async def _play_next(self, guild_id: int) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if not st.queue:
            st.current = None
            st.transport = ""
            st.paused = False
            self._set_status(st, "idle", event="queue_empty")
            return
        st.current = st.queue.pop(0)
        st.paused = False
        st.transport = ""
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
        source = self._build_ffmpeg_source(track.stream_url)
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
        await asyncio.sleep(0.25)
        if not getattr(voice_client, "is_playing", lambda: False)() and not getattr(voice_client, "is_paused", lambda: False)():
            raise RuntimeError("ffmpeg iniciou, mas o áudio não ficou tocando")
        self._set_status(st, "playing", event="direct_track_start")
        self.log("play_started", guild_id=guild_id, transport="direct", title=track.title)

    def _build_ffmpeg_source(self, stream_url: str) -> discord.AudioSource:
        opus_cls = getattr(discord, "FFmpegOpusAudio", None)
        if opus_cls is not None:
            return opus_cls(
                stream_url,
                executable=self.ffmpeg_executable,
                before_options=self.ffmpeg_before_options,
                options=self.ffmpeg_options,
                bitrate=self.ffmpeg_bitrate,
            )
        return discord.FFmpegPCMAudio(
            stream_url,
            executable=self.ffmpeg_executable,
            before_options=self.ffmpeg_before_options,
            options=self.ffmpeg_options,
        )

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
        player = st.player
        st.player = None
        if player and not isinstance(player, getattr(wavelink, "Player", ())):
            with contextlib.suppress(Exception):
                await player.disconnect(force=True)

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
        title = short_text(track_meta.get("title") or body.get("title") or query, 160) or "Música"
        requester_id = safe_id(body.get("requester_id") or track_meta.get("requester_id"))
        requester_name = short_text(body.get("requester_name") or track_meta.get("requester_name"), 80)
        source = short_text(track_meta.get("source") or body.get("source") or "worker-agent", 80)
        webpage_url = str(track_meta.get("webpage_url") or track_meta.get("original_url") or query).strip()
        if direct.startswith(("http://", "https://")):
            return AgentTrack(
                title=title,
                requester_id=requester_id,
                requester_name=requester_name,
                query=query,
                webpage_url=webpage_url,
                stream_url=direct,
                duration=_float_or_none(track_meta.get("duration")),
                uploader=short_text(track_meta.get("uploader"), 120),
                thumbnail=short_text(track_meta.get("thumbnail"), 500),
                source=source or "worker-ytdlp",
                transport_hint="direct",
            )
        resolved = await asyncio.to_thread(self._resolve_with_ytdlp, query)
        return AgentTrack(
            title=short_text(track_meta.get("title") or resolved.get("title") or query, 160),
            requester_id=requester_id,
            requester_name=requester_name,
            query=query,
            webpage_url=str(resolved.get("webpage_url") or webpage_url or query),
            stream_url=str(resolved.get("stream_url") or ""),
            duration=_float_or_none(track_meta.get("duration") if track_meta.get("duration") is not None else resolved.get("duration")),
            uploader=short_text(track_meta.get("uploader") or resolved.get("uploader"), 120),
            thumbnail=short_text(track_meta.get("thumbnail") or resolved.get("thumbnail"), 500),
            source="music-agent-ytdlp",
            transport_hint="direct",
        )

    def _resolve_with_ytdlp(self, query: str) -> dict[str, Any]:
        target = query
        lowered = query.lower().strip()
        if not _looks_like_url(query) and not lowered.startswith(_LOCAL_SEARCH_PREFIXES):
            target = f"{self.default_search.rstrip(':')}:{query}"
        cmd = [shutil.which("python") or "python", "-m", "yt_dlp"]
        cookies = Path(self.cookies_file).expanduser()
        if cookies.exists() and cookies.stat().st_size > 0:
            cmd += ["--cookies", str(cookies)]
        if self.js_runtimes:
            cmd += ["--js-runtimes", self.js_runtimes]
        cmd += ["--no-playlist", "--no-warnings", "--socket-timeout", "20", "-f", self.ytdlp_format, "-J", target]
        self.log("yt_dlp_resolve", query=query, target=target, js=self.js_runtimes)
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
