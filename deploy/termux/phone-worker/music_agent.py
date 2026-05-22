#!/usr/bin/env python3
"""Core Music Agent for the phone worker.

Experimental same-bot music plane: the VPS remains the UI/control plane while
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

AGENT_VERSION = "0.1.0"
STARTED_AT = time.time()


def truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
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
    updated_at: float = field(default_factory=time.time)
    player: Any = None

    def public(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "voice_channel_id": self.voice_channel_id,
            "text_channel_id": self.text_channel_id,
            "status": self.status,
            "paused": self.paused,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
            "current": self.current.public() if self.current else None,
            "queue_size": len(self.queue),
            "queue": [item.public() for item in self.queue[:10]],
        }


class MusicAgent:
    def __init__(self) -> None:
        self.host = os.getenv("MUSIC_AGENT_HOST", "127.0.0.1")
        self.port = env_int("MUSIC_AGENT_PORT", 8786)
        self.token = os.getenv("MUSIC_AGENT_TOKEN") or os.getenv("PHONE_WORKER_TOKEN") or ""
        self.discord_token = os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
        self.lavalink_uri = os.getenv("MUSIC_AGENT_LAVALINK_URI") or os.getenv("LAVALINK_URI") or "http://127.0.0.1:2333"
        self.lavalink_password = os.getenv("MUSIC_AGENT_LAVALINK_PASSWORD") or os.getenv("LAVALINK_PASSWORD") or read_lavalink_password_from_yaml()
        self.lavalink_node_name = os.getenv("MUSIC_AGENT_LAVALINK_NODE_NAME", "phone-agent")
        self.ytdlp_format = os.getenv("MUSIC_AGENT_YTDLP_FORMAT") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_FORMAT") or "bestaudio/best"
        self.ytdlp_timeout = env_int("MUSIC_AGENT_YTDLP_TIMEOUT_SECONDS", 35)
        self.cookies_file = os.getenv("MUSIC_AGENT_YTDLP_COOKIES_FILE") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE") or str(Path.home() / "phone-worker" / "secrets" / "youtube-cookies.txt")
        self.js_runtimes = os.getenv("MUSIC_AGENT_YTDLP_JS_RUNTIMES") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES") or "node"
        self.default_search = os.getenv("MUSIC_AGENT_YTDLP_DEFAULT_SEARCH") or "ytsearch1"
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        self.states: dict[int, GuildMusicState] = {}
        self._pool_connected = False
        self._app = web.Application()
        self._app.add_routes([
            web.get("/health", self.handle_health),
            web.post("/command", self.handle_command),
        ])
        self._wire_discord_events()

    def _wire_discord_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:  # type: ignore[no-untyped-def]
            print(f"[music-agent] online como {self.client.user} versão={AGENT_VERSION}", flush=True)
            await self.ensure_lavalink_pool()

        @self.client.event
        async def on_wavelink_track_start(payload: Any) -> None:  # type: ignore[no-untyped-def]
            guild_id = safe_id(getattr(getattr(payload, "player", None), "guild", None).id if getattr(payload, "player", None) else 0)
            if guild_id:
                st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                st.status = "playing"
                st.paused = False
                st.updated_at = time.time()

        @self.client.event
        async def on_wavelink_track_end(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                await self._play_next(guild_id)

        @self.client.event
        async def on_wavelink_track_exception(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                st.status = "error"
                st.last_error = short_text(getattr(payload, "exception", "erro no Lavalink"), 240)
                st.updated_at = time.time()
                await self._play_next(guild_id)

    def _auth_ok(self, request: web.Request) -> bool:
        if not self.token:
            return True
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}" or request.headers.get("X-Music-Agent-Token") == self.token

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
            print(f"[music-agent] command_error action={body.get('action')!r} erro={type(exc).__name__}: {exc}", flush=True)
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {short_text(exc, 300)}", "status": self.status_payload()}, status=400)

    def status_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": AGENT_VERSION,
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "discord_ready": bool(self.client.is_ready()),
            "user": str(self.client.user) if self.client.user else "",
            "lavalink_uri": self.lavalink_uri,
            "lavalink_node": self.lavalink_node_name,
            "pool_connected": self._pool_connected,
            "guilds": {str(gid): state.public() for gid, state in self.states.items()},
        }

    async def ensure_lavalink_pool(self) -> None:
        if self._pool_connected:
            return
        if not self.lavalink_uri or not self.lavalink_password:
            raise RuntimeError("Lavalink do worker não configurado para o Music Agent")
        node = wavelink.Node(uri=self.lavalink_uri, password=self.lavalink_password, identifier=self.lavalink_node_name)
        try:
            await wavelink.Pool.connect(nodes=[node], client=self.client, cache_capacity=100)
        except TypeError:
            await wavelink.Pool.connect(nodes=[node], client=self.client)
        self._pool_connected = True

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
        track = await self.resolve_track(query, track_meta=track_meta, body=body)
        if st.current and st.status in {"playing", "starting", "paused"}:
            st.queue.append(track)
            st.updated_at = time.time()
            return {"ok": True, "queued": True, "state": st.public()}
        st.queue.append(track)
        await self._play_next(guild_id)
        return {"ok": True, "queued": False, "state": st.public()}

    async def cmd_pause(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.states.setdefault(safe_id(body.get("guild_id")), GuildMusicState(guild_id=safe_id(body.get("guild_id"))))
        player = st.player
        if player:
            await player.pause(True)
            st.paused = True
            st.status = "paused"
            st.updated_at = time.time()
        return {"ok": True, "state": st.public()}

    async def cmd_resume(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.states.setdefault(safe_id(body.get("guild_id")), GuildMusicState(guild_id=safe_id(body.get("guild_id"))))
        player = st.player
        if player:
            await player.pause(False)
            st.paused = False
            st.status = "playing"
            st.updated_at = time.time()
        return {"ok": True, "state": st.public()}

    async def cmd_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.queue.clear()
        st.current = None
        st.status = "idle"
        st.paused = False
        st.updated_at = time.time()
        if st.player:
            with contextlib.suppress(Exception):
                await st.player.stop()
            with contextlib.suppress(Exception):
                await st.player.disconnect()
        return {"ok": True, "state": st.public()}

    async def cmd_skip(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if st.player:
            with contextlib.suppress(Exception):
                await st.player.stop()
        await self._play_next(guild_id)
        return {"ok": True, "state": st.public()}

    async def cmd_volume(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        volume = max(0, min(1000, int(float(body.get("volume") or body.get("volume_percent") or 55))))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if st.player:
            setter = getattr(st.player, "set_volume", None)
            if callable(setter):
                await setter(volume)
        return {"ok": True, "volume": volume, "state": st.public()}

    async def _play_next(self, guild_id: int) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if not st.queue:
            st.current = None
            st.status = "idle"
            st.paused = False
            st.updated_at = time.time()
            return
        st.current = st.queue.pop(0)
        st.status = "starting"
        st.paused = False
        st.last_error = ""
        st.updated_at = time.time()
        try:
            await self.ensure_lavalink_pool()
            guild = self.client.get_guild(guild_id)
            if guild is None:
                raise RuntimeError(f"guild {guild_id} não encontrada no Music Agent")
            channel = guild.get_channel(st.voice_channel_id)
            if channel is None:
                channel = self.client.get_channel(st.voice_channel_id)
            if channel is None:
                raise RuntimeError(f"canal de voz {st.voice_channel_id} não encontrado")
            player_cls = getattr(wavelink, "Player", None)
            if player_cls is None:
                raise RuntimeError("Wavelink não expõe Player")
            player = guild.voice_client
            if player is None or not isinstance(player, player_cls) or not getattr(player, "connected", False):
                player = await channel.connect(cls=player_cls, self_deaf=True)
            elif getattr(getattr(player, "channel", None), "id", None) != st.voice_channel_id:
                await player.move_to(channel)
            st.player = player
            playable = await self._playable_for_track(st.current)
            await player.play(playable)
            st.status = "playing"
            st.updated_at = time.time()
            print(f"[music-agent] playing guild={guild_id} title={st.current.title!r}", flush=True)
        except Exception as exc:
            st.status = "error"
            st.last_error = f"{type(exc).__name__}: {short_text(exc, 260)}"
            st.updated_at = time.time()
            print(f"[music-agent] playback_error guild={guild_id} erro={st.last_error}", flush=True)

    async def _playable_for_track(self, track: AgentTrack) -> Any:
        identifier = track.stream_url or track.webpage_url or track.query
        if not identifier:
            raise RuntimeError("track sem URL tocável")
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
        # Existing direct stream from VPS/worker is accepted, but normal YouTube/text
        # should be resolved here so cookies/EJS stay phone-local.
        direct = str(track_meta.get("stream_url") or body.get("stream_url") or "").strip()
        title = short_text(track_meta.get("title") or body.get("title") or query, 160) or "Música"
        requester_id = safe_id(body.get("requester_id") or track_meta.get("requester_id"))
        requester_name = short_text(body.get("requester_name") or track_meta.get("requester_name"), 80)
        if direct.startswith(("http://", "https://")):
            return AgentTrack(title=title, requester_id=requester_id, requester_name=requester_name, query=query, webpage_url=str(track_meta.get("webpage_url") or query), stream_url=direct, duration=_float_or_none(track_meta.get("duration")), uploader=short_text(track_meta.get("uploader"), 120), thumbnail=short_text(track_meta.get("thumbnail"), 500))
        resolved = await asyncio.to_thread(self._resolve_with_ytdlp, query)
        return AgentTrack(
            title=short_text(track_meta.get("title") or resolved.get("title") or query, 160),
            requester_id=requester_id,
            requester_name=requester_name,
            query=query,
            webpage_url=str(resolved.get("webpage_url") or track_meta.get("webpage_url") or query),
            stream_url=str(resolved.get("stream_url") or ""),
            duration=_float_or_none(track_meta.get("duration") if track_meta.get("duration") is not None else resolved.get("duration")),
            uploader=short_text(track_meta.get("uploader") or resolved.get("uploader"), 120),
            thumbnail=short_text(track_meta.get("thumbnail") or resolved.get("thumbnail"), 500),
            source="music-agent-ytdlp",
        )

    def _resolve_with_ytdlp(self, query: str) -> dict[str, Any]:
        target = query
        if not _looks_like_url(query) and not query.lower().startswith(("ytsearch", "ytmsearch")):
            target = f"{self.default_search.rstrip(':')}:{query}"
        cmd = [shutil.which("python") or "python", "-m", "yt_dlp"]
        cookies = Path(self.cookies_file).expanduser()
        if cookies.exists() and cookies.stat().st_size > 0:
            cmd += ["--cookies", str(cookies)]
        if self.js_runtimes:
            cmd += ["--js-runtimes", self.js_runtimes]
        cmd += ["--no-playlist", "--no-warnings", "--socket-timeout", "20", "-f", self.ytdlp_format, "-J", target]
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
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        print(f"[music-agent] API em http://{self.host}:{self.port}; token={'sim' if self.token else 'não'}", flush=True)
        if not self.discord_token:
            raise RuntimeError("defina MUSIC_AGENT_BOT_TOKEN, DISCORD_TOKEN ou BOT_TOKEN no worker")
        await self.client.start(self.discord_token)


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
