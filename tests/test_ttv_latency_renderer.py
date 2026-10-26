"""Fragment cache bounds, PCM identity and cooperative render cancellation."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
import math
import contextlib
import importlib.util
import io
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.cache import FragmentCache
from teto_renderer.errors import TetoResourceError
from teto_renderer.renderer import TetoRenderer


class FragmentMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = FragmentCache(self.root / 'cache')
        self.source = self.root / 'source.wav'

    def put(self, key, data):
        self.source.write_bytes(data)
        return self.cache.put(self.cache.key(key), self.source)

    def test_many_fragment_writes_scan_once_and_replacements_account_exactly(self):
        for number in range(80):
            self.put(str(number), bytes([number]) * 80)
        self.assertEqual(self.cache.statistics()['cache_scans'], 1)
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 6400)
        self.put('0', b'x' * 100)
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 6420)
        self.put('0', b'y' * 50)
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 6370)
        self.assertEqual(self.cache.statistics()['cache_scans'], 1)

    def test_restart_accounts_existing_files_and_evicts_oldest(self):
        first = self.put('first', b'a' * 100)
        second = self.put('second', b'b' * 100)
        self.cache = FragmentCache(self.cache.root)
        self.cache.max_bytes = 250
        third = self.put('third', b'c' * 100)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 200)
        self.assertEqual(self.cache.statistics()['cache_evictions'], 1)

    def test_active_render_pins_fragments_and_bypasses_full_cache(self):
        self.cache.max_bytes = 160
        self.cache.begin_render()
        first = self.put('first', b'a' * 80)
        second = self.put('second', b'b' * 80)
        bypass = self.put('third', b'c' * 80)
        self.assertEqual(bypass, self.source)
        self.cache.prune()
        self.assertEqual(first.read_bytes(), b'a' * 80)
        self.assertEqual(second.read_bytes(), b'b' * 80)
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 160)
        self.cache.end_render()
        third = self.put('third', b'c' * 80)
        self.assertNotEqual(third, self.source)
        self.assertTrue(third.exists())
        self.assertLessEqual(self.cache.statistics()['cache_size_bytes'], 160)

    def test_cache_hit_is_pinned_until_composition_finishes(self):
        self.cache.max_bytes = 240
        first = self.put('first', b'a' * 80)
        second = self.put('second', b'b' * 80)
        self.cache.begin_render()
        self.assertEqual(self.cache.get(self.cache.key('first')), first)
        third = self.put('third', b'c' * 120)
        self.assertEqual(first.read_bytes(), b'a' * 80)
        self.assertFalse(second.exists())
        self.assertTrue(third.exists())
        self.cache.end_render()

    def test_oversized_fragment_uses_request_file_without_exceeding_limit(self):
        self.cache.max_bytes = 100
        returned = self.put('large', b'a' * 101)
        self.assertEqual(returned, self.source)
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 0)
        self.assertEqual(list(self.cache.root.rglob('*.wav')), [])

    def test_periodic_reconciliation_accounts_other_worker_files(self):
        self.put('first', b'a' * 80)
        external = self.cache.path_for(self.cache.key('external'))
        external.parent.mkdir(parents=True, exist_ok=True)
        external.write_bytes(b'b' * 120)
        later = self.cache._last_scan + self.cache.RECONCILE_SECONDS + 1
        with patch('teto_renderer.cache.time.monotonic', return_value=later):
            self.cache.begin_render()
            self.cache.end_render()
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 200)
        self.assertEqual(self.cache.statistics()['cache_scans'], 2)
        external.unlink()
        self.cache.prune()
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 80)

    def test_parallel_publications_are_complete_and_accounted(self):
        sources = []
        for number in range(32):
            source = self.root / f'{number}.wav'
            source.write_bytes(bytes([number]) * 100)
            sources.append(source)
        self.cache.begin_render()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda source: self.cache.put(self.cache.key(source.stem), source), sources))
        for number, result in enumerate(results):
            self.assertEqual(result.read_bytes(), bytes([number]) * 100)
        self.cache.end_render()
        self.assertEqual(self.cache.statistics()['cache_size_bytes'], 3200)
        self.assertEqual(self.cache.statistics()['cache_scans'], 1)
        self.assertEqual(list(self.cache.root.rglob('*.tmp')), [])


def original_velocity(samples, notes, preroll):
    previous_gain = previous_end = None
    for event in notes:
        first = max(0, round(TetoRenderer.SAMPLE_RATE * (preroll + event['start_ms']) / 1000))
        end = min(len(samples), round(TetoRenderer.SAMPLE_RATE * (preroll + event['start_ms'] + event['duration_ms']) / 1000))
        gain = 0.72 + 0.28 * math.sqrt(event['velocity'] / 127.0)
        incoming = previous_gain if previous_gain is not None and first - previous_end <= round(TetoRenderer.SAMPLE_RATE * 0.02) else gain
        smooth_frames = min(end - first, round(TetoRenderer.SAMPLE_RATE * 0.008))
        for number in range(first, end):
            offset = number - first
            ratio = math.sin(min(1.0, offset / max(1, smooth_frames - 1)) * math.pi / 2.0) ** 2
            samples[number] = round(samples[number] * (incoming + (gain - incoming) * ratio))
        previous_gain, previous_end = gain, end


class MidiVelocityTests(unittest.TestCase):
    def test_pcm_is_exact_for_ramps_gaps_overlaps_truncation_and_single_samples(self):
        randomizer = random.Random(42)
        source = array.array('h', [randomizer.randint(-32768, 32767) for _ in range(44100)])
        for preroll in (0, 0.01, 20, 100):
            for velocity in (0, 1, 32, 64, 100, 126, 127):
                notes = [
                    {'start_ms': 0, 'duration_ms': 100, 'velocity': velocity},
                    {'start_ms': 100, 'duration_ms': 1 / 44.1, 'velocity': 127 - velocity},
                    {'start_ms': 100 + 1 / 44.1, 'duration_ms': 7, 'velocity': velocity},
                    {'start_ms': 120, 'duration_ms': 8, 'velocity': 127 - velocity},
                    {'start_ms': 160, 'duration_ms': 70, 'velocity': velocity},
                    {'start_ms': 200, 'duration_ms': 600, 'velocity': 127 - velocity},
                    {'start_ms': 900, 'duration_ms': 500, 'velocity': velocity},
                    {'start_ms': 1500, 'duration_ms': 10, 'velocity': 127},
                ]
                expected, actual = array.array('h', source), array.array('h', source)
                original_velocity(expected, notes, preroll)
                TetoRenderer._apply_midi_velocity(actual, notes, preroll)
                self.assertEqual(actual, expected, (preroll, velocity))

    def test_transcendental_work_is_bounded_by_ramp_length(self):
        notes = [{'start_ms': 0, 'duration_ms': 1000, 'velocity': 100},
                 {'start_ms': 1000, 'duration_ms': 1000, 'velocity': 80}]
        samples = array.array('h', [1234] * 88200)
        with patch('teto_renderer.renderer.math.sin', wraps=math.sin) as sine:
            TetoRenderer._apply_midi_velocity(samples, notes, 0)
        self.assertLessEqual(sine.call_count, round(44100 * .008))


class RenderCancellationTests(unittest.TestCase):
    def singing_renderer(self, root):
        bank = root / 'bank'
        bank.mkdir()
        with wave.open(str(bank / 'vowel.wav'), 'wb') as output:
            output.setparams((1, 2, 44100, 0, 'NONE', ''))
            output.writeframes(array.array('h', [1000, -1000] * 22050).tobytes())
        (bank / 'oto.ini').write_text('\n'.join('vowel.wav=' + alias + ',0,30,-600,20,10'
                                             for alias in ('- a', 'a', 'A', 'a a', '- A')))
        script = root / 'resampler.py'
        script.write_text(
            'import array, sys, wave\n'
            'with wave.open(sys.argv[2], "wb") as output:\n'
            ' output.setparams((1,2,44100,0,"NONE",""))\n'
            ' output.writeframes(array.array("h", [1000, -1000] * max(1, round(float(sys.argv[7])*22.05))).tobytes())\n')
        return TetoRenderer(environment={
            'PHONE_WORKER_TETO_ENABLED': 'true',
            'PHONE_WORKER_TETO_VOICEBANK_DIR': str(bank),
            'PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR': str(bank),
            'PHONE_WORKER_TETO_VOICEBANK_MODE': 'english',
            'PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES': '1',
            'PHONE_WORKER_TETO_MIN_ALIASES': '1',
            'PHONE_WORKER_TETO_RESAMPLER_COMMAND': f'{sys.executable} {script}',
            'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(root / 'cache'),
            'PHONE_WORKER_TETO_LENGTH_MODE': 'total',
        })

    @staticmethod
    def score():
        return {'version': 1, 'track_index': 0, 'duration_ms': 600, 'lyrics_source': 'vowel',
                'notes': [{'midi_note': pitch, 'start_ms': number * 200, 'duration_ms': 200,
                           'velocity': velocity}
                          for number, (pitch, velocity) in enumerate(((60, 100), (64, 80), (67, 127)))]}

    def test_complete_singing_preserves_pcm_cache_and_returns_stage_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            renderer = self.singing_renderer(Path(temporary))
            with patch.object(TetoRenderer, '_apply_midi_velocity',
                              side_effect=lambda samples, notes, preroll, **_: original_velocity(samples, notes, preroll)):
                expected, first = renderer.synthesize_performance('a', score=self.score())
            actual, repeated = renderer.synthesize_performance('a', score=self.score())
            self.assertEqual(actual, expected)
            self.assertEqual(first['renderer_fingerprint'], repeated['renderer_fingerprint'])
            self.assertGreater(first['resampler_calls'], 0)
            self.assertEqual(repeated['resampler_calls'], 0)
            self.assertEqual(first['cache_scans'], 1)
            self.assertEqual(repeated['cache_scans'], 0)
            for field in ('compose_ms', 'postprocess_ms', 'cache_maintenance_ms', 'cache_size_bytes'):
                self.assertGreaterEqual(repeated[field], 0)
            self.assertFalse(renderer._cache._protected)
            self.assertEqual(renderer._cache._renders, 0)

    def test_cancelled_singing_releases_renderer_and_fragment_leases(self):
        with tempfile.TemporaryDirectory() as temporary:
            renderer = self.singing_renderer(Path(temporary))
            with self.assertRaisesRegex(TetoResourceError, 'cancelada'):
                renderer.synthesize_performance('a', score=self.score(), cancelled=lambda: True)
            self.assertEqual(renderer._cache._renders, 0)
            self.assertFalse(renderer._cache._protected)
            self.assertTrue(renderer._lock.acquire(blocking=False))
            renderer._lock.release()
            raw, meta = renderer.synthesize_performance('a', score=self.score())
            self.assertTrue(raw.startswith(b'RIFF'))
            self.assertGreater(meta['resampler_calls'], 0)

    def test_sigint_waits_for_bridge_cleanup_before_return(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready, cleaned = root / 'ready', root / 'cleaned'
            script = root / 'resampler.py'
            script.write_text(
                'import pathlib, signal, time\n'
                'signal.signal(signal.SIGINT, signal.default_int_handler)\n'
                f'pathlib.Path({str(ready)!r}).touch()\n'
                'try:\n'
                ' time.sleep(30)\n'
                'except KeyboardInterrupt:\n'
                ' time.sleep(.15)\n'
                f' pathlib.Path({str(cleaned)!r}).touch()\n')
            renderer = TetoRenderer(environment={'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(root / 'cache')})
            started = time.monotonic()
            with self.assertRaisesRegex(TetoResourceError, 'cancelada'):
                renderer._run_cancellable_resampler([sys.executable, str(script)], started + 5, ready.exists)
            self.assertTrue(cleaned.exists(), 'physical cleanup must finish before render lease is released')
            self.assertGreaterEqual(time.monotonic() - started, .15)
            self.assertLess(time.monotonic() - started, 3)

    def test_background_capability_rejects_custom_resampler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'custom.py'
            script.write_text('print("custom")\n')
            renderer = TetoRenderer(environment={
                'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(root / 'cache'),
                'PHONE_WORKER_TETO_RESAMPLER_COMMAND': f'{sys.executable} {script}',
            })
            self.assertFalse(renderer.supports_background_cancellation())

    def test_background_capability_recognizes_shipped_bridge(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = ROOT / 'deploy/esper-utau/termux/resampler.py'
            renderer = TetoRenderer(environment={
                'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': temporary,
                'PHONE_WORKER_TETO_RESAMPLER_COMMAND': f'{sys.executable} {bridge}',
            })
            self.assertTrue(renderer.supports_background_cancellation())


def load_bridge():
    path = ROOT / 'deploy/esper-utau/termux/resampler.py'
    specification = importlib.util.spec_from_file_location('ttv_latency_esper_bridge', path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class EsperBridgeCleanupTests(unittest.TestCase):
    def test_main_restores_sigint_inherited_as_ignored(self):
        bridge = ROOT / 'deploy/esper-utau/termux/resampler.py'
        code = (
            'import importlib.util, signal\n'
            'assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN\n'
            f'spec=importlib.util.spec_from_file_location("bridge", {str(bridge)!r})\n'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
            'try:\n'
            ' module.main(["--help"])\n'
            'except SystemExit:\n'
            ' pass\n'
            'assert signal.getsignal(signal.SIGINT) == signal.default_int_handler\n'
        )
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=5)
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_interrupted_or_failed_analysis_is_removed_before_source_unlock(self):
        bridge = load_bridge()
        raw = io.BytesIO()
        with wave.open(raw, 'wb') as output:
            output.setparams((1, 2, 44100, 0, 'NONE', ''))
            output.writeframes(array.array('h', [1000, -1000] * 4410).tobytes())
        pcm = raw.getvalue()
        for failure in (KeyboardInterrupt, RuntimeError):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bank, output = root / 'bank', root / 'output'
                bank.mkdir()
                output.mkdir()
                source, target = bank / 'source.wav', output / 'rendered.wav'
                source.write_bytes(pcm)
                original_frq = bank / 'source_wav.frq'
                original_frq.write_bytes(b'original-bank-frq')
                target.write_bytes(pcm)
                cache = root / 'runtime/cache'
                arguments = [str(source), str(target), 'C4', '100', '', '0', '200', '30', '-400', '100', '15', '!140', 'AA']
                directories = []
                original_lock = bridge.source_lock

                @contextlib.contextmanager
                def tracked_lock(path, deadline):
                    with original_lock(path, deadline):
                        try:
                            yield
                        finally:
                            for directory in directories:
                                self.assertFalse((directory / 'source.esp').exists())
                                self.assertFalse((directory / 'source_wav.frq').exists())

                def failing_guest(command, job, deadline):
                    directory = next(cache.glob('sources/*/source.wav')).parent
                    directories.append(directory)
                    (directory / 'source.esp').write_bytes(b'partial-analysis')
                    (directory / 'source_wav.frq').write_bytes(b'partial-frq')
                    raise failure('interrupted analysis')

                runtime = {'release': root / 'runtime/release'}
                with patch.object(bridge, 'runtime_info', return_value=runtime), \
                        patch.object(bridge, 'guest_command', return_value=['unused']), \
                        patch.object(bridge, 'source_lock', tracked_lock), \
                        patch.object(bridge, 'run_guest', failing_guest):
                    with self.assertRaises(failure):
                        bridge.synthesize(arguments, root=root / 'runtime', cache=cache,
                                          container='voicepeak-arm64', timeout=5)
                self.assertEqual(target.read_bytes(), pcm)
                self.assertEqual(source.read_bytes(), pcm)
                self.assertEqual(original_frq.read_bytes(), b'original-bank-frq')
                self.assertEqual(list((cache / 'jobs').iterdir()), [])

    def test_installer_accepts_previous_bridge_and_preserves_backup(self):
        path = ROOT / 'deploy/esper-utau/termux/setup.py'
        specification = importlib.util.spec_from_file_location('ttv_latency_esper_setup', path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        import hashlib
        self.assertIn('49fac2cae6396f3ee8dda7bc6e2ccbe2ae3835d48b9e25b6eeedffb5b955b43b',
                      module.KNOWN_LAUNCHER_SHA256)
        previous = b'#!/usr/bin/env python3\n# Previously accepted bridge fixture.\n'
        previous_digest = hashlib.sha256(previous).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / 'resampler.py'
            launcher.write_bytes(previous)
            updated = (ROOT / 'deploy/esper-utau/termux/resampler.py').read_bytes()
            with patch.object(module, 'KNOWN_LAUNCHER_SHA256', module.KNOWN_LAUNCHER_SHA256 | {previous_digest}):
                backup = module.atomic_launcher(launcher, updated, previous_digest)
            self.assertEqual(backup.read_bytes(), previous)
            self.assertEqual(launcher.read_bytes(), updated)


if __name__ == '__main__':
    unittest.main()
