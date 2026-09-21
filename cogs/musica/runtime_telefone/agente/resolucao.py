"""Resolução, cache e scheduler de yt-dlp do Music Agent."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .ciclo_vida import remove_owned_task
from .estado import AgentTrack
from .ytdlp_quente import WarmYTDLPResolver
from .utilitarios import (
    _duration_from_ytdlp,
    _float_or_none,
    _looks_like_url,
    _metadata_text,
    _select_stream_info,
    safe_id,
    short_text,
)

_LOCAL_SEARCH_PREFIXES = ("ytsearch", "ytmsearch")


class ResolucaoMixin:
    async def _acquire_resolve_slot(self, priority: int) -> None:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future | None = None
        async with self._resolve_scheduler_lock:
            if self._resolve_active < self.resolve_max_concurrency and not self._resolve_waiters:
                self._resolve_active += 1
                return
            self._resolve_waiter_sequence += 1
            waiter = loop.create_future()
            self._resolve_waiters.append((int(priority), self._resolve_waiter_sequence, waiter))
            self._resolve_waiters.sort(key=lambda item: (item[0], item[1]))
        try:
            # Shield keeps the scheduler-owned Future intact when only the
            # caller is cancelled. That lets us distinguish queued vs. already
            # granted slots and hand a granted slot forward without leaking it.
            await asyncio.shield(waiter)
        except BaseException:
            granted = False
            async with self._resolve_scheduler_lock:
                queued = any(item[2] is waiter for item in self._resolve_waiters)
                if queued:
                    self._resolve_waiters = [item for item in self._resolve_waiters if item[2] is not waiter]
                    if not waiter.done():
                        waiter.cancel()
                else:
                    granted = waiter.done() and not waiter.cancelled()
            if granted:
                await self._release_resolve_slot()
            raise

    async def _release_resolve_slot(self) -> None:
        async with self._resolve_scheduler_lock:
            while self._resolve_waiters:
                _priority, _sequence, waiter = self._resolve_waiters.pop(0)
                if waiter.done():
                    continue
                waiter.set_result(None)
                return
            self._resolve_active = max(0, self._resolve_active - 1)

    @contextlib.asynccontextmanager
    async def _resolve_slot(self, priority: int = 0):
        await self._acquire_resolve_slot(priority)
        try:
            yield
        finally:
            await self._release_resolve_slot()

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            with contextlib.suppress(Exception):
                proc.terminate()
        try:
            proc.wait(timeout=0.6)
        except Exception:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()

    def _run_ytdlp_command(self, cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        cancel_event = getattr(self._resolve_thread_local, "cancel_event", None)
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path.home() / "phone-worker"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        deadline = time.monotonic() + max(0.5, float(timeout))
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate_process_tree(proc)
                raise RuntimeError("resolução yt-dlp cancelada")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process_tree(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                stdout, stderr = proc.communicate(timeout=min(0.2, remaining))
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue

    def _resolve_cache_key(self, query: str, track_meta: dict[str, Any] | None = None) -> str:
        meta = track_meta or {}
        raw = str(meta.get("webpage_url") or meta.get("original_url") or meta.get("stream_url") or query or "").strip().lower()
        raw = re.sub(r"[?&](utm_[^=&]+|feature|si)=[^&]+", "", raw)
        return raw or str(query or "").strip().lower()

    def _cache_prune_one(self, cache: dict[str, tuple[float, dict[str, Any]]]) -> None:
        if not cache:
            return
        oldest = min(cache.items(), key=lambda item: item[1][0])[0]
        cache.pop(oldest, None)

    def _metadata_cache_get(self, key: str) -> dict[str, Any] | None:
        if self.metadata_cache_ttl <= 0 or not key:
            return None
        item = self._metadata_cache.get(key)
        if not item:
            return None
        created, data = item
        if time.monotonic() - created > self.metadata_cache_ttl:
            self._metadata_cache.pop(key, None)
            return None
        return dict(data)

    def _metadata_cache_put(self, key: str, data: dict[str, Any]) -> None:
        if self.metadata_cache_ttl <= 0 or not key or not data:
            return
        stable = dict(data)
        for volatile_key in ("stream_url", "url", "direct_url", "http_headers", "_stream_resolved_monotonic"):
            stable.pop(volatile_key, None)
        if len(self._metadata_cache) >= 512:
            self._cache_prune_one(self._metadata_cache)
        self._metadata_cache[key] = (time.monotonic(), stable)

    def _resolve_cache_get(self, key: str) -> dict[str, Any] | None:
        if self.stream_cache_ttl <= 0 or not key:
            return None
        item = self._resolve_cache.get(key)
        if not item:
            return None
        created, data = item
        if time.monotonic() - created > self.stream_cache_ttl:
            # Preserve metadata even when the playable URL expired.
            self._metadata_cache_put(key, data)
            self._resolve_cache.pop(key, None)
            return None
        return dict(data)

    def _resolve_cache_put(self, key: str, data: dict[str, Any]) -> None:
        if not key or not data:
            return
        self._metadata_cache_put(key, data)
        if self.stream_cache_ttl <= 0 or not data.get("stream_url"):
            return
        if len(self._resolve_cache) >= 128:
            self._cache_prune_one(self._resolve_cache)
        payload = dict(data)
        payload.setdefault("_stream_resolved_monotonic", time.monotonic())
        self._resolve_cache[key] = (time.monotonic(), payload)

    def _invalidate_stream_cache(self, key: str) -> None:
        if key:
            self._resolve_cache.pop(key, None)

    def _invalidate_track_stream_cache(self, track: AgentTrack | None) -> None:
        if track is None:
            return
        meta = track.public()
        key = self._resolve_cache_key(track.query or track.webpage_url or track.title, meta)
        self._invalidate_stream_cache(key)

    def _track_stream_needs_refresh(self, track: AgentTrack | None) -> bool:
        if track is None or not str(track.stream_url or "").startswith(("http://", "https://")):
            return False
        resolved_at = max(0.0, float(getattr(track, "stream_resolved_monotonic", 0.0) or 0.0))
        if resolved_at <= 0:
            # URLs externas sem timestamp conhecido continuam válidas até falhar;
            # o recovery cobre esse caso sem adivinhar a idade do link.
            return False
        return time.monotonic() - resolved_at >= self.stream_refresh_before_play_seconds

    def _agent_track_from_resolved(self, resolved: dict[str, Any], *, query: str, track_meta: dict[str, Any], body: dict[str, Any], cached: bool = False) -> AgentTrack:
        title_hint = _metadata_text(track_meta.get("title") or body.get("title"), limit=160)
        requester_id = safe_id(body.get("requester_id") or track_meta.get("requester_id"))
        requester_name = short_text(body.get("requester_name") or track_meta.get("requester_name"), 80)
        meta_uploader = _metadata_text(track_meta.get("uploader"), limit=120)
        meta_duration = _float_or_none(track_meta.get("duration"))
        source = short_text(track_meta.get("source") or body.get("source") or "worker-agent", 80)
        webpage_url = str(track_meta.get("webpage_url") or track_meta.get("original_url") or resolved.get("webpage_url") or query).strip()
        resolved_title = _metadata_text(resolved.get("title"), limit=160)
        resolved_uploader = _metadata_text(resolved.get("uploader"), limit=120)
        track = AgentTrack(
            title=title_hint or resolved_title or short_text(query, 160) or "Música",
            requester_id=requester_id,
            requester_name=requester_name,
            query=query,
            webpage_url=webpage_url,
            stream_url=str(resolved.get("stream_url") or ""),
            duration=meta_duration if meta_duration is not None else _float_or_none(resolved.get("duration")),
            uploader=meta_uploader or resolved_uploader,
            thumbnail=short_text(track_meta.get("thumbnail") or resolved.get("thumbnail"), 500),
            source=source if source and source != "worker-agent" else "music-agent-ytdlp",
            transport_hint="direct-cache" if cached else "direct",
            audio_format_id=short_text(resolved.get("audio_format_id") or resolved.get("format_id"), 40),
            audio_ext=short_text(resolved.get("audio_ext") or resolved.get("ext"), 20).lower(),
            audio_codec=short_text(resolved.get("audio_codec") or resolved.get("codec"), 40).lower(),
            audio_abr=int(float(resolved.get("audio_abr") or resolved.get("abr") or 0) or 0),
            audio_sample_rate=int(float(resolved.get("audio_sample_rate") or resolved.get("asr") or 0) or 0),
            audio_channels=int(float(resolved.get("audio_channels") or resolved.get("channels") or 0) or 0),
            start_offset_seconds=max(0.0, float(track_meta.get("start_offset_seconds") or track_meta.get("start") or body.get("position_seconds") or 0.0)),
            stream_resolved_monotonic=max(0.0, float(resolved.get("_stream_resolved_monotonic") or time.monotonic())),
        )
        return track

    def _metadata_source_kind(self, track_meta: dict[str, Any]) -> str:
        raw = " ".join(str(track_meta.get(key) or "") for key in (
            "source", "display_source", "extractor", "extractor_key", "ie_key", "webpage_url", "original_url", "query"
        )).lower()
        if "spotify" in raw:
            return "spotify"
        if "deezer" in raw:
            return "deezer"
        if "apple" in raw or "music.apple" in raw:
            return "apple"
        if "youtube" in raw or "youtu.be" in raw:
            return "youtube"
        if "soundcloud" in raw:
            return "soundcloud"
        return ""

    def _query_from_track_meta(self, track_meta: dict[str, Any] | None, *, fallback_query: Any = "") -> str:
        meta = track_meta or {}
        source_kind = self._metadata_source_kind(meta)
        raw_query = str(meta.get("query") or fallback_query or "").strip()
        direct = str(meta.get("stream_url") or meta.get("direct_url") or "").strip()
        if direct.startswith(("http://", "https://")):
            return direct
        original_url = str(meta.get("webpage_url") or meta.get("original_url") or "").strip()
        url_is_metadata_only = source_kind in {"spotify", "deezer", "apple"}
        if original_url.startswith(("http://", "https://")) and not url_is_metadata_only:
            return original_url
        # O VPS já pode enviar a consulta interna fechada em ytsearch1. Não a
        # reconstrua no Agent: além de preservar o contrato direct-play, isso
        # impede que MUSIC_AGENT_YTDLP_DEFAULT_SEARCH=ytsearch3 multiplique
        # candidatos para uma faixa cuja identidade já é conhecida.
        if raw_query.lower().startswith(("ytsearch", "ytmsearch")):
            return raw_query
        if raw_query.startswith(("http://", "https://")) and not any(marker in raw_query.lower() for marker in ("spotify.com", "deezer.com", "music.apple.com")):
            return raw_query
        title = _metadata_text(meta.get("display_title") or meta.get("title") or meta.get("track") or fallback_query, limit=160)
        artist = _metadata_text(meta.get("display_uploader") or meta.get("uploader") or meta.get("artist") or meta.get("creator") or meta.get("channel"), limit=120)
        if artist and title and artist.lower() not in title.lower():
            text = f"{artist} - {title}"
        else:
            text = title or artist or raw_query
        if url_is_metadata_only and text and "official" not in text.lower():
            text = f"{text} official audio"
        if url_is_metadata_only and text:
            return f"ytsearch1:{text.strip()}"
        return text.strip()

    def _agent_track_from_metadata(self, track_meta: dict[str, Any], *, body: dict[str, Any], fallback_query: Any = "") -> AgentTrack:
        virtual_cursor = track_meta.get("virtual_playlist_cursor")
        if isinstance(virtual_cursor, dict) and virtual_cursor:
            source_url = str(
                virtual_cursor.get("source_url")
                or track_meta.get("webpage_url")
                or track_meta.get("original_url")
                or ""
            ).strip()
            title = _metadata_text(virtual_cursor.get("title") or track_meta.get("title") or "Playlist", limit=160) or "Playlist"
            return AgentTrack(
                title=title,
                requester_id=safe_id(body.get("requester_id") or track_meta.get("requester_id")),
                requester_name=short_text(body.get("requester_name") or track_meta.get("requester_name"), 80),
                query="",
                webpage_url=source_url,
                source="playlist-virtual",
                transport_hint="playlist-cursor",
                virtual_playlist_cursor=dict(virtual_cursor),
            )

        query = self._query_from_track_meta(track_meta, fallback_query=fallback_query)
        title = _metadata_text(track_meta.get("display_title") or track_meta.get("title") or query, limit=160) or "Música"
        uploader = _metadata_text(track_meta.get("display_uploader") or track_meta.get("uploader") or track_meta.get("artist") or track_meta.get("channel"), limit=120)
        source = short_text(track_meta.get("display_source") or track_meta.get("source") or body.get("source") or "worker-ytdlp", 80)
        return AgentTrack(
            title=title,
            requester_id=safe_id(body.get("requester_id") or track_meta.get("requester_id")),
            requester_name=short_text(body.get("requester_name") or track_meta.get("requester_name"), 80),
            query=query,
            webpage_url=str(track_meta.get("webpage_url") or track_meta.get("original_url") or "").strip(),
            stream_url=str(track_meta.get("stream_url") or "").strip(),
            duration=_float_or_none(track_meta.get("duration")),
            uploader=uploader,
            thumbnail=short_text(track_meta.get("thumbnail"), 500),
            source=source,
            transport_hint="metadata-lazy" if not str(track_meta.get("stream_url") or "").strip() else "direct",
            audio_format_id=short_text(track_meta.get("resolved_audio_format_id") or track_meta.get("audio_format_id"), 40),
            audio_ext=short_text(track_meta.get("resolved_audio_ext") or track_meta.get("audio_ext"), 20).lower(),
            audio_codec=short_text(track_meta.get("resolved_audio_codec") or track_meta.get("audio_codec"), 40).lower(),
            audio_abr=int(float(track_meta.get("resolved_audio_abr") or track_meta.get("audio_abr") or 0) or 0),
            audio_sample_rate=int(float(track_meta.get("resolved_audio_sample_rate") or track_meta.get("audio_sample_rate") or track_meta.get("asr") or 0) or 0),
            audio_channels=int(float(track_meta.get("resolved_audio_channels") or track_meta.get("audio_channels") or track_meta.get("channels") or 0) or 0),
            start_offset_seconds=max(0.0, float(track_meta.get("start_offset_seconds") or track_meta.get("start") or body.get("position_seconds") or 0.0)),
        )

    async def _prefetch_track(self, body: dict[str, Any], track_meta: dict[str, Any], query: str, cache_key: str) -> None:
        try:
            started = time.time()
            try:
                priority = int(body.get("_prefetch_priority", 20))
            except Exception:
                priority = 20
            await asyncio.wait_for(self.resolve_track(query, track_meta=track_meta, body=body, priority=priority), timeout=self.prefetch_timeout)
            self.log(
                "prefetch_ok",
                guild_id=safe_id(body.get("guild_id")),
                title=track_meta.get("title"),
                priority=priority,
                kind=str(body.get("prefetch_kind") or "background"),
                elapsed_ms=round((time.time() - started) * 1000.0, 1),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.log("prefetch_failed", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), error=short_text(exc, 180))
        finally:
            current_task = asyncio.current_task()
            for task_key in (cache_key, self._guild_prefetch_key(safe_id(body.get("guild_id")), cache_key)):
                remove_owned_task(self._prefetch_tasks, task_key, current_task)

    async def resolve_track(self, query: str, *, track_meta: dict[str, Any], body: dict[str, Any], priority: int = 0) -> AgentTrack:
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
                audio_format_id=short_text(track_meta.get("resolved_audio_format_id") or track_meta.get("audio_format_id"), 40),
                audio_ext=short_text(track_meta.get("resolved_audio_ext") or track_meta.get("audio_ext"), 20).lower(),
                audio_codec=short_text(track_meta.get("resolved_audio_codec") or track_meta.get("audio_codec"), 40).lower(),
                audio_abr=int(float(track_meta.get("resolved_audio_abr") or track_meta.get("audio_abr") or 0) or 0),
                audio_sample_rate=int(float(track_meta.get("resolved_audio_sample_rate") or track_meta.get("audio_sample_rate") or track_meta.get("asr") or 0) or 0),
                audio_channels=int(float(track_meta.get("resolved_audio_channels") or track_meta.get("audio_channels") or track_meta.get("channels") or 0) or 0),
                start_offset_seconds=max(0.0, float(track_meta.get("start_offset_seconds") or track_meta.get("start") or body.get("position_seconds") or 0.0)),
                stream_resolved_monotonic=time.monotonic(),
            )
        cache_key = self._resolve_cache_key(query, track_meta)
        cached = self._resolve_cache_get(cache_key)
        if cached:
            self.log("resolve_stream_cache_hit", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), query=query[:90])
            return self._agent_track_from_resolved(cached, query=query, track_meta=track_meta, body=body, cached=True)
        cached_meta = self._metadata_cache_get(cache_key)
        if cached_meta:
            # Metadata stable can fill missing title/artist/duration while yt-dlp
            # refreshes the short-lived playable URL.
            merged_meta = dict(cached_meta)
            merged_meta.update({k: v for k, v in track_meta.items() if v not in (None, "", [], {})})
            track_meta = merged_meta
            self.log("resolve_metadata_cache_hit", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), query=query[:90])
        async with self._registry_lock(self._resolve_locks, self._resolve_lock_users, cache_key):
            cached = self._resolve_cache_get(cache_key)
            if cached:
                self.log("resolve_stream_cache_hit_after_wait", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), query=query[:90])
                return self._agent_track_from_resolved(cached, query=query, track_meta=track_meta, body=body, cached=True)
            started = time.time()
            async with self._resolve_slot(priority):
                # The blocking resolver runs in a worker thread, but cancellation
                # propagates through this Event so an active yt-dlp process (and
                # its JS-runtime children) is terminated instead of leaking.
                cancel_event = threading.Event()

                def _run() -> dict[str, Any]:
                    self._resolve_thread_local.cancel_event = cancel_event
                    try:
                        return self._resolve_with_ytdlp(query)
                    finally:
                        with contextlib.suppress(Exception):
                            del self._resolve_thread_local.cancel_event

                resolver_task = asyncio.create_task(asyncio.to_thread(_run))
                try:
                    resolved = await asyncio.shield(resolver_task)
                except asyncio.CancelledError:
                    cancel_event.set()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(asyncio.shield(resolver_task), timeout=1.5)
                    raise
            self._resolve_cache_put(cache_key, resolved)
            self.log("resolve_ytdlp_done", guild_id=safe_id(body.get("guild_id")), elapsed_ms=round((time.time() - started) * 1000.0, 1), title=resolved.get("title"))
            return self._agent_track_from_resolved(resolved, query=query, track_meta=track_meta, body=body, cached=False)

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
            warm_enabled = str(os.getenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on", "sim"}
            if warm_enabled:
                client = getattr(self, "_ytdlp_warm_client", None)
                if client is None:
                    client = WarmYTDLPResolver()
                    self._ytdlp_warm_client = client
                cancel_event = getattr(self._resolve_thread_local, "cancel_event", None)
                hot_started = time.time()
                try:
                    hot = client.resolve(
                        target,
                        format_selector=self.ytdlp_format,
                        cookiefile=str(cookies) if cookies.exists() and cookies.stat().st_size > 0 else "",
                        js_runtimes=self.js_runtimes,
                        socket_timeout=12,
                        timeout=max(3.0, min(float(self.ytdlp_timeout), 10.0)),
                        cancel_event=cancel_event,
                    )
                except RuntimeError as exc:
                    if "cancelada" in str(exc).lower():
                        raise
                    hot = None
                if hot and str(hot.get("stream_url") or "").startswith(("http://", "https://")):
                    self.log(
                        "yt_dlp_warm_url_ok",
                        elapsed_ms=round((time.time() - hot_started) * 1000.0, 1),
                        helper_elapsed_ms=hot.get("elapsed_ms"),
                        title=bool(hot.get("title")),
                    )
                    hot.pop("ok", None)
                    hot.pop("id", None)
                    hot.pop("elapsed_ms", None)
                    return hot
                self.log("yt_dlp_warm_url_fallback", elapsed_ms=round((time.time() - hot_started) * 1000.0, 1))
            fast_cmd = base_cmd + [
                "-f", self.ytdlp_format,
                "--print", "__title__:%(title)s",
                "--print", "__uploader__:%(uploader,channel,creator)s",
                "--print", "__duration__:%(duration)s",
                "--print", "__thumbnail__:%(thumbnail)s",
                "--print", "__webpage_url__:%(webpage_url,original_url)s",
                "--print", "__format_id__:%(format_id)s",
                "--print", "__ext__:%(ext)s",
                "--print", "__acodec__:%(acodec)s",
                "--print", "__abr__:%(abr)s",
                "--print", "__asr__:%(asr)s",
                "--print", "__audio_channels__:%(audio_channels)s",
                "-g", target,
            ]
            fast_started = time.time()
            fast = self._run_ytdlp_command(fast_cmd, timeout=max(5, min(self.ytdlp_timeout, 18)))
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
            format_hint = marker("format_id")
            ext_hint = marker("ext")
            codec_hint = marker("acodec")
            abr_hint = marker("abr")
            asr_hint = marker("asr")
            channels_hint = marker("audio_channels")

            def metric(value: str) -> int:
                try:
                    return max(0, int(float(value)))
                except Exception:
                    return 0

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
                    "audio_format_id": format_hint or "yt-dlp-fast",
                    "audio_ext": ext_hint.lower(),
                    "audio_codec": codec_hint.lower(),
                    "audio_abr": metric(abr_hint),
                    "audio_sample_rate": metric(asr_hint),
                    "audio_channels": metric(channels_hint),
                }
            self.log("yt_dlp_fast_url_fallback", rc=fast.returncode, error=short_text(fast.stderr, 160))
        cmd = base_cmd + ["-f", self.ytdlp_format, "-J", target]
        proc = self._run_ytdlp_command(cmd, timeout=self.ytdlp_timeout)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(short_text(proc.stderr or f"yt-dlp rc={proc.returncode}", 300))
        data = json.loads(proc.stdout or "{}")
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            data = next((item for item in data.get("entries") or [] if isinstance(item, dict)), {})
        if not isinstance(data, dict) or not data:
            raise RuntimeError("yt-dlp não retornou mídia")
        stream_info = _select_stream_info(data)
        stream_url = str(stream_info.get("stream_url") or "")
        if not stream_url:
            raise RuntimeError("yt-dlp não retornou stream_url")
        return {
            "title": data.get("title") or data.get("fulltitle") or query,
            "uploader": data.get("uploader") or data.get("channel") or data.get("creator") or "",
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail") or "",
            "webpage_url": data.get("webpage_url") or data.get("original_url") or query,
            **stream_info,
        }
