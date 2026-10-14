"""Compose multipart singing through the existing monophonic worker route."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from osaka_cd.arrangement import (
    arrangement_performance_sha256, arrangement_sha256, validate_arrangement,
)
from osaka_cd.render import render_arrangement


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


class ArrangementAudioMixin:
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

        async def request_part(request):
            child = replace(item, text=request['text'], voice=request['voice_id'],
                            ttv_voice_id=request['voice_id'], ttv_score=request['score'],
                            language=request['language'], ttv_language=request['language'],
                            rate='1.0', pitch='+0Hz', ttv_pitch_semitones='+0.0',
                            teto_pitch_semitones='+0.0', ttv_speech_rate=1.0,
                            ttv_segments=request.get('segments'), ttv_performance_hash='',
                            ttv_arrangement=None, ttv_arrangement_hash='',
                            _normalized_cache_text=None, _cache_key_value=None, _dedup_signature=None)
            # Settings and cache decisions are specific to this physical score.
            for key in ('_tts_settings_frozen', '_tts_route_decision', '_tts_agent_render_metadata'):
                child.__dict__.pop(key, None)
            path = ''
            try:
                path = await self._generate_tts_agent_worker_file(child)
                raw = await asyncio.to_thread(Path(path).read_bytes)
                metadata = dict(getattr(child, '_tts_agent_render_metadata', {}))
                return raw, metadata
            finally:
                if path:
                    with contextlib.suppress(OSError):
                        os.unlink(path)

        def render_part(**request):
            # A synchronous composition thread delegates HTTP to the bot loop.
            # Its lease lasts until the actual request has returned.
            return asyncio.run_coroutine_threadsafe(request_part(request), loop).result()

        timeout = min(3600.0, max(60.0, float(os.getenv('OSAKA_CD_RENDER_TIMEOUT_SECONDS', '1200'))))
        data_root = Path(os.getenv('OSAKA_CD_DATA_ROOT', str(Path.home() / 'osaka-data/cd'))).expanduser()
        started = time.monotonic()
        async with gate:
            task = asyncio.create_task(asyncio.to_thread(
                render_arrangement, arrangement, item.text, render_part,
                cache_dir=data_root / 'audio-cache', render_profile_fingerprint=profile,
                language=item.ttv_language, output_format='mp3',
                segments=getattr(item, 'ttv_segments', None),
                cancelled=cancelled.is_set, timeout_seconds=timeout))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled.set()
                from .runtime import await_physical_completion
                try:
                    await await_physical_completion(task)
                except (Exception, asyncio.CancelledError):
                    pass
                raise
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
        metrics['cd_arrangement_last_parts'] = len(arrangement['parts'])
        metrics['cd_arrangement_last_mix_cache_hit'] = bool(result.get('mix_cache_hit'))
        return path
