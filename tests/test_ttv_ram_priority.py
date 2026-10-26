"""Decoded hot PCM stays immutable and follows real file versions."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
import os
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer import TetoRenderer
from teto_renderer.errors import TetoSynthesisError


def wav(path, samples, *, rate=44100, channels=1):
    with wave.open(str(path), 'wb') as output:
        output.setparams((channels, 2, rate, 0, 'NONE', ''))
        output.writeframes(array.array('h', samples).tobytes())


class DecodedPCMCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.renderer = TetoRenderer(environment={
            'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(self.root / 'fragments'),
            'PHONE_WORKER_TETO_PCM_CACHE_MB': '256',
        })
        self.path = self.root / 'sample.wav'
        wav(self.path, [1000, -1000, 2000, -2000])

    def test_hot_read_skips_wave_decode_and_returns_independent_arrays(self):
        with patch('teto_renderer.renderer.wave.open', wraps=wave.open) as decoded:
            first = self.renderer._read_samples(self.path)
            first[0] = 123
            second = self.renderer._read_samples(self.path)
        self.assertEqual(decoded.call_count, 1)
        self.assertEqual(second.tolist(), [1000, -1000, 2000, -2000])
        self.assertIsNot(first, second)
        self.assertEqual(self.renderer._pcm_cache_statistics(), {
            'pcm_cache_hits': 1, 'pcm_cache_misses': 1, 'pcm_cache_bytes': 8})

    def test_same_size_and_restored_mtime_cannot_reuse_changed_pcm(self):
        self.renderer._read_samples(self.path)
        before = self.path.stat()
        wav(self.path, [500, 600, 700, 800])
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        actual = self.renderer._read_samples(self.path)
        self.assertEqual(actual.tolist(), [500, 600, 700, 800])
        self.assertEqual(self.renderer._pcm_cache_statistics()['pcm_cache_misses'], 2)
        self.assertEqual(self.renderer._pcm_cache_statistics()['pcm_cache_bytes'], 8)

    def test_deleted_and_replaced_path_invalidates_hot_entry(self):
        self.renderer._read_samples(self.path)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.renderer._read_samples(self.path)
        self.assertEqual(self.renderer._pcm_cache_statistics()['pcm_cache_bytes'], 0)
        wav(self.path, [7, 8])
        self.assertEqual(self.renderer._read_samples(self.path).tolist(), [7, 8])

    def test_lru_evicts_without_mutating_audio_and_enforces_byte_budget(self):
        self.renderer._pcm_cache_limit = 16
        second, third = self.root / 'second.wav', self.root / 'third.wav'
        wav(second, [2] * 4)
        wav(third, [3] * 4)
        self.renderer._read_samples(self.path)
        self.renderer._read_samples(second)
        self.renderer._read_samples(self.path)  # First becomes most recent.
        self.renderer._read_samples(third)
        self.assertEqual(self.renderer._pcm_cache_statistics()['pcm_cache_bytes'], 16)
        self.assertNotIn(str(second.absolute()), self.renderer._pcm_cache)
        self.assertIn(str(self.path.absolute()), self.renderer._pcm_cache)

    def test_zero_budget_and_oversized_pcm_do_not_retain_data(self):
        for limit in (0, 7):
            with self.subTest(limit=limit):
                self.renderer._pcm_cache_limit = limit
                self.assertEqual(self.renderer._read_samples(self.path).tolist(), [1000, -1000, 2000, -2000])
                self.assertEqual(self.renderer._pcm_cache_statistics()['pcm_cache_bytes'], 0)
                self.assertFalse(self.renderer._pcm_cache)

    def test_hot_cache_does_not_hide_corrupted_or_truncated_replacement(self):
        self.renderer._read_samples(self.path)
        wav(self.path, [1, 2, 3, 4], rate=22050)
        with self.assertRaises(TetoSynthesisError):
            self.renderer._read_samples(self.path)
        wav(self.path, [1, 2, 3, 4])
        self.path.write_bytes(self.path.read_bytes()[:-2])
        with self.assertRaisesRegex(TetoSynthesisError, 'truncado'):
            self.renderer._read_samples(self.path)
        self.assertFalse(self.renderer._pcm_cache)

    def test_concurrent_decodes_keep_one_accounted_immutable_copy(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.renderer._read_samples(self.path), range(32)))
        self.assertTrue(all(samples.tolist() == [1000, -1000, 2000, -2000] for samples in results))
        self.assertEqual(self.renderer._pcm_cache_statistics()['pcm_cache_bytes'], 8)
        self.assertEqual(len(self.renderer._pcm_cache), 1)
        results[0][0] = 99
        self.assertEqual(results[-1][0], 1000)

    def test_fragment_hits_preserve_file_version_and_live_lru_across_scan(self):
        cache = self.renderer._cache
        key = cache.key('hot-fragment')
        cached = cache.put(key, self.path)
        self.renderer._read_samples(cached)
        before = cached.stat()
        self.assertEqual(cache.get(key), cached)
        recent = cache._entries[cached][1]
        after = cached.stat()
        self.assertEqual((before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_mtime_ns, after.st_ctime_ns))
        self.renderer._read_samples(cached)
        self.assertEqual(self.renderer._pcm_cache_statistics()['pcm_cache_hits'], 1)
        cache._ensure_index(force=True)
        self.assertGreaterEqual(cache._entries[cached][1], recent)


class InitialWindowPriorityTests(unittest.TestCase):
    def test_preutterance_start_prioritizes_groups_and_stays_serial_per_source(self):
        groups = {'late-source': [(0, 'late', 'a'), (2, 'early-same-source', 'a')],
                  'first-source': [(1, 'first', 'b')]}
        placements = [{'start_ms': 1000}, {'start_ms': -80}, {'start_ms': 20}]
        ordered = TetoRenderer._ordered_source_groups(groups, placements)
        self.assertEqual([[entry[0] for entry in group] for group in ordered], [[1], [2, 0]])
        self.assertEqual(len(ordered), len(groups))
        self.assertEqual(groups['late-source'][0][0], 0, 'planning must not mutate source groups')

    def test_serial_voice_uses_lexical_start_when_no_cvvc_placements(self):
        groups = {'late': [(4, None, None)], 'early': [(2, None, None), (0, None, None)]}
        self.assertEqual([[entry[0] for entry in group]
                          for group in TetoRenderer._ordered_source_groups(groups)], [[0, 2], [4]])

    def test_complete_singing_cache_preserves_pcm_and_voice_identity(self):
        from test_ttv_latency_renderer import RenderCancellationTests
        fixture = RenderCancellationTests()
        with tempfile.TemporaryDirectory() as directory:
            renderer = fixture.singing_renderer(Path(directory))
            first, meta = renderer.synthesize_performance('a', score=fixture.score(), timeout_seconds=10)
            second, cached = renderer.synthesize_performance('a', score=fixture.score(), timeout_seconds=10)
            self.assertEqual(first, second)
            self.assertEqual(meta['renderer_fingerprint'], cached['renderer_fingerprint'])
            self.assertGreater(meta['pcm_cache_misses'], 0)
            self.assertGreater(cached['pcm_cache_hits'], 0)
            self.assertEqual(cached['pcm_cache_misses'], 0)
            self.assertEqual(cached['resampler_calls'], 0)
            self.assertEqual(cached['pcm_cache_bytes'], meta['pcm_cache_bytes'])
            self.assertTrue(math.isfinite(meta['normalization_gain']))
            self.assertEqual(meta['normalization_gain'], 1.2,
                             'report effective clamped gain, not the requested 3.6')
            self.assertEqual(cached['normalization_gain'], meta['normalization_gain'])
            self.assertFalse(hasattr(renderer._reading_context, 'render_metrics'))


if __name__ == '__main__':
    unittest.main()
