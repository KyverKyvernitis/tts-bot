"""Verificação offline do benchmark e da limpeza de processos no timeout."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "tts_local_benchmark", ROOT / "scripts/benchmark-tts-local.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class SummaryTests(unittest.TestCase):
    def test_percentiles_exclude_non_timing_fields(self):
        result = benchmark.summarize([
            {"first_pcm_ms": 10, "pcm_bytes": 3840},
            {"first_pcm_ms": 20, "pcm_bytes": 7680},
            {"first_pcm_ms": 40, "pcm_bytes": 11520},
        ])
        self.assertEqual(result, {"first_pcm_ms": {"n": 3, "p50_ms": 20, "p95_ms": 38}})


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg indisponível")
class FFmpegBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.fixture = benchmark.make_fixture(cls.ffmpeg, .5, 5)

    def args(self, **overrides):
        defaults = dict(ffmpeg=self.ffmpeg, timeout_seconds=3,
                        initial_delay_ms=10, chunk_interval_ms=2, chunk_bytes=512)
        return SimpleNamespace(**(defaults | overrides))

    async def test_progressive_decode_keeps_pcm_identical_to_complete_input(self):
        filters = benchmark.load_effect_filters(ROOT)
        # Slowed + Reverb exerce resampling, cauda de echo e o último frame.
        effect_filter = filters["slowed3_reverb3"]
        complete = await benchmark.ffmpeg_trial(self.args(), self.fixture, effect_filter,
                                               progressive=False, probesize=2048)
        progressive = await benchmark.ffmpeg_trial(self.args(), self.fixture, effect_filter,
                                                  progressive=True, probesize=512)
        self.assertEqual(complete["pcm_sha256"], progressive["pcm_sha256"])
        self.assertEqual(complete["pcm_bytes"], progressive["pcm_bytes"])
        self.assertGreater(progressive["first_pcm_frame_ms"], 0)

    async def test_timeout_terminates_process_and_reader_tasks(self):
        processes = []
        original_spawn = asyncio.create_subprocess_exec

        async def record_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        initial_tasks = asyncio.all_tasks()
        with patch.object(benchmark.asyncio, "create_subprocess_exec", record_spawn):
            with self.assertRaises(asyncio.TimeoutError):
                await benchmark.ffmpeg_trial(
                    self.args(timeout_seconds=.1, initial_delay_ms=1000), self.fixture, "",
                    progressive=True, probesize=512)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(asyncio.all_tasks() - initial_tasks)


if __name__ == "__main__":
    unittest.main()
