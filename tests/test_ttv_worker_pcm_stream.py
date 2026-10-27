"""Sealed singing prefixes keep musical PCM while later fragments are pending."""
from __future__ import annotations

import array
import io
import hashlib
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

import test_ttv_latency_renderer as fixtures

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer import TetoRenderer
from teto_renderer.errors import TetoResourceError
import teto_renderer.renderer as implementation


def segment(text, **changes):
    return {'text': text, 'fade_in': False, 'fade_out': False,
            'hold': False, 'pause_after_ms': 0, **changes}


def score(*, gap=0, duration=500, source='message'):
    return {'version': 1, 'track_index': 0, 'duration_ms': duration * 3 + gap * 2,
            'lyrics_source': source, 'notes': [
        {'midi_note': pitch, 'start_ms': number * (duration + gap),
         'duration_ms': duration, 'velocity': velocity}
        for number, (pitch, velocity) in enumerate(((60, 100), (64, 80), (67, 127)))]}


def pcm(raw):
    with wave.open(io.BytesIO(raw), 'rb') as source:
        return source.readframes(source.getnframes())


class SingingPCMStreamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='worker-pcm-test-')
        self.addCleanup(self.temporary.cleanup)
        self.renderer = fixtures.RenderCancellationTests().singing_renderer(Path(self.temporary.name))

    def test_first_pcm_arrives_before_the_last_native_fragment_and_matches_final_wav(self):
        first_pcm = threading.Event()
        native = self.renderer._resample_note
        calls, received, headers = [], [], []

        def resample(**arguments):
            calls.append(arguments['note'])
            if len(calls) == 3:
                self.assertTrue(first_pcm.wait(2), 'last fragment cannot be needed for the first prefix')
            return native(**arguments)

        def consume(data, metadata):
            headers.append(metadata)
            if data:
                received.append(data)
                first_pcm.set()

        with patch.object(self.renderer, '_resample_note', side_effect=resample):
            raw, metadata = self.renderer.synthesize_performance_stream(
                'a a a', score=score(), emit_pcm=consume)
        self.assertTrue(first_pcm.is_set())
        self.assertEqual(len(calls), 3)
        self.assertEqual(b''.join(received), pcm(raw))
        self.assertGreater(len(received), 1)
        self.assertEqual(headers[0]['sample_rate'], 44100)
        self.assertEqual(headers[0]['normalization_gain'], 1.0)
        self.assertEqual(headers[0]['channels'], 1)
        self.assertEqual(headers[0]['expected_frames'] * 2, len(pcm(raw)))
        self.assertEqual(metadata['streamed_frames'] * 2, len(pcm(raw)))
        self.assertEqual(metadata['normalization_gain'], 1.0)

    def test_gaps_velocity_and_word_fades_are_identical_to_the_final_pcm(self):
        for gap in (0, 40, 180):
            with self.subTest(gap=gap):
                received = []
                raw, _ = self.renderer.synthesize_performance_stream(
                    'a a a', score=score(gap=gap), emit_pcm=lambda data, _: received.append(data),
                    segments=[segment('a', fade_in=True), segment('a', fade_out=True),
                              segment('a', fade_in=True, fade_out=True)])
                self.assertEqual(b''.join(received), pcm(raw))

    def test_sustained_vowel_keeps_one_native_render_and_its_entire_pitch_curve(self):
        received = []
        raw, metadata = self.renderer.synthesize_performance_stream('a', score=score(source='vowel'),
            emit_pcm=lambda data, _: received.append(data))
        self.assertEqual(metadata['resampler_calls'], 1)
        self.assertEqual(metadata['singing_nuclei'], 1)
        self.assertEqual(metadata['melisma_notes'], 2)
        self.assertEqual(b''.join(received), pcm(raw))

    def test_callback_failure_cancels_workers_before_releasing_physical_leases(self):
        def consume(data, _metadata):
            if data:
                raise RuntimeError('consumer closed')
        with self.assertRaisesRegex(RuntimeError, 'consumer closed'):
            self.renderer.synthesize_performance_stream('a a a', score=score(), emit_pcm=consume)
        self.assertEqual(self.renderer._cache._renders, 0)
        self.assertFalse(self.renderer._cache._protected)
        self.assertTrue(self.renderer._lock.acquire(blocking=False))
        self.renderer._lock.release()
        raw, _ = self.renderer.synthesize_performance('a', score=score(source='vowel'))
        self.assertTrue(raw.startswith(b'RIFF'))

    def test_false_consumer_result_is_cancellation_and_plain_speech_is_rejected(self):
        with self.assertRaises(TetoResourceError):
            self.renderer.synthesize_performance_stream('a', score=score(source='vowel'),
                                                       emit_pcm=lambda *_: False)
        with self.assertRaisesRegex(ValueError, 'partitura'):
            self.renderer.synthesize_performance_stream('a', emit_pcm=lambda *_: None)
        self.assertEqual(self.renderer._cache._renders, 0)

    def test_callback_metadata_cannot_mutate_later_frame_format(self):
        formats = []
        def consume(_data, metadata):
            formats.append(metadata['sample_rate'])
            metadata['sample_rate'] = 1
        _raw, final = self.renderer.synthesize_performance_stream('a a a', score=score(), emit_pcm=consume)
        self.assertTrue(formats)
        self.assertEqual(set(formats), {44100})
        self.assertEqual(final['sample_rate'], 44100)


