from __future__ import annotations

import asyncio
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.tts.prepared import PreparedOpusCache
from cogs.tts.runtime import MemoryBudget, ReplayBuffer, StreamJob, unlink_if_unlocked
from cogs.tts.streaming import SharedSynthesisMixin


class PreparedQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'phrase.mp3'
        self.path.write_bytes(b'leased audio')
        self.cache = PreparedOpusCache(idle_poll_seconds=.01)

    async def asyncTearDown(self):
        await self.cache.aclose()
        self.directory.cleanup()

    async def test_waits_beyond_three_seconds_and_leases_pending_audio(self):
        idle = asyncio.Event()
        encoded = []

        async def encode(audio_file, options, *, idle, before_options='-nostdin'):
            encoded.append(audio_file.read())
            return [b'packet']

        key = self.cache.key(self.path, '')
        with patch.object(self.cache, '_encode', encode):
            preparing = asyncio.create_task(self.cache.prepare(key, self.path, '', idle=idle.is_set))
            await asyncio.sleep(3.05)
            self.assertFalse(preparing.done())
            self.assertFalse(self.cache.busy)
            self.assertFalse(unlink_if_unlocked(str(self.path)))
            idle.set()
            await asyncio.wait_for(preparing, 1)
        self.assertEqual(encoded, [b'leased audio'])
        self.assertEqual(self.cache.get(key).read(), b'packet')

    async def test_pending_queue_is_bounded_coalesced_and_closed(self):
        self.cache.max_pending = 2
        idle = lambda: False
        paths = [Path(self.directory.name) / f'{index}.mp3' for index in range(3)]
        for path in paths:
            path.write_bytes(b'audio')
        keys = [self.cache.key(path, '') for path in paths]
        first = asyncio.create_task(self.cache.prepare(keys[0], paths[0], '', idle=idle))
        second = asyncio.create_task(self.cache.prepare(keys[1], paths[1], '', idle=idle))
        await asyncio.sleep(.02)
        await self.cache.prepare(keys[0], paths[0], '', idle=idle)
        await self.cache.prepare(keys[2], paths[2], '', idle=idle)
        self.assertEqual(self.cache.pending, set(keys[:2]))
        await self.cache.aclose()
        self.assertTrue(first.cancelled())
        self.assertTrue(second.cancelled())
        self.assertFalse(self.cache.pending)
        self.assertTrue(unlink_if_unlocked(str(paths[0])))

    async def test_replaced_path_is_not_encoded_or_published(self):
        idle = asyncio.Event()
        key = self.cache.key(self.path, '')
        encoded = []

        async def encode(*args, **kwargs):
            encoded.append(True)
            return [b'packet']

        with patch.object(self.cache, '_encode', encode):
            preparing = asyncio.create_task(self.cache.prepare(key, self.path, '', idle=idle.is_set))
            await asyncio.sleep(.02)
            replacement = Path(self.directory.name) / 'replacement.mp3'
            replacement.write_bytes(b'new audio')
            os.replace(replacement, self.path)
            idle.set()
            await asyncio.wait_for(preparing, 1)
        self.assertFalse(encoded)
        self.assertIsNone(self.cache.get(key))
        self.assertEqual(self.path.read_bytes(), b'new audio')

    async def test_atomic_replacement_during_encoding_reads_leased_inode_and_discards_result(self):
        key = self.cache.key(self.path, '')
        encoded = []

        async def encode(audio_file, options, *, idle, before_options='-nostdin'):
            replacement = Path(self.directory.name) / 'replacement.mp3'
            replacement.write_bytes(b'replacement audio')
            os.replace(replacement, self.path)
            encoded.append(audio_file.read())
            return [b'old packet']

        with patch.object(self.cache, '_encode', encode):
            await self.cache.prepare(key, self.path, '', idle=lambda: True)
        self.assertEqual(encoded, [b'leased audio'])
        self.assertIsNone(self.cache.get(key))
        self.assertEqual(self.path.read_bytes(), b'replacement audio')

    async def test_two_cancellations_keep_lease_and_encoder_slot_until_physical_stop(self):
        started = threading.Event()
        release = threading.Event()
        cleaned = threading.Event()
        captured = {}

        class BlockingSource:
            def __init__(self, audio_file, **kwargs):
                captured.update(kwargs)
                self.audio_file = audio_file

            def read(self):
                started.set()
                if not release.wait(2):
                    raise RuntimeError('test did not release physical encoder')
                self.audio_file.read(1)
                return b''

            def cleanup(self):
                cleaned.set()

        key = self.cache.key(self.path, '')
        with patch('cogs.tts.prepared.discord.FFmpegOpusAudio', BlockingSource, create=True):
            preparing = asyncio.create_task(self.cache.prepare(key, self.path, '', idle=lambda: True))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            preparing.cancel()
            self.assertTrue(await asyncio.to_thread(cleaned.wait, 1))
            preparing.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(preparing.done())
            self.assertTrue(self.cache.busy)
            self.assertFalse(unlink_if_unlocked(str(self.path)))
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await preparing
        self.assertTrue(captured['pipe'])
        self.assertNotIn('codec', captured)
        self.assertFalse(self.cache.busy)
        self.assertFalse(self.cache.pending)
        self.assertTrue(unlink_if_unlocked(str(self.path)))

    async def test_input_options_distinguish_identity_and_reach_encoder(self):
        plain = self.cache.key(self.path, '-vn')
        before_options = '-nostdin -ss 0.2'
        seek = self.cache.key(self.path, '-vn', before_options=before_options)
        self.assertNotEqual(plain, seek)
        captured = []

        async def encode(audio_file, options, *, idle, before_options):
            captured.append((options, before_options, audio_file.read()))
            return [b'seek packet']

        with patch.object(self.cache, '_encode', encode):
            await self.cache.prepare(seek, self.path, '-vn', before_options=before_options, idle=lambda: True)
        self.assertEqual(captured, [('-vn', before_options, b'leased audio')])
        self.assertIsNone(self.cache.get(plain))
        self.assertEqual(self.cache.get(seek).read(), b'seek packet')
        self.assertFalse(self.cache.pending)
        self.assertTrue(unlink_if_unlocked(str(self.path)))

    async def test_foreground_arrival_stops_background_encoder(self):
        started = threading.Event()
        released = threading.Event()
        foreground = asyncio.Event()

        class Source:
            def __init__(self, audio_file, **kwargs):
                pass

            def read(self):
                started.set()
                released.wait(1)
                return b''

            def cleanup(self):
                released.set()

        key = self.cache.key(self.path, '')
        with patch('cogs.tts.prepared.discord.FFmpegOpusAudio', Source, create=True):
            preparing = asyncio.create_task(self.cache.prepare(key, self.path, '', idle=lambda: not foreground.is_set()))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            foreground.set()
            await asyncio.wait_for(preparing, 1)
        self.assertTrue(released.is_set())
        self.assertIsNone(self.cache.get(key))
        self.assertFalse(self.cache.busy)


