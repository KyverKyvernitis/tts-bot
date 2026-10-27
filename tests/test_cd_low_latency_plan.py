"""PCM equivalence, safe short plans and native singing publication contracts."""
from __future__ import annotations

import array
import copy
import hashlib
import io
import math
import random
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import wave

from cogs.tts.cd_stream import PCMStreamBuffer
from osaka_cd import pcm
from osaka_cd.arrangement import vowel_guide_score
from osaka_cd.lyrics import (automatic_render_plan, prepare_message_arrangement,
                            _short_accompaniment_chunks)
from osaka_cd.progressive import DecodedPCMCache, RenderError, stream_arrangement
from test_cd_progressive_render import renderer, template


def mono_samples(frames=44100):
    values = array.array('h', (round(11000*math.sin(number*.071))
                              for number in range(frames)))
    if sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes()


def unit_renderer(calls, *, rate=44100):
    base = renderer(calls, rate=rate)
    def render(**request):
        raw, metadata = base(**request)
        metadata.update(sample_rate=rate, channels=1, sample_width_bytes=2,
                        normalization_gain=1.0, voice_id=request['voice_id'],
                        reading_language=request['language'])
        return raw, metadata
    return render


def native_renderer(base, events, *, after_push=None, corrupt=False, callbacks=True):
    def render(*, on_pcm, **request):
        raw, metadata = base(**request)
        with wave.open(io.BytesIO(raw), 'rb') as source:
            data = source.readframes(source.getnframes())
        events.append(('begin', request['part_id']))
        if callbacks:
            on_pcm(b'', metadata)
            for start in range(0, len(data), 8820):
                on_pcm(data[start:start+8820], metadata)
                if after_push is not None:
                    after_push()
        events.append(('end', request['part_id']))
        if corrupt:
            raw = raw[:-2] + bytes((raw[-2] ^ 1, raw[-1]))
            metadata['sha256'] = hashlib.sha256(raw).hexdigest()
        return raw, metadata
    return render


class PCMConversionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and pcm.native_resampling_available(),
                         'FFmpeg and libswresample required')
    def test_44100_native_full_and_fragmented_stream_match_ffmpeg_exactly(self):
        source = mono_samples(44100+137)
        expected = subprocess.run(['ffmpeg', '-v', 'error', '-f', 's16le', '-ar', '44100',
            '-ac', '1', '-i', 'pipe:0', '-ar', '48000', '-ac', '1',
            '-f', 's16le', 'pipe:1'], input=source, capture_output=True, check=True).stdout
        for chunk_bytes in (len(source), 2, 8820, 32768):
            with self.subTest(chunk_bytes=chunk_bytes):
                converter = pcm.PCM16Resampler(44100)
                try:
                    actual = b''.join(converter.feed(source[start:start+chunk_bytes])
                                      for start in range(0, len(source), chunk_bytes))
                    actual += converter.finish()
                finally:
                    converter.close()
                self.assertEqual(actual, expected)

    def test_converter_lifecycle_and_invalid_samples_never_create_pcm(self):
        for value in (0, -1, True, 44100.0, 192000):
            with self.subTest(rate=value), self.assertRaises(pcm.PCMConversionError):
                pcm.PCM16Resampler(value)
        converter = pcm.PCM16Resampler(48000)
        for invalid in (b'\0', bytearray(b'\0\0'), None):
            with self.assertRaises(pcm.PCMConversionError):
                converter.feed(invalid)
        source = mono_samples(150)
        self.assertEqual(converter.feed(source), source)
        self.assertEqual(converter.finish(), b'')
        self.assertEqual(converter.finish(), b'')
        with self.assertRaises(pcm.PCMConversionError):
            converter.feed(source)

    def test_numpy_and_scalar_gain_mix_follow_identical_rounding(self):
        source = array.array('h', [-32768, -32767, -1001, -3, -1, 0, 1, 3, 1001, 32766, 32767])
        if sys.byteorder != 'little':
            source.byteswap()
        native = pcm._np
        for gain in (.6, .8, 1.0, 1.2):
            with self.subTest(gain=gain):
                with patch.object(pcm, '_np', None):
                    expected = pcm.scale_pcm16(source.tobytes(), gain)
                if native is not None:
                    with patch.object(pcm, '_np', native):
                        self.assertEqual(pcm.scale_pcm16(source.tobytes(), gain), expected)
                decoded = array.array('h', expected)
                if sys.byteorder != 'little':
                    decoded.byteswap()
                self.assertEqual(list(decoded), [max(-32768, min(32767, round(v/gain)))
                    for v in (-32768, -32767, -1001, -3, -1, 0, 1, 3, 1001, 32766, 32767)])
        rng = random.Random(41)
        parts = [(array.array('h', (rng.randrange(-30000, 30001) for _ in range(500))), .31, .17),
                 (array.array('h', (rng.randrange(-30000, 30001) for _ in range(500))), .49, .53)]
        with patch.object(pcm, '_np', None):
            expected = pcm.stereo_mix(parts, 11, 437)
        if native is not None:
            with patch.object(pcm, '_np', native):
                self.assertEqual(pcm.stereo_mix(parts, 11, 437), expected)


