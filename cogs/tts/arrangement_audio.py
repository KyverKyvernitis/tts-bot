"""Compose multipart singing through the existing monophonic worker route."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time

from osaka_cd.arrangement import (
    arrangement_performance_sha256, arrangement_sha256, validate_arrangement,
)
from osaka_cd.render import render_arrangement, warm_arrangement_stems
from .cd_audio import CDStreamingMixin

_LOG = logging.getLogger(__name__)


def execution_arrangement(value, voice_id, *, message=True):
    """Freeze user controls without changing the catalog's original arrangement."""
    if value is None:
        return None
    result = validate_arrangement(value)
    for part in result['parts']:
        part['voice_id'] = voice_id
        if message and part['role'] == 'lead':
            part['lyrics_source'] = 'message'
            for note in part['notes']:
                note.pop('lyric', None)
    return validate_arrangement(result)


def frozen_arrangement(item):
    value = getattr(item, 'ttv_arrangement', None)
    if value is None:
        if getattr(item, 'ttv_arrangement_hash', ''):
            raise ValueError('TTV: identidade de arranjo sem partes musicais.')
        return None
    if str(getattr(item, 'engine', '')).lower() != 'teto':
        raise ValueError('TTV: um arranjo musical exige uma vocaloid.')
    result = validate_arrangement(value)
    expected = arrangement_sha256(result)
    if getattr(item, 'ttv_arrangement_hash', '') not in ('', expected):
        raise ValueError('TTV: o arranjo mudou depois da preparação.')
    item.ttv_arrangement = result
    item.ttv_arrangement_hash = expected
    return result


