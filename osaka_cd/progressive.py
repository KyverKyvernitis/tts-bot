"""Chronological CD synthesis and stereo PCM publication without a final file.

Only immutable decoded stems are shared. The per-presentation timelines and
the not-yet-published consonantal overlap belong to one producer thread.
"""
from __future__ import annotations

import array
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

from .arrangement import arrangement_render_plan, arrangement_sha256, validate_arrangement, vowel_guide_score
from .lyrics import automatic_render_plan
from .render import (
    MAX_AUDIO_BYTES, MAX_STEM_BYTES, SAMPLE_RATE, RenderError, _cached_stem,
    _check, _digest, _ffmpeg, _message_allocation, _mix_controls, _preroll,
    _remember_stem, _render_metrics, _result_audio, _stem_key, _wav_info,
    _worker_stage_metrics,
)

PROGRESSIVE_VERSION = 'osaka-progressive-pcm-1'
PREROLL_MS = 350
DECODED_CACHE_POLICY = ':pcm48k-unit-normalization-v1'


@dataclass(frozen=True)
class DecodedStem:
    pcm: bytes
    preroll_frames: int
    raw_sha256: str
    normalization_gain: float | None = None


class DecodedPCMCache:
    """A bounded, thread-safe L1 cache of immutable 48 kHz mono PCM16."""
    def __init__(self, max_bytes=512 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('invalid_pcm_cache_budget')
        self.max_bytes = max_bytes
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            stem = self._items.get(key)
            if stem is not None:
                self._items.move_to_end(key)
            return stem

    def put(self, key, stem):
        if not isinstance(stem, DecodedStem) or type(stem.pcm) is not bytes:
            raise TypeError('immutable_decoded_stem_required')
        if len(stem.pcm) > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old.pcm)
            self._items[key] = stem
            self._bytes += len(stem.pcm)
            while self._bytes > self.max_bytes:
                _, old = self._items.popitem(last=False)
                self._bytes -= len(old.pcm)

    def clear(self):
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def size_bytes(self):
        with self._lock:
            return self._bytes


def _requests(arrangement, text, segments, language, first_window_ms, later_window_ms):
    automatic = arrangement.get('message_lyrics_mode') in {'automatic-lyrics-v1', 'automatic-lyrics-v2'}
    if automatic:
        plan = automatic_render_plan(arrangement, text, segments, language=language,
            first_request_ms=first_window_ms, request_ms=later_window_ms)
    else:
        # Old fixed-score callers keep their exact lyric allocation. Message
        # preparation normally upgrades free lyrics to automatic-lyrics-v2.
        plan = arrangement_render_plan(arrangement)
    words = {}
    for part in arrangement['parts'] if not automatic else ():
        requests = [request for chunk in plan['chunks'] for request in chunk['parts']
                    if request['id'] == part['id'] and request['score'].get('lyrics_source') == 'message']
        if requests:
            words[part['id']] = iter(_message_allocation(text, requests, segments))
    result = []
    for chunk in plan['chunks']:
        for request in chunk['parts']:
            score = json.loads(json.dumps(request['score']))
            effects = None
            if score.get('lyrics_source') == 'message':
                if automatic:
                    lyrics, effects = request['adaptive_text'], request['adaptive_segments']
                else:
                    lyrics, effects = next(words[request['id']])
                if not lyrics:
                    score, lyrics = vowel_guide_score(score), 'a'
            elif score.get('lyrics_source') == 'midi':
                lyrics = 'a'
            else:
                score['lyrics_source'], lyrics = 'vowel', 'a'
            call = dict(score=score, text=lyrics, voice_id=request['voice_id'],
                        language=language, pitch=0.0, rate=1.0,
                        part_id=request['id'], role=request.get('role', 'harmony'))
            if effects is not None:
                call['segments'] = effects
            result.append(dict(start_ms=chunk['start_ms'], call=call,
                continuations=any(marker.get('continues_from_previous') or marker.get('continues_in_next')
                                  for marker in request.get('continuations', []))))
    # Later words must not leapfrog another voice's first audible window.
    result.sort(key=lambda item: (item['start_ms'], item['call']['role'] != 'lead'))
    return result, list(plan.get('warnings', ()))


def _normalization_gain(metadata):
    value = metadata.get('normalization_gain')
    # The renderer's _apply_gain caps the effective gain at 0.6..1.2. Reported
    # values outside that interval cannot describe the current PCM contract.
    if type(value) not in (int, float) or not 0.6 <= value <= 1.2:
        return None
    return float(value)