class ShortPlanTests(unittest.TestCase):
    def test_lead_and_articulated_harmony_ramp_through_1_2_4_8_seconds(self):
        value = template(notes=64)
        harmony = value['parts'][1]
        harmony['lyrics_source'] = 'midi'
        for note in harmony['notes']:
            note['lyric'] = 'a'
        text = ' '.join(['a'] * 64)
        adapted = prepare_message_arrangement(value, text)
        before = copy.deepcopy(adapted)
        plan = automatic_render_plan(adapted, text, first_request_ms=1000, request_ms=8000)
        for identity in ('lead', 'harmony'):
            selected = [(chunk['start_ms'], part) for chunk in plan['chunks']
                        for part in chunk['parts'] if part['id'] == identity]
            self.assertEqual([part['score']['duration_ms'] for _, part in selected[:4]],
                             [1000, 2000, 4000, 8000])
            actual = [dict(note, start_ms=start + note['start_ms'])
                      for start, part in selected for note in part['score']['notes']]
            original = next(part['notes'] for part in adapted['parts'] if part['id'] == identity)
            keys = ('midi_note', 'start_ms', 'duration_ms', 'velocity')
            self.assertEqual([{key: note[key] for key in keys} for note in actual],
                             [{key: note[key] for key in keys} for note in original])
        self.assertEqual(adapted, before)

    def test_long_holds_and_continuous_harmony_keep_their_original_anchors(self):
        text = 'a ' + '~ ' * 8 + 'fim'
        adapted = prepare_message_arrangement(template(), text)
        plan = automatic_render_plan(adapted, text, first_request_ms=1000, request_ms=8000)
        lead = [part for chunk in plan['chunks'] for part in chunk['parts'] if part['role'] == 'lead']
        self.assertGreater(lead[0]['score']['duration_ms'], 8000)
        self.assertFalse(any(part['adaptive_text'].startswith('~') for part in lead))
        harmony = adapted['parts'][1]
        chunks = _short_accompaniment_chunks(adapted, 1000, 8000)
        requests = [part for chunk in chunks for part in chunk['parts']]
        self.assertEqual(len(requests), 1, 'A latency target must not reattack a held harmony')
        expected = vowel_guide_score(dict(version=1, track_index=0, duration_ms=adapted['duration_ms'],
            lyrics_source='vowel', notes=harmony['notes']))
        self.assertEqual(requests[0]['score']['notes'], expected['notes'])
        self.assertFalse(requests[0]['lyrics_reattacked'])

    def test_native_fragments_do_not_masquerade_as_new_warm_generation_windows(self):
        stream = PCMStreamBuffer(30000)
        stream.begin_generation(0)
        stream.observe_generation(500, 3000, 12000)
        stream.append(b'\0\0\0\0' * (500*48), generation_ms=0)
        stream.observe_generation(500, 1, 11500)
        stream.append(b'\0\0\0\0' * (500*48), generation_ms=0)
        self.assertFalse(stream.ready)
        self.assertEqual(stream.generation_ratio, 3.001)
        self.assertEqual(stream.admission_snapshot()['generation_windows'], 1)
        stream.begin_generation(1000)
        stream.observe_generation(2000, 400, 4000)
        stream.append(b'\0\0\0\0' * (2000*48), generation_ms=0)
        self.assertEqual(stream.generation_ratio, .2)
        self.assertTrue(stream.ready)
        self.assertEqual(stream.admission_snapshot()['generation_windows'], 2)


