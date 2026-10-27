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
import math
from pathlib import Path
import sys
import threading
import time

from .arrangement import arrangement_render_plan, arrangement_sha256, validate_arrangement, vowel_guide_score
from .lyrics import automatic_render_plan
from .pcm import PCM16Resampler, native_resampling_available, scale_pcm16, stereo_mix
from .render import (
    MAX_AUDIO_BYTES, MAX_STEM_BYTES, SAMPLE_RATE, RenderError, _cached_stem,
    _check, _digest, _ffmpeg, _message_allocation, _mix_controls, _preroll,
    _remember_stem, _render_metrics, _result_audio, _stem_key, _wav_info,
    _worker_stage_metrics,
)

PROGRESSIVE_VERSION = 'osaka-progressive-pcm-2'
PREROLL_MS = 350
DECODED_CACHE_POLICY = ':pcm48k-unit-normalization-v1'
FIRST_WINDOW_MS = 1000
LATER_WINDOW_MS = 8000


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
    automatic = arrangement.get('message_lyrics_mode') in {
        'automatic-lyrics-v1', 'automatic-lyrics-v2', 'automatic-lyrics-v3',
    }
    if automatic:
        plan = automatic_render_plan(arrangement, text, segments, language=language,
            first_request_ms=first_window_ms, request_ms=later_window_ms)
    else:
        # Old fixed-score callers keep their exact lyric allocation. Message
        # Preparation freezes the current automatic lyric allocation policy.
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
    info = _wav_info(audio)
    preroll = _preroll(audio, meta, score, wav_info=info)
    rate, _, pcm = info
    gain = _normalization_gain(meta)
    if require_normalization and gain is None:
        raise RenderError('stem_normalization_metadata_missing')
    if gain is not None and gain != 1.0:
        # The source timeline was already PCM16 before its request-wide peak
        # adjustment. Undo that one constant factor, retaining MIDI intensity
        # and word fades. This avoids a volume step when neighbouring requests
        # were normalized using different peaks; it introduces no new AGC.
        pcm = scale_pcm16(pcm, gain)
    if rate != SAMPLE_RATE:
        # High quality conversion is done once per decoded-cache miss. The
        # realtime mixer and consumers only see a single fixed PCM format.
        if native_resampling_available():
            converter = PCM16Resampler(rate, SAMPLE_RATE)
            try:
                pcm = converter.feed(pcm) + converter.finish()
            finally:
                converter.close()
        else:
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


def remember_decoded_stem(cache, call, profile, audio, meta, *, require_normalization=True):
    """Warm the same immutable PCM entry used by foreground singing."""
    if cache is None or not profile:
        return None
    key = _stem_key(call, profile) + DECODED_CACHE_POLICY
    saved = cache.get(key)
    if saved is not None and (not require_normalization or saved.normalization_gain is not None):
        return saved
    audio, meta = _result_audio((audio, meta), verify_audio=False)
    decoded = _decode(audio, meta, call['score'], require_normalization=require_normalization)
    cache.put(key, decoded)
    return decoded