class NativeDSPTests(unittest.TestCase):
    def test_optional_native_gain_matches_scalar_clipping_and_truncation(self):
        if implementation._numpy_dsp() is None:
            self.skipTest('optional NumPy not installed')
        randomizer = random.Random(19)
        source = array.array('h', [-32768, 32767, -1, 0, 1] +
                             [randomizer.randint(-32768, 32767) for _ in range(20000)])
        for gain in (.1, .6, .913, 1, 1.004, 1.2, 5):
            with patch.object(implementation, '_numpy_dsp', return_value=None):
                expected = TetoRenderer._apply_gain(source, gain)
            actual = TetoRenderer._apply_gain(source, gain)
            self.assertEqual(actual, expected, gain)

    def test_optional_native_velocity_preserves_rounding_for_ranges_and_overlaps(self):
        if implementation._numpy_dsp() is None:
            self.skipTest('optional NumPy not installed')
        source = array.array('h', [32767, -32768, 3, -5, 19, -37] * 15000)
        events = score(duration=600)['notes']
        events[1]['start_ms'] -= 40
        for offset in (0, 3800, 26000):
            with self.subTest(offset=offset):
                expected, actual = array.array('h', source), array.array('h', source)
                with patch.object(implementation, '_numpy_dsp', return_value=None):
                    TetoRenderer._apply_midi_velocity(expected, events, 23,
                                                    sample_offset=offset, total_frames=len(source) + offset)
                TetoRenderer._apply_midi_velocity(actual, events, 23,
                                                sample_offset=offset, total_frames=len(source) + offset)
                self.assertEqual(actual, expected)


class InProcessBridgeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='trusted-bridge-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.renderer = fixtures.RenderCancellationTests().singing_renderer(self.root)

    def test_verified_bridge_reuses_one_client_and_never_stages_or_spawns_python(self):
        bridge = self.root / 'resampler.py'
        bridge.write_text(
            'import array, wave\n'
            'class InProcessEsper:\n'
            ' def __init__(self, **settings): self.sources=[]; self.settings=settings\n'
            ' def synthesize(self, values, **controls):\n'
            '  self.sources.append(values[0])\n'
            '  with wave.open(values[1], "wb") as output:\n'
            '   output.setparams((1,2,44100,0,"NONE",""))\n'
            '   output.writeframes(array.array("h", [1200,-1200]*round(float(values[6])*22.05)).tobytes())\n'
            '  return {"ok":True}\n')
        digest = hashlib.sha256(bridge.read_bytes()).hexdigest()
        with patch.object(self.renderer, 'BACKGROUND_BRIDGE_SHA256', digest), \
             patch.object(implementation.subprocess, 'run', side_effect=AssertionError('launcher spawned')), \
             patch.object(implementation.shutil, 'copyfile', side_effect=AssertionError('source copied')):
            raw, metadata = self.renderer.synthesize_performance('a a a', score=score())
            client = self.renderer._esper_client_cache[1]
            self.assertIs(self.renderer._esper_inprocess_client(self.renderer._resampler_command()), client)
        self.assertEqual(client.sources, [str(self.root / 'bank/vowel.wav')] * 3)
        self.assertEqual(metadata['resampler_calls'], 3)
        self.assertTrue(pcm(raw))

    def test_custom_script_and_unknown_options_never_load_in_process(self):
        command = self.renderer._resampler_command()
        self.assertIsNone(self.renderer._esper_inprocess_client(command))
        bridge = self.root / 'resampler.py'
        with patch.object(self.renderer, 'BACKGROUND_BRIDGE_SHA256', hashlib.sha256(bridge.read_bytes()).hexdigest()):
            # Unknown command options remain the CLI's responsibility.
            self.renderer._bridge_identity_cache.clear()
            self.assertIsNone(self.renderer._esper_inprocess_client(command + ['--custom', 'value']))
        self.assertIsNone(self.renderer._esper_client_cache)


if __name__ == '__main__':
    unittest.main()