class NativeCompositionTests(unittest.TestCase):
    def prepare(self):
        text = ' '.join(['a']*8)
        return text, prepare_message_arrangement(template(notes=8), text)

    @unittest.skipUnless(pcm.native_resampling_available(), 'Native resampler required')
    def test_native_prefix_precedes_final_wav_and_preserves_the_complete_two_voice_mix(self):
        text, adapted = self.prepare()
        baseline, output, calls, events, snapshots, observed = [], [], [], [], [], []
        first = stream_arrangement(adapted, text, unit_renderer([]), baseline.append,
            require_normalization=True, render_profile_fingerprint='voice-bank-1')
        def emit(raw):
            output.append(raw)
            snapshots.append(tuple(events))
        result = stream_arrangement(adapted, text, unit_renderer(calls), emit,
            require_normalization=True, render_profile_fingerprint='voice-bank-1',
            render_part_stream=native_renderer(unit_renderer(calls), events),
            on_generation=lambda *args: observed.append(args))
        self.assertEqual(b''.join(output), b''.join(baseline))
        self.assertEqual(result['pcm_sha256'], first['pcm_sha256'])
        self.assertEqual(result['sample_rate'], 48000)
        self.assertEqual(result['channels'], 2)
        self.assertTrue(any(('begin', 'harmony') in point and ('end', 'harmony') not in point
                            for point in snapshots))
        self.assertTrue(any(hint > 2000 for _, _, hint in observed),
                        'The remaining long physical harmony cannot be reported as a 100ms wait')
        self.assertEqual({call['voice_id'] for call in calls}, {'kasane-teto'})
        self.assertEqual({call['language'] for call in calls}, {'pt-BR'})
        self.assertEqual({stem['part_id'] for stem in result['stems']}, {'lead', 'harmony'})
        self.assertTrue(all(stem['normalization_gain'] == 1.0 for stem in result['stems']))

    def test_no_native_callbacks_falls_back_before_any_prefix_is_published(self):
        text, adapted = self.prepare()
        baseline, output, events, snapshots = [], [], [], []
        stream_arrangement(adapted, text, unit_renderer([]), baseline.append, require_normalization=True)
        def emit(raw):
            output.append(raw)
            snapshots.append(tuple(events))
        result = stream_arrangement(adapted, text, unit_renderer([]), emit,
            require_normalization=True,
            render_part_stream=native_renderer(unit_renderer([]), events, callbacks=False))
        self.assertEqual(b''.join(output), b''.join(baseline))
        self.assertTrue(all(('end', 'harmony') in point for point in snapshots))
        self.assertEqual(result['audio_bytes'], len(b''.join(output)))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg fallback required')
    def test_missing_native_resampler_waits_for_verified_wav_then_falls_back(self):
        text, adapted = self.prepare()
        baseline, output, events, snapshots = [], [], [], []
        stream_arrangement(adapted, text, unit_renderer([]), baseline.append, require_normalization=True)
        def emit(raw):
            output.append(raw)
            snapshots.append(tuple(events))
        with patch('osaka_cd.progressive.native_resampling_available', return_value=False):
            stream_arrangement(adapted, text, unit_renderer([]), emit,
                require_normalization=True,
                render_part_stream=native_renderer(unit_renderer([]), events))
        self.assertEqual(b''.join(output), b''.join(baseline))
        self.assertTrue(all(('end', 'harmony') in point for point in snapshots))

    @unittest.skipUnless(pcm.native_resampling_available(), 'Native resampler required')
    def test_mismatched_final_wav_is_rejected_and_never_cached(self):
        text, adapted = self.prepare()
        output, cache, events = [], DecodedPCMCache(), []
        with self.assertRaisesRegex(RenderError, 'streaming_stem_result_mismatch'):
            stream_arrangement(adapted, text, unit_renderer([]), output.append,
                require_normalization=True, memory_cache=cache, render_profile_fingerprint='bank',
                render_part_stream=native_renderer(unit_renderer([]), events, corrupt=True))
        self.assertTrue(output)
        calls = []
        stream_arrangement(adapted, text, unit_renderer(calls), lambda raw: None,
            require_normalization=True, memory_cache=cache, render_profile_fingerprint='bank')
        self.assertIn('harmony', {call['part_id'] for call in calls})

    def test_native_format_change_or_nonunit_gain_is_rejected(self):
        text, adapted = self.prepare()
        for failure in ('rate', 'gain'):
            with self.subTest(failure=failure):
                emitted = []
                def broken(*, on_pcm, **request):
                    raw, metadata = unit_renderer([])(**request)
                    if failure == 'gain':
                        metadata['normalization_gain'] = 1.2
                        on_pcm(b'', metadata)
                    else:
                        on_pcm(b'', metadata)
                        on_pcm(b'\0\0', {**metadata, 'sample_rate': 48000})
                    return raw, metadata
                with self.assertRaisesRegex(RenderError, 'streaming_stem_(format_changed|metadata)'):
                    stream_arrangement(adapted, text, unit_renderer([]), emitted.append,
                        require_normalization=True, render_part_stream=broken)
                self.assertFalse(emitted)

    @unittest.skipUnless(pcm.native_resampling_available(), 'Native resampler required')
    def test_consumer_cancellation_aborts_native_publication_without_future_words(self):
        text, adapted = self.prepare()
        calls, events = [], []
        with self.assertRaisesRegex(RenderError, 'render_cancelled'):
            stream_arrangement(adapted, text, unit_renderer(calls), lambda raw: False,
                require_normalization=True,
                render_part_stream=native_renderer(unit_renderer(calls), events))
        self.assertEqual([call['part_id'] for call in calls], ['lead', 'harmony'])
        self.assertEqual(events, [('begin', 'harmony')])


if __name__ == '__main__':
    unittest.main()