class _StreamingStem:
    """Verify native callbacks and keep one resampling state for a whole stem."""
    def __init__(self, score, place, *, cancelled, deadline):
        self.score, self.place = score, place
        self.cancelled, self.deadline = cancelled, deadline
        self.rate = self.preroll = self.converter = None
        self.input_hash = hashlib.sha256()
        self.input_frames = self.output_frames = 0
        self.chunks = []
        self.placed = False

    def push(self, pcm, meta):
        _check(self.cancelled, self.deadline)
        if not isinstance(pcm, bytes) or len(pcm) % 2 or not isinstance(meta, dict):
            raise RenderError('invalid_streaming_stem_pcm')
        rate, preroll = meta.get('sample_rate'), meta.get('midi_preroll_ms')
        if (type(rate) is not int or not 8000 <= rate <= 96000
                or meta.get('channels', 1) != 1 or meta.get('sample_width_bytes', 2) != 2
                or _normalization_gain(meta) != 1.0
                or type(preroll) not in (int, float) or not math.isfinite(preroll)
                or not 0 <= preroll <= PREROLL_MS):
            raise RenderError('invalid_streaming_stem_metadata')
        if self.rate is None:
            self.rate, self.preroll = rate, preroll
            if rate == SAMPLE_RATE or native_resampling_available():
                self.converter = PCM16Resampler(rate, SAMPLE_RATE)
        elif rate != self.rate or preroll != self.preroll:
            raise RenderError('streaming_stem_format_changed')
        self.input_frames += len(pcm)//2
        if self.input_frames*2 > MAX_STEM_BYTES:
            raise RenderError('stem_size_limit')
        self.input_hash.update(pcm)
        if self.converter is not None:
            self._append(self.converter.feed(pcm))

    def _append(self, pcm):
        if not pcm:
            return
        preroll_frames = round(self.preroll*SAMPLE_RATE/1000)
        expected = round((self.score['duration_ms']+self.preroll)*SAMPLE_RATE/1000)
        # A final rounding sample belongs to the converter's flush, not to the
        # following musical request. Keep published samples inside this stem.
        useful = pcm[:max(0, expected-self.output_frames)*2]
        self.chunks.append(pcm)
        if useful:
            self.place(useful, self.output_frames, preroll_frames, expected)
            self.placed = True
        self.output_frames += len(pcm)//2

    def finish(self, audio, meta):
        if not self.input_frames:
            return _decode(audio, meta, self.score, require_normalization=True)
        info = _wav_info(audio)
        rate, frames, source_pcm = info
        preroll = _preroll(audio, meta, self.score, wav_info=info)
        if (rate != self.rate or frames != self.input_frames
                or abs(preroll-self.preroll) > 1.6*1000/rate
                or _normalization_gain(meta) != 1.0
                or hashlib.sha256(source_pcm).digest() != self.input_hash.digest()):
            raise RenderError('streaming_stem_result_mismatch')
        if self.converter is None:
            # An older FFmpeg installation without a public shared-library ABI
            # keeps the complete-file fallback, with no partial audio emitted.
            return _decode(audio, meta, self.score, require_normalization=True)
        self._append(self.converter.finish())
        pcm = b''.join(self.chunks)
        expected = round((self.score['duration_ms']+preroll)*SAMPLE_RATE/1000)
        if abs(len(pcm)//2-expected) > 2:
            raise RenderError('invalid_resampled_stem_duration')
        pcm = pcm[:expected*2].ljust(expected*2, b'\0')
        return DecodedStem(pcm, round(preroll*SAMPLE_RATE/1000),
                           hashlib.sha256(audio).hexdigest(), 1.0)

    def close(self):
        if self.converter is not None:
            self.converter.close()


def stream_arrangement(arrangement, text, render_part, emit_pcm, *, segments=None,
                       render_profile_fingerprint='', language='pt-BR',
                       memory_cache=None, cache_dir=None, first_window_ms=FIRST_WINDOW_MS,
                       later_window_ms=LATER_WINDOW_MS, cancelled=None, timeout_seconds=1200,
                       require_normalization=False, on_generation=None, render_part_stream=None,
                       on_generation_boundary=None):
    """Emit complete synchronized stereo PCM windows as soon as they are final.

    ``emit_pcm(bytes)`` runs in this producer thread. It may block for bounded
    backpressure or raise to cancel. The caller retains the physical synthesis
    lease until this function exits, including after a consumer cancellation.
    Output is PCM16 little endian, stereo, 48 kHz. Initial fixed 350 ms preroll
    removes the need to discover the largest preroll in the entire song.
    """
    started = time.monotonic()
    generation_started = started
    arrangement = validate_arrangement(arrangement)
    if not isinstance(text, str) or language not in {'pt-BR', 'en-US'}:
        raise RenderError('invalid_render_text_or_language')
    if (on_generation is not None and not callable(on_generation)
            or render_part_stream is not None and not callable(render_part_stream)
            or on_generation_boundary is not None and not callable(on_generation_boundary)):
        raise RenderError('invalid_progressive_callback')
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

    def publish(end, next_window_ms=0):
        nonlocal cursor, emitted_bytes, first_pcm_ms, first_nonzero_frame, generation_started
        if end <= cursor:
            return
        now = time.monotonic()
        if on_generation is not None:
            on_generation((end-cursor)*1000/SAMPLE_RATE,
                          max(0.0, (now-generation_started)*1000), next_window_ms)
        generation_started = now
        # Bounded publications allow prompt cancellation even for a long final
        # sustain, and let the consumer slice native Discord 20 ms frames.
        while cursor < end:
            _check(cancelled, deadline)
            stop = min(end, cursor + SAMPLE_RATE // 2)
            mix_started = time.monotonic()
            items = [(timelines[identity], *gains[identity]) for identity in controls]
            output = stereo_mix(items, cursor, stop)
            if first_nonzero_frame is None:
                first_nonzero_frame = next((cursor+number//2 for number in range(0,len(output),2)
                    if output[number] or output[number+1]), None)
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
        if on_generation_boundary is not None and (number == 0 or requests[number-1]['start_ms'] != start_ms):
            on_generation_boundary(start_ms)
        identity = call['part_id']
        original_end = previous_end[identity]
        next_start = requests[number+1]['start_ms'] if number+1 < len(requests) else None
        seal_limit = (total_frames if next_start is None else
                      min(total_frames, round(next_start*SAMPLE_RATE/1000)))
        future_window_ms = 0
        if next_start is not None and next_start > start_ms:
            remaining = requests[number+1:]
            next_calls = [entry for entry in remaining if entry['start_ms'] == next_start]
            following_start = next((entry['start_ms'] for entry in remaining
                                    if entry['start_ms'] > next_start), None)
            next_output_ms = ((arrangement['duration_ms'] + PREROLL_MS if following_start is None
                               else following_start) - next_start)
            future_window_ms = max(next_output_ms,
                                   max(entry['call']['score']['duration_ms'] for entry in next_calls))
        streamed = None

        def place(pcm, relative, preroll_frames, expected, *, partial=False):
            offset = round((PREROLL_MS + start_ms)*SAMPLE_RATE/1000)-preroll_frames
            first = offset+relative
            if first < cursor or first < 0:
                raise RenderError('progressive_preroll_crossed_published_audio')
            samples = array.array('h', pcm)
            if sys.byteorder != 'little':
                samples.byteswap()
            samples = samples[:max(0,total_frames-first)]
            timeline = timelines[identity]
            total_overlap = max(0,min(expected,original_end-offset))
            overlap = max(0,min(len(samples),total_overlap-relative))
            denominator = max(1,total_overlap-1)
            for index in range(overlap):
                ratio = (relative+index)/denominator
                samples[index] = round(timeline[first+index]*(1-ratio)+samples[index]*ratio)
            timeline[first:first+len(samples)] = samples
            previous_end[identity] = max(previous_end[identity],first+len(samples))
            if partial:
                # Only the final simultaneous stem can seal the mix. Every
                # other voice is already complete, and future sources retain
                # their consonantal overlap behind the next request's clock.
                # A prefix can be sealed while the current physical stem is
                # still rendering beyond it. That remaining work must finish
                # before the next chronological lead can start. Predict that
                # complete wait, rather than advertising another 100ms packet.
                pending_native_ms = max(0, expected-relative-len(samples))*1000/SAMPLE_RATE
                publish(min(seal_limit,first+len(samples)),
                        pending_native_ms + future_window_ms)
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
            if hit:
                audio, meta = saved
            elif render_part_stream is not None and (next_start is None or next_start>start_ms):
                streamed = _StreamingStem(call['score'],
                    lambda pcm, relative, preroll, expected: place(
                        pcm,relative,preroll,expected,partial=True),
                    cancelled=cancelled,deadline=deadline)
                try:
                    audio, meta = _result_audio(render_part_stream(on_pcm=streamed.push,**call),
                                               verify_audio=False)
                    decoded = streamed.finish(audio,meta)
                finally:
                    streamed.close()
            else:
                audio, meta = _result_audio(render_part(**call),verify_audio=False)
            if not hit:
                timings['synthesize'] += (time.monotonic()-synth_started)*1000
                metrics, worker_timing = _render_metrics(meta), _worker_stage_metrics(meta)
            _check(cancelled, deadline)
            if len(audio) > MAX_STEM_BYTES:
                raise RenderError('stem_size_limit')
            decode_started = time.monotonic()
            if decoded is None:
                decoded = _decode(audio, meta, call['score'], require_normalization=require_normalization)
            timings['decode'] += (time.monotonic()-decode_started)*1000
            if memory_cache is not None and render_profile_fingerprint:
                memory_cache.put(decoded_key, decoded)
            if cache is not None and not hit:
                pending_disk.append((key, audio, meta, decoded.preroll_frames*1000/SAMPLE_RATE))
        else:
            timings['cache_lookup'] += (time.monotonic()-lookup_started)*1000
        if streamed is None or not streamed.placed:
            place(decoded.pcm,0,decoded.preroll_frames,len(decoded.pcm)//2)
        stems.append(dict(part_id=identity, voice_id=call['voice_id'], start_ms=start_ms,
            score_sha256=_digest(call['score']),
            key=key, cache_hit=hit, cache_kind=hit_kind, render_sha256=decoded.raw_sha256,
            render_metrics=metrics, worker_timing_ms=worker_timing,
            normalization_gain=decoded.normalization_gain,
            continuations=request['continuations']))
        # Every future source begins no earlier than its request's clock start:
        # fixed350ms - actual_preroll>=0. Retain the unsealed overlap in RAM.
        if next_start is None:
            publish(total_frames)
        elif next_start > start_ms:
            publish(min(total_frames, round(next_start*SAMPLE_RATE/1000)), future_window_ms)
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
