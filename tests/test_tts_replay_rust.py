"""Native replay integration; no Discord, provider or network is involved."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cogs.tts import replay_rust
from cogs.tts.runtime import MemoryBudget, ReplayBuffer


class ReplaySelectionTests(unittest.TestCase):
    def test_disabled_does_not_load_native_module(self):
        with patch.dict(os.environ, {"TTS_BACKEND": "python", "TTS_REPLAY_RUST": "0"}), patch.object(
                replay_rust, "native_module", side_effect=AssertionError("native import")):
            budget = MemoryBudget(100)
            buffer = ReplayBuffer("unused", budget, memory_limit=100, max_bytes=100)
            self.assertIsInstance(buffer, ReplayBuffer)
            buffer.close()

    def test_selected_native_mismatch_fails_before_stream(self):
        with patch.dict(os.environ, {"TTS_BACKEND": "python", "TTS_REPLAY_RUST": "1"}), patch.object(
                replay_rust, "native_module", side_effect=RuntimeError("protocol mismatch")):
            with self.assertRaisesRegex(RuntimeError, "protocol mismatch"):
                MemoryBudget(100)


class NativeReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            self.module = replay_rust.native_module()
        except (ImportError, OSError, RuntimeError) as error:
            # Explicit opt-in makes an absent/stale binary a failed integration
            # run. Ordinary Python-only test runs can still exercise fallback.
            if replay_rust.enabled():
                raise
            self.skipTest(f"native replay extension unavailable: {error}")
        self.flag = patch.dict(os.environ, {"TTS_REPLAY_RUST": "1"})
        self.flag.start()
        self.directory = tempfile.TemporaryDirectory()
        self.buffers = []

    async def asyncTearDown(self):
        for buffer in self.buffers:
            buffer.close()
        await asyncio.sleep(0)
        self.directory.cleanup()
        self.flag.stop()

    def buffer(self, budget=None, *, memory_limit=8, max_bytes=4096):
        budget = budget if budget is not None else MemoryBudget(8)
        result = ReplayBuffer(self.directory.name, budget, memory_limit=memory_limit, max_bytes=max_bytes)
        self.buffers.append(result)
        self.assertEqual(result.backend, "rust")
        return result

    async def test_shared_ram_spill_and_late_independent_readers(self):
        budget = MemoryBudget(8)
        first, second = self.buffer(budget), self.buffer(budget)
        await first.append(b"abcdef")
        fast = first.chunks(chunk_size=2)
        self.assertEqual(await anext(fast), b"ab")
        await second.append(b"1234")
        self.assertTrue(second._native.snapshot()["spilled"])
        self.assertEqual(budget.used, 6)
        await first.append(b"ghijkl")
        self.assertEqual(budget.used, 0)
        await first.finish()
        await second.finish()
        self.assertEqual(b"".join([part async for part in fast]), b"cdefghijkl")
        self.assertEqual(b"".join([part async for part in first.chunks(chunk_size=5)]), b"abcdefghijkl")
        self.assertEqual(b"".join([part async for part in second.chunks()]), b"1234")
        first.close()
        second.close()
        self.assertEqual(budget.used, 0)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_error_drains_received_bytes_and_preserves_original_exception(self):
        buffer = self.buffer()
        failure = ConnectionError("provider failed")
        reader = buffer.chunks()
        waiting = asyncio.create_task(anext(reader))
        await asyncio.sleep(0)
        await buffer.append(b"prefix")
        self.assertEqual(await waiting, b"prefix")
        await buffer.finish(failure)
        with self.assertRaises(ConnectionError) as raised:
            await anext(reader)
        self.assertIs(raised.exception, failure)
        with self.assertRaises(ConnectionError) as raised:
            await buffer.wait_for_bytes()
        self.assertIs(raised.exception, failure)
        with self.assertRaises(RuntimeError):
            await buffer.copy_to(str(Path(self.directory.name) / "cache"))

    async def test_size_limit_and_atomic_cache_publication(self):
        buffer = self.buffer(max_bytes=9)
        destination = Path(self.directory.name) / "cached.mp3"
        destination.write_bytes(b"previous")
        await buffer.append(b"complete!")
        with self.assertRaises(ValueError):
            await buffer.append(b"overflow")
        self.assertEqual(buffer.size, 9)
        with self.assertRaises(RuntimeError):
            await buffer.copy_to(str(destination))
        self.assertEqual(destination.read_bytes(), b"previous")
        await buffer.finish()
        await buffer.copy_to(str(destination))
        self.assertEqual(destination.read_bytes(), b"complete!")
        with self.assertRaises(OSError):
            await buffer.copy_to(self.directory.name)
        self.assertEqual([p.name for p in Path(self.directory.name).iterdir()], ["cached.mp3"])

    async def test_cancelled_reader_leaves_other_consumers_and_producer_alive(self):
        buffer = self.buffer()
        first, second = buffer.chunks(), buffer.chunks()
        waiting = asyncio.create_task(anext(first))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await buffer.append(b"audio")
        await buffer.finish()
        self.assertEqual(b"".join([part async for part in second]), b"audio")
        self.assertFalse(buffer.closed)

    async def test_double_cancel_and_close_wait_for_physical_append(self):
        buffer = self.buffer(memory_limit=0)
        native = buffer._native
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        class DelayedNative:
            def __getattr__(self, name):
                return getattr(native, name)

            def append(self, data):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise TimeoutError("test append was not released")
                native.append(data)

        buffer._native = DelayedNative()
        task = asyncio.create_task(buffer.append(b"complete"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            buffer.close()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            self.assertFalse(native.snapshot()["closed"])
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertTrue(native.snapshot()["closed"])
        self.assertEqual(buffer.budget.used, 0)

    async def test_close_wakes_blocked_readers_and_empty_stream_reports_no_audio(self):
        buffer = self.buffer()
        reader = buffer.chunks()
        pending = asyncio.create_task(anext(reader))
        await asyncio.sleep(0)
        buffer.close()
        with self.assertRaisesRegex(RuntimeError, "fechado"):
            await asyncio.wait_for(pending, 1)
        empty = self.buffer()
        await empty.finish()
        with self.assertRaisesRegex(RuntimeError, "não enviou áudio"):
            await empty.wait_for_bytes()

    async def test_ram_append_and_read_do_not_schedule_blocking_workers(self):
        buffer = self.buffer(memory_limit=1024)
        self.assertTrue(hasattr(buffer._native, "try_append_in_memory"), "rebuild native replay fast paths")
        with patch.object(buffer, "_io", side_effect=AssertionError("RAM went through worker")):
            await buffer.append(b"audio")
            reader = buffer.chunks()
            self.assertEqual(await anext(reader), b"audio")
            await reader.aclose()
        await buffer.finish()
        buffer.close()
        self.assertEqual(buffer.budget.used, 0)


if __name__ == "__main__":
    unittest.main()