class _SlotProbe(SharedSynthesisMixin):
    def __init__(self, prefetch):
        self.prefetch = prefetch
        self.samples = []

    def _get_gtts_prefetch_semaphore(self):
        return self.prefetch

    def _get_edge_prefetch_semaphore(self):
        return self.prefetch

    def _record_latency_sample(self, key, value):
        self.samples.append((key, value))


def _slot_job(engine='gtts'):
    return SimpleNamespace(item=SimpleNamespace(engine=engine), foreground=asyncio.Event())


class PrefetchReservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_worker_prefetch_waits_for_promotion_without_physical_admission(self):
        probe = _SlotProbe(None)
        job = _slot_job()
        waiting = asyncio.create_task(probe._shared_prefetch_slot(job))
        await asyncio.sleep(.01)
        self.assertFalse(waiting.done())
        current = _slot_job()
        current.foreground.set()
        self.assertIsNone(await probe._shared_prefetch_slot(current))
        job.foreground.set()
        self.assertIsNone(await asyncio.wait_for(waiting, 1))

    async def test_single_worker_pending_prefetch_cancels_immediately(self):
        waiting = asyncio.create_task(_SlotProbe(None)._shared_prefetch_slot(_slot_job()))
        await asyncio.sleep(.01)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

    async def test_gtts_prefetch_limit_and_foreground_bypass(self):
        semaphore = asyncio.Semaphore(1)
        probe = _SlotProbe(semaphore)
        admitted = await probe._shared_prefetch_slot(_slot_job())
        self.assertIs(admitted, semaphore)
        job = _slot_job()
        waiting = asyncio.create_task(probe._shared_prefetch_slot(job))
        await asyncio.sleep(.01)
        self.assertFalse(waiting.done())
        foreground = _slot_job()
        foreground.foreground.set()
        self.assertIsNone(await probe._shared_prefetch_slot(foreground))
        job.foreground.set()
        self.assertIsNone(await asyncio.wait_for(waiting, 1))
        admitted.release()
        await asyncio.sleep(0)
        self.assertEqual(semaphore._value, 1)

    async def test_cancellation_after_speculative_grant_does_not_leak_slot(self):
        semaphore = asyncio.Semaphore(0)
        probe = _SlotProbe(semaphore)
        waiting = asyncio.create_task(probe._shared_prefetch_slot(_slot_job()))
        await asyncio.sleep(.01)
        semaphore.release()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0)
        self.assertEqual(semaphore._value, 1)

    async def test_promotion_racing_with_grant_returns_speculative_slot(self):
        semaphore = asyncio.Semaphore(0)
        probe = _SlotProbe(semaphore)
        job = _slot_job()
        waiting = asyncio.create_task(probe._shared_prefetch_slot(job))
        await asyncio.sleep(.01)
        semaphore.release()
        job.foreground.set()
        self.assertIsNone(await asyncio.wait_for(waiting, 1))
        self.assertEqual(semaphore._value, 1)

    async def test_first_byte_metrics_exclude_buffer_io_and_are_recorded_once(self):
        probe = _SlotProbe(None)
        with tempfile.TemporaryDirectory() as directory:
            buffer = ReplayBuffer(directory, MemoryBudget(1024), memory_limit=1024, max_bytes=4096)
            job = StreamJob(key='test', item=SimpleNamespace(engine='gtts'), buffer=buffer,
                            started_at=time.monotonic() - .1, slot_wait_ms=30)
            original_append = buffer.append

            async def delayed_append(data):
                await asyncio.sleep(.04)
                await original_append(data)

            with patch.object(buffer, 'append', delayed_append):
                await probe._append_shared_audio(job, b'first')
            after_append_ms = (time.monotonic() - job.started_at) * 1000.0
            await probe._append_shared_audio(job, b'second')
            self.assertEqual([key for key, _ in probe.samples], ['gtts_first_byte', 'gtts_network_first_byte'])
            self.assertAlmostEqual(probe.samples[0][1] - probe.samples[1][1], 30, places=3)
            self.assertGreater(after_append_ms - job.first_audio_ms, 30)
            await buffer.finish()
            buffer.close()