def arrangement_cache_identity(item):
    value = frozen_arrangement(item)
    if value is None:
        return ''
    raw = json.dumps({'arrangement': arrangement_performance_sha256(value),
                      'lyrics_policy': value.get('message_lyrics_mode', 'fixed'),
                      'text': item.text, 'segments': getattr(item, 'ttv_segments', None)},
                     sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def arrangement_profile_identity(item, route):
    """Bind every stem to its real singing bank; unknown banks disable reuse."""
    value = frozen_arrangement(item)
    if value is None:
        return ''
    from .ttv import worker_voice_details
    profiles = {}
    language = getattr(item, 'ttv_language', 'pt-BR')
    for part in value['parts']:
        voice = worker_voice_details(route, part['voice_id'])
        fingerprint = (voice.get('singing_fingerprints') or {}).get(language)
        if not fingerprint:
            fingerprint = (voice.get('language_fingerprints') or {}).get(language) or voice.get('fingerprint')
        if not isinstance(fingerprint, str) or not fingerprint.strip() or fingerprint == 'unavailable':
            return ''
        profiles[part['voice_id']] = fingerprint
    return hashlib.sha256(json.dumps(profiles, sort_keys=True).encode()).hexdigest()


def _stem_child(item, request, *, background=False, progressive=False):
    child = replace(item, text=request['text'], voice=request['voice_id'],
                        ttv_voice_id=request['voice_id'], ttv_score=request['score'],
                        language=request['language'], ttv_language=request['language'],
                        rate='1.0', pitch='+0Hz', ttv_pitch_semitones='+0.0',
                        teto_pitch_semitones='+0.0', ttv_speech_rate=1.0,
                        ttv_segments=request.get('segments'), ttv_performance_hash='',
                        ttv_arrangement=None, ttv_arrangement_hash='',
                        _normalized_cache_text=None, _cache_key_value=None, _dedup_signature=None)
    for key in ('_tts_settings_frozen', '_tts_route_decision', '_tts_agent_render_metadata'):
        child.__dict__.pop(key, None)
    if background:
        child._cd_background = True
    if progressive:
        child._cd_progressive_request = True
    return child


class ArrangementAudioMixin(CDStreamingMixin):
    async def _request_cd_stem_stream(self, item, request, *, on_pcm):
        if self._tts_agent_route_state().get('ttv_pcm_stream_protocol') != 1:
            return await self._request_cd_stem(item, request, progressive=True)
        from .cd_worker_stream import request_pcm_stream
        return await request_pcm_stream(
            self, _stem_child(item, request, progressive=True), on_pcm)

    async def _request_cd_stem(self, item, request, *, background=False, progressive=False):
        child = _stem_child(item, request, background=background, progressive=progressive)
        request_bytes = getattr(self, '_generate_tts_agent_worker_bytes', None)
        if callable(request_bytes):
            raw, metadata = await request_bytes(child)
            return raw, dict(metadata)
        path = ''
        try:
            path = await self._generate_tts_agent_worker_file(child)
            raw = await asyncio.to_thread(Path(path).read_bytes)
            return raw, dict(getattr(child, '_tts_agent_render_metadata', {}))
        finally:
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def _cd_warmup_idle(self):
        if getattr(self, '_tts_shutting_down', False):
            return False
        if int(getattr(self, '_cd_foreground_compositions', 0)):
            return False
        pending = getattr(self, '_tts_foreground_work_pending', None)
        if callable(pending) and pending():
            return False
        if any(not task.done() for task in getattr(self, '_tts_message_tasks', {}).values()):
            return False
        return not any(getattr(state, 'active_item', None) is not None
                       or not state.queue.empty()
                       for state in getattr(self, 'guild_states', {}).values())

    def _schedule_cd_random_warmup(self, guild_id, user_id, *, settings):
        """Warm future random choices without drawing or changing their cycle."""
        return self._schedule_cd_accompaniment_warmup(
            guild_id, user_id, melody_id=settings.get('ttv_melody_id'),
            settings=settings, random_choices=settings.get('ttv_random_cd') is True)

    def _schedule_cd_accompaniment_warmup(self, guild_id, user_id, *, melody_id, settings,
                                          random_choices=False):
        owner = (int(guild_id), int(user_id))
        tasks = getattr(self, '_cd_warmup_tasks', None)
        if tasks is None:
            tasks = self._cd_warmup_tasks = {}
        previous = tasks.pop(owner, None)
        if previous is not None and not previous.done():
            previous.cancel()
        if getattr(self, '_tts_shutting_down', False):
            return None
        if os.getenv('OSAKA_CD_WARMUP_ENABLED', 'true').strip().lower() in {'0', 'false', 'no', 'off'}:
            return None
        from .melodies import DEFAULT_MELODY_ID
        if not random_choices and melody_id == DEFAULT_MELODY_ID:
            return None
        if len(tasks) >= 16 and owner not in tasks:
            return None
        frozen_settings = dict(settings)

        async def run():
            from .audio import QueueItem
            from .melodies import arrangement_for_melody
            from .ttv import resolve_preferences
            from osaka_cd.runtime import pin_snapshot
            preferences = resolve_preferences(frozen_settings)
            idle_since, deadline = None, time.monotonic() + 300
            while time.monotonic() < deadline:
                if self._cd_warmup_idle():
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= 1:
                        break
                else:
                    idle_since = None
                await asyncio.sleep(.25)
            else:
                return
            route = self._tts_agent_route_state()
            if route.get('ttv_background_protocol') != 1:
                return
            if random_choices:
                from .random_cd import remaining_random_melodies
                candidates = tuple(entry.id for entry in remaining_random_melodies(
                    self, guild_id, user_id))
            else:
                candidates = (melody_id,)
            gate = getattr(self, '_cd_warmup_gate', None)
            if gate is None:
                gate = self._cd_warmup_gate = asyncio.Lock()
            await asyncio.wait_for(gate.acquire(), timeout=max(.01, deadline-time.monotonic()))
            try:
                for identity in candidates:
                    if time.monotonic() >= deadline or not self._cd_warmup_idle():
                        break
                    try:
                        with pin_snapshot():
                            arrangement = execution_arrangement(
                                arrangement_for_melody(identity, preview=True), preferences['ttv_voice_id'])
                    except ValueError:
                        # A catalog activation can retire a speculative choice.
                        continue
                    if arrangement is None or not any(part.get('lyrics_source') != 'message'
                                                       for part in arrangement['parts']):
                        continue
                    item = QueueItem(guild_id=int(guild_id), channel_id=0, author_id=int(user_id),
                        text='a', engine='teto', voice=preferences['ttv_voice_id'],
                        language=preferences['ttv_language'], rate='1.0', pitch='+0Hz',
                        teto_pitch_semitones='+0.0', ttv_arrangement=arrangement, **preferences)
                    profile = arrangement_profile_identity(item, self._tts_agent_route_state())
                    if not profile:
                        continue
                    await self._warm_cd_accompaniment(
                        item, profile, timeout_seconds=max(.01, min(300, deadline-time.monotonic())))
            finally:
                gate.release()

        async def guarded():
            try:
                await run()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Speculation is optional and must not suspend a healthy route
                # or put lyrics, local paths or credentials in logs.
                _LOG.debug('[cd_warmup] stopped type=%s', type(error).__name__)
            finally:
                if tasks.get(owner) is asyncio.current_task():
                    tasks.pop(owner, None)

        schedule = getattr(self, '_schedule_tts_background', asyncio.create_task)
        task = schedule(guarded())
        if task is not None:
            tasks[owner] = task
        return task

    async def _warm_cd_accompaniment(self, item, profile, *, timeout_seconds=300):
        from osaka_cd.progressive import DecodedPCMCache, FIRST_WINDOW_MS, LATER_WINDOW_MS
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        cache = getattr(self, '_cd_decoded_pcm_cache', None)
        if cache is None:
            cache = self._cd_decoded_pcm_cache = DecodedPCMCache()

        async def request(request):
            if not self._cd_warmup_idle():
                raise RuntimeError('cd_warmup_foreground_pending')
            return await self._request_cd_stem(item, request, background=True, progressive=True)

        def render_part(**call):
            return asyncio.run_coroutine_threadsafe(request(call), loop).result()

        data_root = Path(os.getenv('OSAKA_CD_DATA_ROOT', str(Path.home() / 'osaka-data/cd'))).expanduser()
        # No foreground gate here: a user request must reach the phone so it
        # can preempt the active background render and await physical cleanup.
        task = asyncio.create_task(asyncio.to_thread(warm_arrangement_stems,
            frozen_arrangement(item), render_part, cache_dir=data_root / 'audio-cache',
            render_profile_fingerprint=profile, language=item.ttv_language,
            cancelled=cancelled.is_set, timeout_seconds=timeout_seconds, max_stems=8,
            first_request_ms=FIRST_WINDOW_MS, request_ms=LATER_WINDOW_MS,
            first_only=True, memory_cache=cache))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            from .runtime import await_physical_completion
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await await_physical_completion(task)
            raise
        metrics = self._get_metrics_store()
        metrics['cd_warmup_last_stems'] = len(result['stems'])
        metrics['cd_warmup_last_rendered'] = sum(not stem['cache_hit'] for stem in result['stems'])
        metrics['cd_pcm_cache_bytes'] = cache.size_bytes
        return result

    async def _generate_arrangement_worker_file(self, item):
        arrangement = frozen_arrangement(item)
        if arrangement is None:
            raise ValueError('TTV: arranjo ausente.')
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        gate = getattr(self, '_cd_composition_gate', None)
        if gate is None:
            gate = self._cd_composition_gate = asyncio.Semaphore(1)
        route = self._tts_agent_route_state()
        # Never substitute a static protocol version for the actual bank identity.
        profile = arrangement_profile_identity(item, route)

        def render_part(**request):
            # A synchronous composition thread delegates HTTP to the bot loop.
            # Its lease lasts until the actual request has returned.
            return asyncio.run_coroutine_threadsafe(self._request_cd_stem(item, request), loop).result()

        timeout = min(3600.0, max(60.0, float(os.getenv('OSAKA_CD_RENDER_TIMEOUT_SECONDS', '1200'))))
        data_root = Path(os.getenv('OSAKA_CD_DATA_ROOT', str(Path.home() / 'osaka-data/cd'))).expanduser()
        started = time.monotonic()
        self._cd_foreground_compositions = int(getattr(self, '_cd_foreground_compositions', 0)) + 1
        gate_started = time.monotonic()
        try:
            async with gate:
                gate_wait_ms = (time.monotonic() - gate_started) * 1000
                result = await self._run_cd_composition(item, arrangement, render_part, profile,
                                                        data_root, cancelled, timeout)
        finally:
            self._cd_foreground_compositions -= 1
        result['metadata']['timing_ms']['composition_queue'] = round(gate_wait_ms, 2)
        for field, stage in (('_cd_preparation_ms', 'preparation'),
                             ('_cd_lyrics_adaptation_ms', 'lyrics_adaptation')):
            if hasattr(item, field):
                result['metadata']['timing_ms'][stage] = getattr(item, field)
        path = self._make_runtime_temp_file(suffix='.mp3')
        try:
            await asyncio.to_thread(Path(path).write_bytes, result['audio'])
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        item._tts_agent_selected_engine = 'teto'
        item._tts_agent_render_metadata = result['metadata']
        item._cd_arrangement_result = {key: result[key] for key in
                                      ('sha256', 'mix_sha256', 'arrangement_sha256', 'stems')}
        metrics = self._get_metrics_store()
        metrics['cd_arrangement_last_ms'] = round((time.monotonic() - started) * 1000, 2)
        metrics['cd_arrangement_last_queue_ms'] = round(gate_wait_ms, 2)
        metrics['cd_arrangement_last_timing_ms'] = result['metadata']['timing_ms']
        metrics['cd_arrangement_last_stems'] = result['stems']
        metrics['cd_arrangement_last_parts'] = len(arrangement['parts'])
        metrics['cd_arrangement_last_mix_cache_hit'] = bool(result.get('mix_cache_hit'))
        _LOG.info('[cd_render] mix=%s parts=%s stems=%s mix_cache_hit=%s resampler_calls=%s timing_ms=%s',
                  result['mix_sha256'][:12], len(arrangement['parts']), len(result['stems']),
                  bool(result.get('mix_cache_hit')),
                  sum(stem['render_metrics'].get('resampler_calls', 0) for stem in result['stems']),
                  result['metadata']['timing_ms'])
        return path

    async def _run_cd_composition(self, item, arrangement, render_part, profile, data_root, cancelled, timeout):
        task = asyncio.create_task(asyncio.to_thread(
            render_arrangement, arrangement, item.text, render_part,
            cache_dir=data_root / 'audio-cache', render_profile_fingerprint=profile,
            language=item.ttv_language, output_format='mp3',
            segments=getattr(item, 'ttv_segments', None),
            cancelled=cancelled.is_set, timeout_seconds=timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            from .runtime import await_physical_completion
            try:
                await await_physical_completion(task)
            except (Exception, asyncio.CancelledError):
                pass
            raise
