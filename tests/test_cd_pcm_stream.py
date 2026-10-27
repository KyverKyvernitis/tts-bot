"""RAM PCM buffering, first-audio admission and audio-thread lifecycle."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest

from cogs.tts.cd_stream import PCMStreamBuffer, PCMStreamCancelled


def pcm(milliseconds, sample=b'\x34\x12\x78\x56'):
    return sample * (milliseconds * 48)


class PCMFrameTests(unittest.TestCase):
    def test_chronological_chunks_emit_exact_frames_and_pad_final_tail_once(self):
        stream = PCMStreamBuffer(45)
        first, second = pcm(13), pcm(32, b'\xaa\x01\x20\xff')
        stream.append(first)
        stream.append(second)
        stream.finish()
        output = [stream.read(), stream.read(), stream.read()]
        self.assertEqual([len(frame) for frame in output], [3840] * 3)
        self.assertEqual(b''.join(output), first + second + b'\0' * (15 * 192))
        self.assertEqual(stream.read(), b'')
        self.assertEqual(stream.read(), b'')
        self.assertEqual(stream.produced_ms, 45)
        self.assertEqual(stream.playback_ms, 45)
        self.assertEqual(stream.buffered_ms, 0)
        self.assertEqual(stream.frames_read, 3)

    def test_exact_frame_eof_has_no_extra_silence(self):
        stream = PCMStreamBuffer(20)
        stream.append(pcm(20))
        stream.finish()
        self.assertEqual(stream.read(), pcm(20))
        self.assertEqual(stream.read(), b'')

    def test_partial_live_frame_waits_instead_of_padding_or_ending(self):
        stream = PCMStreamBuffer(20, read_timeout_seconds=2)
        stream.append(pcm(5))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stream.read)
            time.sleep(.02)
            self.assertFalse(future.done())
            stream.append(pcm(15, b'\x01\x00\x02\x00'))
            self.assertEqual(future.result(timeout=1), pcm(5) + pcm(15, b'\x01\x00\x02\x00'))
        stream.finish()
        self.assertEqual(stream.read(), b'')
        self.assertEqual(stream.admission_snapshot()['read_waits'], 1)
        self.assertGreater(stream.admission_snapshot()['read_wait_ms'], 0)

    def test_later_error_is_propagated_after_first_played_frame(self):
        stream = PCMStreamBuffer(100)
        stream.append(pcm(40))
        self.assertEqual(len(stream.read()), 3840)
        failure = RuntimeError('physical render failed')
        stream.finish(failure)
        with self.assertRaisesRegex(RuntimeError, 'physical render failed'):
            stream.read()
        self.assertIs(stream.error, failure)
        self.assertTrue(stream.stop.is_set())

    def test_cleanup_unblocks_reader_and_releases_ram(self):
        stream = PCMStreamBuffer(100, read_timeout_seconds=2)
        stream.append(pcm(5))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stream.read)
            time.sleep(.02)
            self.assertFalse(future.done())
            stream.cleanup()
            self.assertEqual(future.result(timeout=1), b'')
        self.assertTrue(stream.closed and stream.stop.is_set())
        self.assertEqual(stream.buffered_bytes, 0)
        with self.assertRaises(PCMStreamCancelled):
            stream.append(pcm(20))
        stream.cleanup()

    def test_cancel_unblocks_reader_and_stops_producer(self):
        stream = PCMStreamBuffer(100, read_timeout_seconds=2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stream.read)
            time.sleep(.02)
            stream.cancel()
            with self.assertRaises(PCMStreamCancelled):
                future.result(timeout=1)
        self.assertTrue(stream.done and stream.cancelled and stream.stop.is_set())

    def test_stall_surfaces_an_error_instead_of_silent_eof(self):
        stream = PCMStreamBuffer(100, read_timeout_seconds=.01)
        with self.assertRaisesRegex(TimeoutError, 'stalled'):
            stream.read()
        self.assertTrue(stream.done and stream.stop.is_set())
        self.assertIsInstance(stream.error, TimeoutError)

    def test_memory_cap_errors_without_discarding_or_overcounting_audio(self):
        stream = PCMStreamBuffer(100, max_bytes=3840)
        stream.append(pcm(20))
        with self.assertRaises(BufferError):
            stream.append(pcm(1))
        self.assertEqual(stream.produced_bytes, 3840)
        self.assertEqual(stream.buffered_bytes, 3840)
        with self.assertRaises(BufferError):
            stream.read()
        self.assertTrue(stream.stop.is_set())

    def test_memory_cap_counts_unread_audio_not_lifetime_production(self):
        stream = PCMStreamBuffer(40, max_bytes=3840)
        stream.append(pcm(20))
        stream.read()
        stream.append(pcm(20))
        stream.finish()
        self.assertEqual(stream.produced_bytes, 7680)
        self.assertEqual(stream.buffered_bytes, 3840)
        self.assertEqual(stream.read(), pcm(20))
        self.assertEqual(stream.read(), b'')

    def test_mutable_input_is_owned_and_non_silent_frame_time_is_precise(self):
        now = [1.0]
        stream = PCMStreamBuffer(40, clock=lambda: now[0])
        mutable = bytearray(pcm(20))
        stream.append(pcm(20, b'\0' * 4))
        stream.append(mutable)
        mutable[:] = b'\0' * len(mutable)
        stream.finish()
        now[0] = 2
        self.assertEqual(stream.read(), pcm(20, b'\0' * 4))
        self.assertEqual(stream.first_frame_at, 2)
        self.assertIsNone(stream.first_non_silent_at)
        now[0] = 3
        self.assertEqual(stream.read(), pcm(20))
        self.assertEqual(stream.first_non_silent_at, 3)

    def test_lazy_source_adapter_is_real_subclass_and_forwards_cleanup(self):
        class AudioSource:
            pass
        stream = PCMStreamBuffer(20)
        stream.append(pcm(20))
        stream.finish()
        source = stream.as_audio_source(AudioSource)
        self.assertIsInstance(source, AudioSource)
        self.assertFalse(source.is_opus())
        self.assertEqual(source.read(), pcm(20))
        self.assertEqual(source.produced_ms, 20)
        source.cleanup()
        self.assertTrue(stream.closed)

    def test_invalid_parameters_and_samples_never_allocate_audio(self):
        for value in (0, -1, True, float('nan'), float('inf'), 10 ** 1000):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                PCMStreamBuffer(value)
        stream = PCMStreamBuffer(100)
        with self.assertRaises(ValueError):
            stream.append(b'\0')
        with self.assertRaises(ValueError):
            stream.append(pcm(20), generation_ms=True)
        self.assertEqual(stream.produced_bytes, 0)


class AdaptiveAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_slices_share_one_complete_window_measurement(self):
        stream = PCMStreamBuffer(10000)
        stream.observe_generation(1000, 400, next_window_ms=2000)
        stream.append(pcm(500), generation_ms=0)
        self.assertFalse(stream.ready)
        self.assertEqual(stream.required_prebuffer_ms, 1000)
        stream.append(pcm(500), generation_ms=0)
        await stream.wait_ready(timeout=.1)
        observed = stream.admission_snapshot()
        self.assertEqual(observed['generation_windows'], 1)
        self.assertEqual(observed['initial_generation_ms'], 400)
        self.assertEqual(observed['generation_ms'], 400)
        self.assertEqual(observed['sustained_generation_ratio'], .4)

    async def test_cold_start_is_not_extrapolated_after_a_real_steady_window(self):
        stream = PCMStreamBuffer(30000)
        stream.observe_generation(1000, 5000, next_window_ms=2000)
        stream.append(pcm(1000), generation_ms=0)
        self.assertFalse(stream.ready, 'One cold sample cannot establish sustained speed')
        stream.observe_generation(2000, 400, next_window_ms=4000)
        stream.append(pcm(2000), generation_ms=0)
        await stream.wait_ready(timeout=.1)
        self.assertEqual(stream.generation_ms, 5400)
        self.assertEqual(stream.generation_ratio, .2)
        self.assertEqual(stream.required_prebuffer_ms, 1000)
        self.assertFalse(stream.done)

    async def test_fast_producer_still_covers_latency_of_a_long_next_window(self):
        stream = PCMStreamBuffer(10000)
        stream.observe_generation(1000, 400, next_window_ms=8000)
        stream.append(pcm(1000), generation_ms=0)
        self.assertFalse(stream.ready, 'Fast average throughput does not make an 8s block arrive early')
        self.assertEqual(stream.required_prebuffer_ms, 3400)
        stream.observe_generation(8000, 3200, next_window_ms=1000)
        stream.append(pcm(8000), generation_ms=0)
        await stream.wait_ready(timeout=.1)

    async def test_slow_windowed_producer_keeps_deficit_and_block_arrival_reserve(self):
        stream = PCMStreamBuffer(10000)
        for number in range(3):
            stream.observe_generation(2000, 4000, next_window_ms=2000)
            stream.append(pcm(2000), generation_ms=0)
            self.assertFalse(stream.ready, number)
        self.assertEqual(stream.required_prebuffer_ms, 6200)
        stream.observe_generation(2000, 4000, next_window_ms=2000)
        stream.append(pcm(2000), generation_ms=0)
        await stream.wait_ready(timeout=.1)
        self.assertEqual(stream.produced_ms, 8000)
        self.assertFalse(stream.done)

    def test_window_observation_rejects_invalid_numbers_before_mutating_state(self):
        stream = PCMStreamBuffer(10000)
        for bad in (0, -1, True, float('nan'), float('inf')):
            with self.subTest(audio_ms=bad), self.assertRaises(ValueError):
                stream.observe_generation(bad, 20)
        for bad in (-1, True, float('nan'), float('inf')):
            with self.subTest(generation_ms=bad), self.assertRaises(ValueError):
                stream.observe_generation(1000, bad)
            with self.subTest(next_window_ms=bad), self.assertRaises(ValueError):
                stream.observe_generation(1000, 20, next_window_ms=bad)
        self.assertEqual(stream.generation_ms, 0)
        self.assertEqual(stream.admission_snapshot()['generation_windows'], 0)

    async def test_fast_producer_can_start_before_completion(self):
        stream = PCMStreamBuffer(10000, prebuffer_ms=500, safety_ms=250)
        stream.append(pcm(1000), generation_ms=400)
        await stream.wait_ready(timeout=.1)
        self.assertTrue(stream.ready)
        self.assertFalse(stream.done)
        self.assertEqual(stream.required_prebuffer_ms, 750)
        self.assertEqual(stream.generation_ratio, .4)

    async def test_slow_producer_builds_remaining_deficit_before_playback(self):
        stream = PCMStreamBuffer(10000, prebuffer_ms=500, safety_ms=250)
        stream.append(pcm(2000), generation_ms=4000)
        self.assertFalse(stream.ready)
        self.assertEqual(stream.required_prebuffer_ms, 8250)
        with self.assertRaises(TimeoutError):
            await stream.wait_ready(timeout=.02)
        self.assertFalse(stream.stop.is_set(), 'admission timeout belongs to its consumer')
        stream.append(pcm(4000), generation_ms=8000)
        self.assertEqual(stream.required_prebuffer_ms, 4250)
        await stream.wait_ready(timeout=.1)
        self.assertFalse(stream.done)
        self.assertEqual(stream.produced_ms, 6000)

    async def test_short_complete_audio_bypasses_prebuffer_and_empty_fails(self):
        stream = PCMStreamBuffer(100)
        stream.append(pcm(50), generation_ms=2000)
        self.assertFalse(stream.ready)
        stream.finish()
        await stream.wait_ready(timeout=.1)
        empty = PCMStreamBuffer(100)
        empty.finish()
        with self.assertRaisesRegex(RuntimeError, 'no PCM'):
            await empty.wait_ready(timeout=.1)

    async def test_wait_is_cancelled_without_an_executor_thread_or_producer_leak(self):
        stream = PCMStreamBuffer(100)
        task = asyncio.create_task(stream.wait_ready())
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(stream.done)
        stream.cancel()
        with self.assertRaises(PCMStreamCancelled):
            await stream.wait_ready()

    async def test_error_is_not_hidden_by_prebuffered_pcm_and_promotion_is_recorded(self):
        stream = PCMStreamBuffer(2000)
        stream.append(pcm(1000), generation_ms=1)
        self.assertTrue(stream.ready)
        stream.promote()
        self.assertTrue(stream.foreground.is_set())
        stream.finish(ValueError('stem rejected'))
        with self.assertRaisesRegex(ValueError, 'stem rejected'):
            await stream.wait_ready(timeout=.1)


if __name__ == '__main__':
    unittest.main()