class RealPreparedEncoderTests(unittest.TestCase):
    def test_real_discord_ffmpeg_encodes_mp3_wav_and_effect_variants(self):
        if shutil.which('ffmpeg') is None:
            self.skipTest('FFmpeg required')
        try:
            importlib.metadata.version('discord.py')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('real discord.py dependency required')
        # A subprocess imports real discord.py even when other tests installed
        # module stubs in this process; the test never depends on suite order.
        script = '''
import asyncio, pathlib, subprocess, tempfile
import discord
from cogs.tts.prepared import PreparedOpusCache
async def main():
    discord.opus.Encoder()
    decoder = discord.opus.Decoder()
    with tempfile.TemporaryDirectory() as directory:
        cache = PreparedOpusCache(idle_poll_seconds=.01)
        for suffix in ('wav', 'mp3'):
            path = pathlib.Path(directory) / ('phrase.' + suffix)
            subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','sine=frequency=440:duration=0.35',
                '-ar','24000','-ac','1','-y',str(path)], check=True)
            for options in ('-vn', '-vn -af asetrate=30000,aresample=48000,atempo=0.9,aecho=0.8:0.6:60:0.25'):
                for before_options in ('-nostdin', '-nostdin -ss 0.2'):
                    key = cache.key(path, options, before_options=before_options)
                    await cache.prepare(key, path, options, idle=lambda: True,
                                        before_options=before_options)
                    source = cache.get(key)
                    assert source is not None, (suffix, options, before_options, 'no prepared frames')
                    frames = []
                    while packet := source.read():
                        frames.append(packet)
                        pcm = decoder.decode(packet)
                        assert len(pcm) == 3840, len(pcm)
                    assert len(frames) > 5, (suffix, options, len(frames))
                    assert any(any(decoder.decode(packet)) for packet in frames)
                    cold = discord.FFmpegOpusAudio(str(path), before_options=before_options,
                                                   options=options, bitrate=64)
                    cold_frames = []
                    try:
                        while packet := cold.read():
                            if not cold_frames and packet.startswith((b'OpusHead', b'OpusTags')):
                                continue
                            cold_frames.append(packet)
                    finally:
                        cold.cleanup()
                    assert frames == cold_frames, (suffix, options, before_options, 'cold/prepared mismatch')
        await cache.aclose()
asyncio.run(main())
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
