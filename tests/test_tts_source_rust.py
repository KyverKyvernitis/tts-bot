"""Real FFmpeg validation of the native replay-to-Discord boundary."""
from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from cogs.tts import replay_rust, source_rust
from cogs.tts.runtime import MemoryBudget, ReplayBuffer


def wav_bytes():
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(struct.pack("<hh", 1000, 1000) * 2640)
    return target.getvalue()


class NativeSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("FFmpeg unavailable")
        try:
            native = replay_rust.native_module()
            if getattr(native, "SOURCE_PROTOCOL_VERSION", None) != 1:
                raise RuntimeError("native source protocol unavailable")
        except (ImportError, OSError, RuntimeError) as error:
            if source_rust.enabled():
                raise
            self.skipTest(str(error))
        self.flags = patch.dict(os.environ, {"TTS_REPLAY_RUST": "1", "TTS_SOURCE_RUST": "1"})
        self.flags.start()
        self.directory = tempfile.TemporaryDirectory()
        self.buffer = ReplayBuffer(self.directory.name, MemoryBudget(8), memory_limit=8, max_bytes=1024 * 1024)
        self.sources = []

    async def asyncTearDown(self):
        for source in self.sources:
            source.cleanup()
        self.buffer.close()
        await asyncio.sleep(0)
        self.directory.cleanup()
        self.flags.stop()

    def source(self, options="-loglevel error"):
        source = source_rust.NativePCMSource(self.buffer,
            before_options="-f wav -probesize 32 -analyzeduration 0", options=options)
        self.sources.append(source)
        return source

    async def drain(self, source):
        frames = []
        while True:
            frame = await asyncio.to_thread(source.read)
            if not frame:
                break
            self.assertEqual(len(frame), 3840)
            frames.append(frame)
        return frames

    async def test_progressive_native_decode_does_not_use_python_replay_reader(self):
        source = self.source()
        data = wav_bytes()
        reading = asyncio.create_task(self.drain(source))
        # The source was started with an empty replay. Condvar wakes on native
        # appends, without an asyncio reader or FIFO writer moving these bytes.
        await self.buffer.append(data[:100])
        await asyncio.sleep(.01)
        await self.buffer.append(data[100:])
        await self.buffer.finish()
        frames = await asyncio.wait_for(reading, 3)
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0][:4], struct.pack("<hh", 1000, 1000))
        self.assertEqual(frames[-1][-960:], bytes(960))
        state = source.snapshot()
        self.assertTrue(state["reaped"])
        self.assertEqual(state["input_bytes"], len(data))
        self.assertFalse(source.is_opus())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_effect_filter_and_independent_decoder_after_cancellation(self):
        cancelled = self.source()
        reading = asyncio.create_task(asyncio.to_thread(cancelled.read))
        await asyncio.sleep(.01)
        cancelled.cleanup()
        with self.assertRaisesRegex(RuntimeError, "cancelad"):
            await asyncio.wait_for(reading, 2)
        self.assertFalse(self.buffer.closed)
        active = self.source("-loglevel error -af volume=0")
        await self.buffer.append(wav_bytes())
        await self.buffer.finish()
        frames = await asyncio.wait_for(self.drain(active), 3)
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(frame == bytes(3840) for frame in frames))
        self.assertTrue(active.snapshot()["reaped"])

    async def test_provider_error_is_not_empty_success(self):
        await self.buffer.append(wav_bytes())
        await self.buffer.finish(ConnectionError("native provider failed"))
        source = self.source()
        with self.assertRaisesRegex(RuntimeError, "native provider failed"):
            await asyncio.wait_for(self.drain(source), 3)
        self.assertTrue(source.snapshot()["reaped"])


if __name__ == "__main__":
    unittest.main()
