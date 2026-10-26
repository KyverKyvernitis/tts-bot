"""Local progressive CD playback with RAM buffers and physical render leases."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import threading
import time

from osaka_cd.progressive import DecodedPCMCache, PREROLL_MS, stream_arrangement
from .cd_stream import PCMStreamBuffer
from .runtime import await_physical_completion


@dataclass
class CDPCMHandle:
    path: str
    item: object
    buffer: PCMStreamBuffer
    cancelled: threading.Event = field(default_factory=threading.Event)
    producer_task: object = None
    metadata: dict = field(default_factory=dict)
    finalized: bool = False


class CDStreamingMixin:
    def _cd_stream_for_path(self, path):
        if not path:
            return None
        return getattr(self, '_cd_pcm_handles', {}).get(os.path.abspath(path))

    def _cd_streaming_allowed_for(self, item):
        if os.getenv('OSAKA_CD_STREAMING_ENABLED', 'true').strip().lower() in {'0', 'false', 'no', 'off'}:
            return False, 'disabled'
        if getattr(self, '_tts_shutting_down', False):
            return False, 'shutdown'
        if (getattr(item, 'engine', '') != 'teto' or not getattr(item, 'ttv_arrangement', None)
                or getattr(item, '_cd_force_full_file', False)
                or getattr(item, 'chatbot_no_auto_connect', False)):
            return False, 'not_local_cd'
        available = getattr(self, '_tts_agent_route_available', None)
        if not callable(available) or not available():
            return False, 'worker_unavailable'
        preferred = getattr(self, '_tts_agent_should_try_worker', None)
        if callable(preferred) and not preferred(item)[0]:
            return False, 'worker_not_selected'
        from cogs.musica.integracoes.tts import deve_rotear_tts_para_agente
        if deve_rotear_tts_para_agente(getattr(self, 'bot', None), item.guild_id, item.channel_id):
            return False, 'remote_owner_requires_complete_audio'
        music = getattr(self, '_is_music_active_for_guild', None)
        if callable(music) and music(item.guild_id):
            # Remote/mixed file contracts must never receive a marker path.
            return False, 'music_requires_complete_audio'
        owned = getattr(self, '_get_voice_client_for_guild', None)
        if callable(owned):
            get_guild = getattr(getattr(self, 'bot', None), 'get_guild', None)
            guild = get_guild(item.guild_id) if callable(get_guild) else None
            client = owned(guild) if guild is not None else None
            if client is not None and getattr(self, '_voice_client_owned_by_music', lambda _: False)(client):
                return False, 'music_owns_voice'
        return True, 'local_pcm'

    async def _wait_cd_prefetch_turn(self, item):
        while getattr(item, '_tts_prefetch', False):
            if getattr(self, '_tts_shutting_down', False):
                raise asyncio.CancelledError()
            own = self._cd_stream_for_path(getattr(item, '_cd_stream_path', ''))
            if own is not None and (own.cancelled.is_set() or own.buffer.stop.is_set()):
                raise asyncio.CancelledError()
            others = [handle for handle in getattr(self, '_cd_pcm_handles', {}).values()
                      if handle.item is not item and handle.producer_task is not None
                      and not handle.producer_task.done()
                      and not getattr(handle.item, '_tts_prefetch', False)]
            if not others:
                return
            await asyncio.sleep(.025)

    async def _prepare_cd_pcm_stream(self, state, item):
        from .arrangement_audio import arrangement_profile_identity, frozen_arrangement
        arrangement = frozen_arrangement(item)
        profile = arrangement_profile_identity(item, self._tts_agent_route_state())
        if not profile:
            raise RuntimeError('identidade do banco de canto indisponível')
        cache = getattr(self, '_cd_decoded_pcm_cache', None)
        if cache is None:
            cache = self._cd_decoded_pcm_cache = DecodedPCMCache()
        path = self._make_runtime_temp_file(suffix='.cdpcm')
        buffer = PCMStreamBuffer(arrangement['duration_ms'] + PREROLL_MS,
                                 prebuffer_ms=1000, safety_ms=500)
        if not getattr(item, '_tts_prefetch', False):
            buffer.promote()
        handle = CDPCMHandle(path, item, buffer)
        handles = getattr(self, '_cd_pcm_handles', None)
        if handles is None:
            handles = self._cd_pcm_handles = {}
        handles[os.path.abspath(path)] = handle
        item._tts_audio_origin, item._tts_audio_route = 'progressive_pcm', 'worker'
        item._cd_stream_path = path
        loop = asyncio.get_running_loop()
        gate = getattr(self, '_cd_composition_gate', None)
        if gate is None:
            gate = self._cd_composition_gate = asyncio.Semaphore(1)
        root = Path(os.getenv('OSAKA_CD_DATA_ROOT', str(Path.home() / 'osaka-data/cd'))).expanduser()

        def stopped():
            return handle.cancelled.is_set() or buffer.stop.is_set()

        async def request(call):
            await self._wait_cd_prefetch_turn(item)
            if stopped():
                raise RuntimeError('cd_pcm_cancelled')
            self._cd_foreground_compositions = int(getattr(self, '_cd_foreground_compositions', 0)) + 1
            try:
                async with gate:
                    if stopped():
                        raise RuntimeError('cd_pcm_cancelled')
                    return await self._request_cd_stem(item, call, progressive=True)
            finally:
                self._cd_foreground_compositions -= 1

        def render_part(**call):
            return asyncio.run_coroutine_threadsafe(request(call), loop).result()

        async def produce():
            physical = None
            try:
                await self._wait_cd_prefetch_turn(item)
                if stopped():
                    raise asyncio.CancelledError()
                last_publication = time.monotonic()

                def emit(pcm):
                    nonlocal last_publication
                    now = time.monotonic()
                    buffer.append(pcm, generation_ms=max(0.0, (now - last_publication) * 1000))
                    last_publication = now

                physical = asyncio.create_task(asyncio.to_thread(stream_arrangement,
                    arrangement, item.text, render_part, emit,
                    segments=getattr(item, 'ttv_segments', None), language=item.ttv_language,
                    render_profile_fingerprint=profile, memory_cache=cache,
                    cache_dir=root / 'audio-cache', cancelled=stopped,
                    first_window_ms=3000, later_window_ms=8000, timeout_seconds=1200,
                    require_normalization=True))
                handle.metadata = await asyncio.shield(physical)
                item._tts_agent_render_metadata = handle.metadata
                metrics = self._get_metrics_store()
                metrics['cd_pcm_last_first_pcm_ms'] = handle.metadata.get('first_pcm_ms')
                metrics['cd_pcm_last_timing_ms'] = handle.metadata.get('timing_ms', {})
                metrics['cd_pcm_cache_bytes'] = cache.size_bytes
                buffer.finish()
            except asyncio.CancelledError:
                handle.cancelled.set()
                buffer.cancel()
                if physical is not None:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await await_physical_completion(physical)
                raise
            except Exception as error:
                buffer.finish(error)

        handle.producer_task = asyncio.create_task(produce())
        try:
            await buffer.wait_ready(timeout=1200)
            return handle
        except BaseException:
            await self._finalize_cd_pcm_stream(handle, cancel=True)
            raise

    async def _finalize_cd_pcm_stream(self, handle, *, cancel=False):
        if handle.finalized:
            return
        handle.finalized = True
        try:
            if cancel:
                handle.cancelled.set()
                handle.buffer.cancel()
            # Do not cancel the HTTP coroutine: native work owns its lease
            # until the physical renderer has really returned.
            if handle.producer_task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await await_physical_completion(handle.producer_task)
        finally:
            handle.buffer.cleanup()
            getattr(self, '_cd_pcm_handles', {}).pop(os.path.abspath(handle.path), None)
            with contextlib.suppress(OSError):
                os.unlink(handle.path)

    def _cancel_cd_pcm_streams(self):
        for handle in list(getattr(self, '_cd_pcm_handles', {}).values()):
            handle.cancelled.set()
            handle.buffer.cancel()
            task = asyncio.create_task(self._finalize_cd_pcm_stream(handle, cancel=True))
            # Existing shutdown also cancels generic background tasks. Keep
            # drain tasks separate so native process leases cannot be released.
            drains = getattr(self, '_cd_pcm_drains', None)
            if drains is None:
                drains = self._cd_pcm_drains = set()
            drains.add(task)
            task.add_done_callback(drains.discard)