def _decode(audio, meta, score, *, require_normalization=False):
    preroll = _preroll(audio, meta, score)
    rate, _, pcm = _wav_info(audio)
    gain = _normalization_gain(meta)
    if require_normalization and gain is None:
        raise RenderError('stem_normalization_metadata_missing')
    if gain is not None and gain != 1.0:
        samples = array.array('h', pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        # The source timeline was already PCM16 before its request-wide peak
        # adjustment. Undo that one constant factor, retaining MIDI intensity
        # and word fades. This avoids a volume step when neighbouring requests
        # were normalized using different peaks; it introduces no new AGC.
        for index, value in enumerate(samples):
            samples[index] = max(-32768, min(32767, round(value / gain)))
        if sys.byteorder != 'little':
            samples.byteswap()
        pcm = samples.tobytes()
    if rate != SAMPLE_RATE:
        # High quality conversion is done once per decoded-cache miss. The
        # realtime mixer and consumers only see a single fixed PCM format.
        pcm = _ffmpeg(['-f', 's16le', '-ar', str(rate), '-ac', '1', '-i', 'pipe:0',
                       '-ar', str(SAMPLE_RATE), '-ac', '1', '-f', 's16le', 'pipe:1'],
                      data=pcm, timeout=40)
    expected = round((score['duration_ms'] + preroll) * SAMPLE_RATE / 1000)
    actual = len(pcm) // 2
    if len(pcm) % 2 or abs(actual - expected) > 2:
        raise RenderError('invalid_resampled_stem_duration')
    if actual < expected:
        pcm += bytes((expected - actual) * 2)
    elif actual > expected:
        pcm = pcm[:expected * 2]
    return DecodedStem(pcm, round(preroll * SAMPLE_RATE / 1000), hashlib.sha256(audio).hexdigest(), gain)


def stream_arrangement(arrangement, text, render_part, emit_pcm, *, segments=None,
                       render_profile_fingerprint='', language='pt-BR',
                       memory_cache=None, cache_dir=None, first_window_ms=3000,
                       later_window_ms=8000, cancelled=None, timeout_seconds=1200,
                       require_normalization=False):
    """Emit complete synchronized stereo PCM windows as soon as they are final.

    ``emit_pcm(bytes)`` runs in this producer thread. It may block for bounded
    backpressure or raise to cancel. The caller retains the physical synthesis
    lease until this function exits, including after a consumer cancellation.
    Output is PCM16 little endian, stereo, 48 kHz. Initial fixed 350 ms preroll
    removes the need to discover the largest preroll in the entire song.
    """
    import math
    started = time.monotonic()
    arrangement = validate_arrangement(arrangement)
    if not isinstance(text, str) or language not in {'pt-BR', 'en-US'}:
        raise RenderError('invalid_render_text_or_language')
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600):
        raise RenderError('invalid_render_timeout')
    for value in (first_window_ms, later_window_ms):
        if type(value) is not int or not 40 <= value <= 16000:
            raise RenderError('invalid_progressive_window')
    deadline = started + timeout_seconds
    _check(cancelled, deadline)
    requests, warnings = _requests(arrangement, text, segments, language,
                                   first_window_ms, later_window_ms)
    if not requests:
        raise RenderError('empty_composition')
    controls = {part['id']: _mix_controls(part) for part in arrangement['parts']}
    gain_sum = sum(gain for gain, _ in controls.values())
    if gain_sum <= 0:
        raise RenderError('silent_composition_gain')
    total_frames = round((arrangement['duration_ms'] + PREROLL_MS) * SAMPLE_RATE / 1000)
    if total_frames * 4 + 44 > MAX_AUDIO_BYTES:
        raise RenderError('composition_size_limit')
    # A presentation may intentionally spend RAM to avoid sparse disk timelines.
    timelines = {identity: array.array('h', [0]) * total_frames for identity in controls}
    previous_end = dict.fromkeys(controls, 0)
    gains = {identity: (gain * min(1.0, 1-pan) * 0.92 / gain_sum,
                        gain * min(1.0, 1+pan) * 0.92 / gain_sum)
             for identity, (gain, pan) in controls.items()}
    cache = Path(cache_dir) if cache_dir is not None and render_profile_fingerprint else None
    stems, pending_disk = [], []
    cursor, emitted_bytes = 0, 0
    first_pcm_ms, first_nonzero_frame = None, None
    pcm_hash = hashlib.sha256()
    timings = {'plan': (time.monotonic()-started)*1000, 'synthesize': 0.0,
               'cache_lookup': 0.0, 'decode': 0.0, 'mix': 0.0, 'cache_store': 0.0}

    def publish(end):
        nonlocal cursor, emitted_bytes, first_pcm_ms, first_nonzero_frame
        if end <= cursor:
            return
        # Bounded publications allow prompt cancellation even for a long final
        # sustain, and let the consumer slice native Discord 20 ms frames.
        while cursor < end:
            _check(cancelled, deadline)
            stop = min(end, cursor + SAMPLE_RATE // 2)
            mix_started = time.monotonic()
            output = array.array('h')
            items = [(timelines[identity], *gains[identity]) for identity in controls]
            for frame in range(cursor, stop):
                left = right = 0.0
                for timeline, left_gain, right_gain in items:
                    value = timeline[frame]
                    left += value * left_gain
                    right += value * right_gain
                left, right = round(left), round(right)
                if first_nonzero_frame is None and (left or right):
                    first_nonzero_frame = frame
                output.extend((left, right))
            if sys.byteorder != 'little':
                output.byteswap()
            raw = output.tobytes()
            timings['mix'] += (time.monotonic()-mix_started)*1000
            _check(cancelled, deadline)
            if emit_pcm(raw) is False:
                raise RenderError('render_cancelled')
            if first_pcm_ms is None:
                first_pcm_ms = (time.monotonic()-started)*1000
            pcm_hash.update(raw)
            emitted_bytes += len(raw)
            cursor = stop

    for number, request in enumerate(requests):
        _check(cancelled, deadline)
        call, start_ms = request['call'], request['start_ms']
        key = _stem_key(call, render_profile_fingerprint)
        decoded_key = key + DECODED_CACHE_POLICY
        lookup_started = time.monotonic()
        decoded = memory_cache.get(decoded_key) if memory_cache is not None and render_profile_fingerprint else None
        if decoded is not None and require_normalization and decoded.normalization_gain is None:
            decoded = None
        hit = decoded is not None
        hit_kind = 'memory' if hit else ''
        metrics, worker_timing = {}, {}
        if decoded is None:
            saved = _cached_stem(cache, key, call['score'])
            if saved is not None and require_normalization and _normalization_gain(saved[1]) is None:
                saved = None
            hit = saved is not None
            hit_kind = 'disk' if hit else ''
            timings['cache_lookup'] += (time.monotonic()-lookup_started)*1000
            synth_started = time.monotonic()
            audio, meta = saved if hit else _result_audio(render_part(**call))
            if not hit:
                timings['synthesize'] += (time.monotonic()-synth_started)*1000
                metrics, worker_timing = _render_metrics(meta), _worker_stage_metrics(meta)
            _check(cancelled, deadline)
            if len(audio) > MAX_STEM_BYTES:
                raise RenderError('stem_size_limit')
            decode_started = time.monotonic()
            decoded = _decode(audio, meta, call['score'], require_normalization=require_normalization)
            timings['decode'] += (time.monotonic()-decode_started)*1000
            if memory_cache is not None and render_profile_fingerprint:
                memory_cache.put(decoded_key, decoded)
            if cache is not None and not hit:
                pending_disk.append((key, audio, meta, decoded.preroll_frames*1000/SAMPLE_RATE))
        else:
            timings['cache_lookup'] += (time.monotonic()-lookup_started)*1000
        samples = array.array('h', decoded.pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        identity = call['part_id']
        offset = round((PREROLL_MS + start_ms) * SAMPLE_RATE / 1000) - decoded.preroll_frames
        if offset < cursor or offset < 0:
            raise RenderError('progressive_preroll_crossed_published_audio')
        samples = samples[:max(0, total_frames-offset)]
        timeline = timelines[identity]
        overlap = max(0, min(len(samples), previous_end[identity]-offset))
        denominator = max(1, overlap-1)
        for index in range(overlap):
            ratio = index/denominator
            samples[index] = round(timeline[offset+index]*(1-ratio) + samples[index]*ratio)
        timeline[offset:offset+len(samples)] = samples
        previous_end[identity] = max(previous_end[identity], offset+len(samples))
        stems.append(dict(part_id=identity, voice_id=call['voice_id'], start_ms=start_ms,
            score_sha256=_digest(call['score']),
            key=key, cache_hit=hit, cache_kind=hit_kind, render_sha256=decoded.raw_sha256,
            render_metrics=metrics, worker_timing_ms=worker_timing,
            normalization_gain=decoded.normalization_gain,
            continuations=request['continuations']))
        # Every future source begins no earlier than its request's clock start:
        # fixed350ms - actual_preroll>=0. Retain the unsealed overlap in RAM.
        next_start = requests[number+1]['start_ms'] if number+1 < len(requests) else None
        if next_start is None:
            publish(total_frames)
        elif next_start > start_ms:
            publish(min(total_frames, round(next_start*SAMPLE_RATE/1000)))
    _check(cancelled, deadline)
    store_started = time.monotonic()
    for key, audio, meta, preroll in pending_disk:
        _check(cancelled, deadline)
        _remember_stem(cache, key, audio, meta, preroll)
    timings['cache_store'] = (time.monotonic()-store_started)*1000
    return dict(renderer_version=PROGRESSIVE_VERSION, duration_ms=arrangement['duration_ms']+PREROLL_MS,
        musical_duration_ms=arrangement['duration_ms'], sample_rate=SAMPLE_RATE,
        channels=2, sample_width_bytes=2, midi_preroll_ms=PREROLL_MS,
        fixed_preroll_ms=PREROLL_MS, audio_bytes=emitted_bytes, pcm_sha256=pcm_hash.hexdigest(),
        arrangement_sha256=arrangement_sha256(arrangement),
        render_profile_fingerprint=render_profile_fingerprint,
        first_pcm_ms=round(first_pcm_ms or 0, 2),
        first_nonzero_offset_ms=round(first_nonzero_frame*1000/SAMPLE_RATE, 3) if first_nonzero_frame is not None else None,
        stems=stems, alignment_warnings=warnings,
        chunk_continuations_reattacked=any(stem['continuations'] for stem in stems),
        timing_ms={**{key: round(value, 2) for key, value in timings.items()},
                   'total': round((time.monotonic()-started)*1000, 2)})
