"""Síntese, cache e overlay TTS do Music Agent."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib
import io
import os
import queue
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import discord

from .configuracao import env_float, env_int, truthy
from .estado import GuildMusicState
from .mixer_pcm import AgentMixedAudioSource
from .utilitarios import safe_id, short_text

class _TimedTTSSource(discord.AudioSource):
    def __init__(self, source, *, started, reader=None, buffered=False):
        self.source, self.started, self.reader = source, started, reader
        self.first_frame_ms = None
        self.closed = False
        self.error = None
        self.finished = False
        self.frames = queue.Queue(maxsize=25) if buffered else None
        self.ready = threading.Event()
        if buffered:
            threading.Thread(target=self._fill, name='tts-pcm-reader', daemon=True).start()

    def _fill(self):
        try:
            while not self.closed:
                frame = self.source.read()
                while not self.closed:
                    try:
                        self.frames.put(frame, timeout=.1)
                        self.ready.set()
                        break
                    except queue.Full:
                        continue
                if not frame:
                    break
        except Exception as error:
            self.error = error
            self.ready.set()
        finally:
            self.finished = True
            self.ready.set()

    def read(self):
        if self.closed:
            return b''
        if self.frames is None:
            frame = self.source.read()
        else:
            try:
                frame = self.frames.get_nowait()
            except queue.Empty:
                if self.error:
                    raise self.error
                if self.finished:
                    return b''
                return bytes(3840)  # keep the music clock running during provider stalls
        if not frame and self.reader is not None and self.reader.error is not None:
            raise RuntimeError('síntese progressiva incompleta') from self.reader.error
        if frame and self.first_frame_ms is None:
            self.first_frame_ms = (time.monotonic() - self.started) * 1000
        return frame

    def is_opus(self):
        return self.source.is_opus()

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        if self.reader is not None:
            self.reader.close()
        self.source.cleanup()


def _schedule_tts_prune(callback, path):
    global _TTS_MAINTENANCE_PENDING, _TTS_MAINTENANCE_RUNNING
    with _TTS_MAINTENANCE_LOCK:
        _TTS_MAINTENANCE_PENDING = callback, path
        if _TTS_MAINTENANCE_RUNNING:
            return
        _TTS_MAINTENANCE_RUNNING = True
    def run():
        global _TTS_MAINTENANCE_PENDING, _TTS_MAINTENANCE_RUNNING
        while True:
            with _TTS_MAINTENANCE_LOCK:
                current = _TTS_MAINTENANCE_PENDING
                _TTS_MAINTENANCE_PENDING = None
                if current is None:
                    _TTS_MAINTENANCE_RUNNING = False
                    return
            with contextlib.suppress(Exception):
                current[0](protected=current[1])
    try:
        _TTS_MAINTENANCE_POOL.submit(run)
    except RuntimeError:
        with _TTS_MAINTENANCE_LOCK:
            _TTS_MAINTENANCE_RUNNING = False
            _TTS_MAINTENANCE_PENDING = None


_TTS_MAINTENANCE_LOCK = threading.Lock()
_TTS_MAINTENANCE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-cache-prune")
_TTS_MAINTENANCE_PENDING = None
_TTS_MAINTENANCE_RUNNING = False
_TTS_CACHE_TOUCHES = {}


class TTSMixin:
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

    def _tts_cache_enabled(self) -> bool:
        return truthy(os.getenv("MUSIC_AGENT_TTS_CACHE_ENABLED"), truthy(os.getenv("PHONE_WORKER_TTS_AGENT_CACHE_ENABLED"), True))

    def _tts_cache_root(self) -> Path:
        configured = str(os.getenv("MUSIC_AGENT_TTS_CACHE_DIR") or os.getenv("PHONE_WORKER_TTS_CACHE_DIR") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "phone-worker" / "cache" / "tts"

    def _tts_cache_limits(self) -> tuple[int, int]:
        max_mb = max(16, min(32768, env_int("MUSIC_AGENT_TTS_CACHE_MAX_MB", env_int("PHONE_WORKER_TTS_CACHE_MAX_MB", 4096))))
        max_files = max(64, min(100000, env_int("MUSIC_AGENT_TTS_CACHE_MAX_FILES", env_int("PHONE_WORKER_TTS_CACHE_MAX_FILES", 20000))))
        return max_mb * 1024 * 1024, max_files

    def _sanitize_tts_cache_key(self, raw: Any) -> str:
        key = re.sub(r"[^a-z0-9_\-]", "", str(raw or "").strip().lower())
        if len(key) < 16:
            raise ValueError("cache_key curta")
        return key[:96]

    def _tts_cache_path(self, key: str, audio_format: str) -> Path:
        fmt = str(audio_format or "mp3").strip().lower().replace(".", "")
        if fmt in {"wave", "wav"}:
            fmt = "wav"
        elif fmt in {"ogg", "opus"}:
            fmt = "ogg"
        else:
            fmt = "mp3"
        return self._tts_cache_root() / f"{key}.{fmt}"

    def _find_tts_cache_file(self, key: str) -> tuple[Path | None, str]:
        root = self._tts_cache_root()
        for fmt in ("mp3", "wav", "ogg"):
            path = root / f"{key}.{fmt}"
            try:
                if path.exists() and path.stat().st_size > 0:
                    return path, fmt
            except Exception:
                continue
        return None, ""

    def _touch_tts_cache_file(self, path: Path) -> None:
        now = time.monotonic()
        key = str(path)
        with _TTS_MAINTENANCE_LOCK:
            if now - _TTS_CACHE_TOUCHES.get(key, -60) < 30:
                return
            if len(_TTS_CACHE_TOUCHES) >= 4096:
                _TTS_CACHE_TOUCHES.pop(next(iter(_TTS_CACHE_TOUCHES)))
            _TTS_CACHE_TOUCHES[key] = now
        with contextlib.suppress(OSError):
            os.utime(path, None)

    def _prune_tts_cache(self, *, protected: Path | None = None) -> None:
        import fcntl
        root = self._tts_cache_root()
        max_bytes, max_files = self._tts_cache_limits()
        stats = []
        with contextlib.suppress(OSError):
            with os.scandir(root) as entries:
                for entry in entries:
                    if Path(entry.name).suffix not in {'.mp3', '.wav', '.ogg'} or not entry.is_file(follow_symlinks=False):
                        continue
                    with contextlib.suppress(OSError):
                        st = entry.stat(follow_symlinks=False)
                        stats.append((st.st_mtime, st.st_size, entry.path))
        count, total = len(stats), sum(row[1] for row in stats)
        if count <= max_files and total <= max_bytes:
            return
        fresh = time.time() - 180
        for mtime, size, path in sorted(stats):
            if count <= max_files and total <= max_bytes:
                break
            if mtime > fresh or protected is not None and path == str(protected):
                continue
            try:
                with open(path, 'rb') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    before, current = os.fstat(handle.fileno()), os.stat(path)
                    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                        continue
                    os.unlink(path)
                    count -= 1
                    total -= size
            except OSError:
                continue

    def _tts_cache_key_for_body(self, body: dict[str, Any], *, engine: str, text: str) -> str:
        requested_engine = str(body.get("engine") or engine or "gtts").strip().lower().replace("-", "_") or "gtts"
        aliases = {"google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts", "edge_tts": "edge"}
        requested_engine = aliases.get(requested_engine, requested_engine)
        normalized_engine = aliases.get(str(engine or requested_engine).strip().lower().replace("-", "_"), str(engine or requested_engine).strip().lower().replace("-", "_"))
        provided = str(body.get("cache_key") or "").strip()
        if provided and requested_engine == normalized_engine:
            with contextlib.suppress(Exception):
                return self._sanitize_tts_cache_key(provided)
        normalized_text = str(text or "").strip()
        if engine == "edge":
            voice = str(body.get("voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
            payload = f"edge|{voice}|{self._normalize_edge_rate(body.get('rate'))}|{self._normalize_edge_pitch(body.get('pitch'))}|{normalized_text}"
        else:
            language = self._normalize_tts_language(body.get("language"))
            payload = f"gtts|{language}|{body.get('tld') or 'com'}|{normalized_text}"
        return hashlib.sha256(("tts-v2|" + payload).encode("utf-8")).hexdigest()

    def _tts_cache_mode_allows_read(self, body: dict[str, Any]) -> bool:
        mode = str(body.get("cache_mode") or "prefer").strip().lower()
        return mode not in {"0", "false", "off", "disabled", "none", "bypass", "refresh"}

    def _tts_cache_mode_allows_store(self, body: dict[str, Any]) -> bool:
        mode = str(body.get("cache_mode") or "prefer").strip().lower()
        return mode not in {"0", "false", "off", "disabled", "none", "bypass", "no_store"}

    def _try_read_tts_cache_to_target(self, *, key: str, target: Path, body: dict[str, Any]) -> bool:
        path, audio_format = self._find_tts_cache_file(key)
        if path is None:
            return False
        data = path.read_bytes()
        if not data:
            return False
        max_bytes = max(1024, int(env_float("MUSIC_AGENT_TTS_MAX_B64_BYTES", 8 * 1024 * 1024)))
        if len(data) > max_bytes:
            return False
        target.write_bytes(data)
        body["audio_format"] = audio_format
        body["tts_cache_hit"] = True
        self._touch_tts_cache_file(path)
        self.log("tts_cache_hit", engine=str(body.get("engine") or ""), file=path.name, bytes=len(data))
        return True

    def _store_tts_cache_bytes(self, *, key: str, data: bytes, audio_format: str, engine: str) -> None:
        if not key or not data:
            return
        path = self._tts_cache_path(key, audio_format)
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)
            self._touch_tts_cache_file(path)
            _schedule_tts_prune(self._prune_tts_cache, path)
            self.log("tts_cache_store", engine=engine, file=path.name, bytes=len(data))
        except Exception as exc:
            self.log("tts_cache_store_failed", engine=engine, error=f"{type(exc).__name__}: {short_text(exc, 120)}")
        finally:
            with contextlib.suppress(Exception):
                tmp.unlink()

    async def _synthesize_tts_file(self, body: dict[str, Any], target: Path) -> str:
        text = short_text(body.get("text") or body.get("content") or "", 1600)
        if not text:
            raise ValueError("texto TTS vazio")
        engine = str(body.get("engine") or "gtts").strip().lower().replace("-", "_")
        if engine in {"google", "google_tts", "googlecloud", "google_cloud", "gcloud"}:
            engine = "gtts"
        cache_key = ""
        if self._tts_cache_enabled():
            with contextlib.suppress(Exception):
                cache_key = self._tts_cache_key_for_body(body, engine=engine, text=text)
            if cache_key and self._tts_cache_mode_allows_read(body) and self._try_read_tts_cache_to_target(key=cache_key, target=target, body=body):
                return f"{engine}-cache"
        audio_format = "mp3"
        try:
            transport = importlib.import_module('tts_transport')
        except ImportError:
            transport = None
        if transport is not None:
            stream = transport.AudioStream(engine=engine, text=text,
                voice=str(body.get('voice') or 'pt-BR-FranciscaNeural'),
                language=self._normalize_tts_language(body.get('language')),
                rate=self._normalize_edge_rate(body.get('rate')),
                pitch=self._normalize_edge_pitch(body.get('pitch')),
                tld=str(body.get('tld') or 'com'), timeout=float(body.get('timeout_seconds') or 30))
            try:
                data = await asyncio.to_thread(lambda: b''.join(stream))
            finally:
                stream.close()
        elif engine == 'edge':
            import edge_tts
            communicate = edge_tts.Communicate(text=text, voice=str(body.get('voice') or 'pt-BR-FranciscaNeural'),
                rate=self._normalize_edge_rate(body.get('rate')), pitch=self._normalize_edge_pitch(body.get('pitch')),
                connect_timeout=4, receive_timeout=15)
            chunks = []
            async for chunk in communicate.stream():
                if chunk.get('type') == 'audio' and chunk.get('data'):
                    chunks.append(chunk['data'])
            data = b''.join(chunks)
        else:
            from gtts import gTTS
            def synthesize():
                buffer = io.BytesIO()
                gTTS(text=text, lang=self._normalize_tts_language(body.get('language')), timeout=(3.5, 8)).write_to_fp(buffer)
                return buffer.getvalue()
            data = await asyncio.to_thread(synthesize)
        if not data:
            raise RuntimeError("TTS não gerou áudio")
        target.write_bytes(data)
        body.setdefault("audio_format", audio_format)
        if cache_key and self._tts_cache_enabled() and self._tts_cache_mode_allows_store(body):
            self._store_tts_cache_bytes(key=cache_key, data=data, audio_format=str(body.get("audio_format") or audio_format), engine=engine)
        return engine

    def _tts_requests(self):
        if not hasattr(self, '_active_tts_requests'):
            self._active_tts_requests = {}
            self._cancelled_tts_requests = {}
        return self._active_tts_requests

    async def _run_tts_request(self, body, *, direct):
        active = self._tts_requests()
        now = time.monotonic()
        self._cancelled_tts_requests = {key: when for key, when in self._cancelled_tts_requests.items() if now - when < 60}
        request = str(body.get('tts_request_id') or '')[:80]
        key = (safe_id(body.get('guild_id')), request)
        if request and key in self._cancelled_tts_requests:
            return {'ok': False, 'cancelled': True, 'error': 'TTS cancelado antes da admissão'}
        if request and key in active:
            return {'ok': False, 'error': 'pedido TTS já está ativo'}
        owner = asyncio.current_task()
        if request:
            active[key] = owner
        try:
            return await (self.cmd_voice_tts(body) if direct else self.cmd_tts(body))
        finally:
            if active.get(key) is owner:
                active.pop(key, None)

    async def cmd_cancel_tts(self, body):
        active = self._tts_requests()
        request = str(body.get('tts_request_id') or '')[:80]
        if not request:
            return {'ok': False, 'error': 'tts_request_id obrigatório'}
        key = (safe_id(body.get('guild_id')), request)
        if len(self._cancelled_tts_requests) >= 256:
            self._cancelled_tts_requests.pop(next(iter(self._cancelled_tts_requests)))
        self._cancelled_tts_requests[key] = time.monotonic()
        task = active.get(key)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {'ok': True, 'cancelled': True, 'tts_request_id': request}

    async def _buffer_tts_source(self, source, *, started, timeout, reader=None):
        timed = _TimedTTSSource(source, started=started, reader=reader, buffered=True)
        try:
            ready = await asyncio.to_thread(timed.ready.wait, min(20.0, max(1.0, float(timeout))))
            if not ready or timed.error:
                raise RuntimeError('TTS não preparou o primeiro PCM') from timed.error
            return timed
        except BaseException:
            timed.cleanup()
            raise

    async def _prepare_tts_source(self, body, target, *, started):
        text = str(body.get('text') or body.get('content') or '').strip()
        if not text or len(text) > 1600:
            raise ValueError('tamanho de texto TTS inválido')
        engine = str(body.get('engine') or 'gtts').lower().replace('-', '_')
        if engine in {'google', 'google_tts', 'googlecloud', 'google_cloud', 'gcloud'}:
            engine = 'gtts'
        key = self._tts_cache_key_for_body(body, engine=engine, text=text) if self._tts_cache_enabled() else ''
        cache_hit = key and self._tts_cache_mode_allows_read(body) and await asyncio.to_thread(
            self._try_read_tts_cache_to_target, key=key, target=target, body=body)
        if cache_hit:
            source = discord.FFmpegPCMAudio(str(target), executable=self.ffmpeg_executable,
                before_options='-nostdin', options='-vn -sn -dn -loglevel warning')
            return await self._buffer_tts_source(source, started=started, timeout=body.get('timeout_seconds') or 30), f'{engine}-cache'
        try:
            transport = importlib.import_module('tts_transport')
        except ImportError:
            await self._synthesize_tts_file(body, target)
            source = discord.FFmpegPCMAudio(str(target), executable=self.ffmpeg_executable,
                before_options='-nostdin', options='-vn -sn -dn -loglevel warning')
            return await self._buffer_tts_source(source, started=started, timeout=body.get('timeout_seconds') or 30), engine

        provider_module = {'gtts': 'gtts', 'edge': 'edge_tts'}.get(engine)
        if provider_module:
            try:
                importlib.import_module(provider_module)
            except Exception as exc:
                raise RuntimeError(
                    f"provider TTS {engine} indisponível no Music Agent; envie áudio pré-sintetizado"
                ) from exc

        stream = transport.AudioStream(engine=engine, text=text,
            voice=str(body.get('voice') or 'pt-BR-FranciscaNeural'),
            language=self._normalize_tts_language(body.get('language')),
            rate=self._normalize_edge_rate(body.get('rate')), pitch=self._normalize_edge_pitch(body.get('pitch')),
            tld=str(body.get('tld') or 'com'), timeout=float(body.get('timeout_seconds') or 30))
        publish = None
        if key and self._tts_cache_mode_allows_store(body):
            publish = lambda data: self._store_tts_cache_bytes(key=key, data=data, audio_format='mp3', engine=engine)
        reader = transport.AudioReader(stream, on_complete=publish)
        timed = None
        try:
            source = discord.FFmpegPCMAudio(reader, pipe=True, executable=self.ffmpeg_executable,
                before_options='-nostdin -f mp3 -probesize 32768 -analyzeduration 0',
                options='-vn -sn -dn -loglevel warning')
            timed = await self._buffer_tts_source(source, started=started, reader=reader, timeout=body.get('timeout_seconds') or 30)
            return timed, engine
        except BaseException:
            reader.close()
            if timed is not None:
                timed.cleanup()
            raise

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
        tts_source = None
        future = None
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
                    tts_source, engine = await self._prepare_tts_source(body, path, started=started)
                if tts_source is None:
                    tts_source = await self._buffer_tts_source(
                        discord.FFmpegPCMAudio(tts_input, executable=self.ffmpeg_executable,
                            before_options="-nostdin", options="-vn -sn -dn -loglevel warning"),
                        started=started, timeout=timeout,
                    )
                future = source.add_tts(tts_source, volume=max(0.0, min(2.0, env_float("MUSIC_AGENT_TTS_VOLUME", 1.0))))
                self.log("tts_overlay_start", guild_id=guild_id, engine=engine, chars=len(str(body.get("text") or "")), prebuilt=bool(audio_url or audio_b64))
                await asyncio.wait_for(future, timeout=timeout)
        finally:
            if future is not None:
                source.cancel_tts(future)
            if tts_source is not None:
                tts_source.cleanup()
            has_tts = getattr(source, "has_tts", None)
            st.ducked = bool(has_tts()) if callable(has_tts) else False
            st.updated_at = time.time()
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        self.log("tts_overlay_done", guild_id=guild_id, elapsed_ms=round(elapsed_ms, 1))
        return {"ok": True, "engine": engine, "playback_ms": round(elapsed_ms, 1), "first_frame_observed": tts_source.first_frame_ms is not None, "first_frame_ms": tts_source.first_frame_ms, "state": st.public()}

    async def cmd_voice_tts(self, body: dict[str, Any]) -> dict[str, Any]:
        """Play a short TTS directly from the worker-owned Discord voice plane.

        This is intentionally only a voice/audio-plane command. The VPS still owns
        commands, permissions, panels and DB state. The Music Agent uses its
        existing Discord voice client solely to connect/play audio in the target
        voice channel.
        """
        guild_id = safe_id(body.get("guild_id"))
        voice_channel_id = safe_id(body.get("voice_channel_id") or body.get("channel_id"))
        if guild_id <= 0 or voice_channel_id <= 0:
            raise ValueError("guild_id/voice_channel_id obrigatórios para TTS direto")
        timeout = max(3.0, min(90.0, float(body.get("timeout_seconds") or 30.0)))
        started = time.monotonic()
        async with self._registry_lock(self._tts_direct_locks, self._tts_direct_lock_users, guild_id):
            st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
            st.voice_channel_id = voice_channel_id
            st.text_channel_id = safe_id(body.get("text_channel_id") or st.text_channel_id)
            # If worker music is currently playing through the mixed source, reuse
            # the overlay path so TTS and music do not fight over the same voice connection.
            player = st.player
            source = getattr(player, "source", None)
            if st.current is not None and isinstance(source, AgentMixedAudioSource):
                return await self.cmd_tts(body)
            if st.current is not None:
                return {"ok": False, "error": "música ativa sem mixer TTS direto; evitando interromper player", "state": st.public()}

            guild, channel = await self._resolve_guild_and_channel(guild_id, voice_channel_id)
            existing = guild.voice_client
            if existing is None or not getattr(existing, "is_connected", lambda: False)():
                self.log("voice_direct_tts_connecting", guild_id=guild_id, channel=voice_channel_id)
                voice_client = await channel.connect(self_deaf=True)
            else:
                voice_client = existing
                if getattr(getattr(voice_client, "channel", None), "id", None) != voice_channel_id:
                    await voice_client.move_to(channel)
            st.player = voice_client
            st.transport = "worker_voice_direct_tts"
            st.status = "tts_direct"
            st.updated_at = time.time()

            try:
                if getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)():
                    with contextlib.suppress(Exception):
                        voice_client.stop()
                    await asyncio.sleep(0.15)

                loop = asyncio.get_running_loop()
                finished = loop.create_future()
                def _after(error: Exception | None) -> None:
                    def complete():
                        if not finished.done():
                            finished.set_exception(error) if error else finished.set_result(None)
                    loop.call_soon_threadsafe(complete)

                engine = "worker"
                with tempfile.TemporaryDirectory(prefix="music-agent-direct-tts-") as tmp:
                    def _audio_suffix_from_format(value: Any) -> str:
                        fmt = str(value or "").strip().lower().replace(".", "").replace("-", "_")
                        if fmt in {"ogg", "opus", "ogg_opus"}:
                            return ".ogg"
                        if fmt in {"wav", "wave", "linear16", "pcm"}:
                            return ".wav"
                        if fmt == "m4a":
                            return ".m4a"
                        return ".mp3"

                    def _build_tts_audio_source(tts_input_path: str, *, audio_format: str = "") -> Any:
                        suffix = _audio_suffix_from_format(audio_format or Path(str(tts_input_path)).suffix)
                        opus_cls = getattr(discord, "FFmpegOpusAudio", None)
                        if suffix == ".ogg" and opus_cls is not None:
                            try:
                                return opus_cls(
                                    tts_input_path,
                                    executable=self.ffmpeg_executable,
                                    before_options="-nostdin",
                                    options="-vn -sn -dn -loglevel warning",
                                    codec="copy",
                                )
                            except TypeError:
                                pass
                            except Exception as exc:
                                self.log("voice_direct_tts_opus_copy_fallback", error=short_text(exc, 180))
                            try:
                                return opus_cls(
                                    tts_input_path,
                                    executable=self.ffmpeg_executable,
                                    before_options="-nostdin",
                                    options="-vn -sn -dn -loglevel warning",
                                )
                            except Exception as exc:
                                self.log("voice_direct_tts_opus_source_fallback", error=short_text(exc, 180))
                        return discord.FFmpegPCMAudio(tts_input_path, executable=self.ffmpeg_executable, before_options="-nostdin", options="-vn -sn -dn -loglevel warning")

                    audio_format = str(body.get("audio_format") or body.get("format") or "").strip().lower()
                    path = Path(tmp) / f"tts{_audio_suffix_from_format(audio_format)}"
                    audio_url = str(body.get("audio_url") or body.get("url") or "").strip()
                    audio_b64 = str(body.get("audio_b64") or body.get("audioBase64") or "").strip()
                    tts_input = ""
                    audio_source = None
                    if audio_url.startswith(("http://", "https://", "file://")):
                        tts_input = audio_url
                        engine = str(body.get("engine") or "prebuilt-url").strip() or "prebuilt-url"
                    elif audio_b64:
                        raw = base64.b64decode(audio_b64.encode("ascii"), validate=True)
                        max_bytes = max(1024, int(env_float("MUSIC_AGENT_TTS_MAX_B64_BYTES", 8 * 1024 * 1024)))
                        if len(raw) > max_bytes:
                            raise ValueError("áudio TTS grande demais para o Music Agent")
                        path.write_bytes(raw)
                        tts_input = str(path)
                        engine = str(body.get("engine") or "prebuilt-b64").strip() or "prebuilt-b64"
                    else:
                        path = Path(tmp) / "tts.mp3"
                        audio_source, engine = await self._prepare_tts_source(body, path, started=started)
                        audio_format = "mp3"
                    if audio_source is None:
                        audio_source = _TimedTTSSource(_build_tts_audio_source(tts_input, audio_format=audio_format), started=started)
                    play_started = time.monotonic()
                    try:
                        voice_client.play(audio_source, after=_after)
                        self.log("voice_direct_tts_start", guild_id=guild_id, engine=engine, channel=voice_channel_id, chars=len(str(body.get("text") or "")))
                        await asyncio.wait_for(finished, timeout=timeout)
                    except BaseException:
                        with contextlib.suppress(Exception):
                            if getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)():
                                voice_client.stop()
                        raise
                    finally:
                        with contextlib.suppress(Exception):
                            audio_source.cleanup()
                elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
                playback_ms = max(0.0, (time.monotonic() - play_started) * 1000.0)
                st.status = "idle"
                st.updated_at = time.time()
                self._schedule_idle_disconnect(guild_id)
                self.log("voice_direct_tts_done", guild_id=guild_id, engine=engine, elapsed_ms=round(elapsed_ms, 1))
                return {
                    "ok": True,
                    "engine": engine,
                    "audio_format": audio_format or "mp3",
                    "direct_tts": True,
                    "voice_connected": bool(getattr(voice_client, "is_connected", lambda: False)()),
                    "playback_ms": round(playback_ms, 1),
                    "elapsed_ms": round(elapsed_ms, 1),
                    "first_frame_observed": audio_source.first_frame_ms is not None,
                    "first_frame_ms": audio_source.first_frame_ms,
                    "state": st.public(),
                }

            finally:
                if st.player is voice_client and st.current is None and st.status == "tts_direct":
                    self._set_status(st, "idle", event="voice_direct_tts_end")
                    self._schedule_idle_disconnect(guild_id)
