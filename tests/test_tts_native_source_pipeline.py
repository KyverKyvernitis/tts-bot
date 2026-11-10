"""Local WebSocket -> native provider -> replay -> real FFmpeg -> PCM.

Only integer progress and Discord PCM frames return to Python. The audio fixture
is generated locally; these tests never contact Edge, Google or Discord.
"""
from __future__ import annotations

import asyncio
import io
import shutil
import struct
import subprocess
import wave

import pytest

from test_tts_native_async_bridge import audio_frame, edge_server, native, start


def mp3_fixture():
    assert shutil.which("ffmpeg"), "the native source integration gate requires FFmpeg"
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(struct.pack("<hh", 1000, 1000) * 48000)
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
                           "-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", "pipe:1"],
                          input=buffer.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=5, check=True).stdout


@pytest.mark.asyncio
async def test_provider_to_native_source_plays_before_websocket_finishes(native, tmp_path):
    assert getattr(native, "SOURCE_PROTOCOL_VERSION", None) == 1
    audio = mp3_fixture()
    assert len(audio) > 8192
    release = asyncio.Event()

    async def server(socket):
        await socket.send_bytes(audio_frame(audio[:8192]))
        await release.wait()
        await socket.send_bytes(audio_frame(audio[8192:]))
        await socket.send_str("Path:turn.end\r\n\r\n")

    budget = native.ReplayMemoryBudget(128)
    replay = native.NativeReplayBuffer(str(tmp_path), budget, 128, 65536)
    source = native.NativeSourceHandle(replay,
        ["-f", "mp3", "-probesize", "32", "-analyzeduration", "0"], ["-loglevel", "error"])
    stream = None
    try:
        async with edge_server(server) as endpoint:
            stream = await start(native, endpoint)
            progress = await asyncio.wait_for(stream.pump_chunk(replay), 2)
            assert progress[0] == progress[1] == 8192
            first = await asyncio.wait_for(asyncio.to_thread(source.read_frame), 2)
            assert len(first) == 3840
            assert not release.is_set(), "the first PCM frame waited for full provider completion"
            release.set()
            while (progress := await asyncio.wait_for(stream.pump_chunk(replay), 2)) is not None:
                assert isinstance(progress[0], int) and isinstance(progress[1], int)
            replay.finish(None)
            frames = [first]
            while frame := await asyncio.wait_for(asyncio.to_thread(source.read_frame), 2):
                assert len(frame) == 3840
                frames.append(frame)
            assert len(frames) >= 40
            assert any(any(frame) for frame in frames)
            snapshot = source.snapshot()
            assert snapshot["reaped"] is True
            assert snapshot["input_bytes"] == len(audio)
            assert snapshot["error"] is None
            assert replay.snapshot()["spilled"] is True
    finally:
        release.set()
        if stream is not None:
            stream.cancel()
        source.cancel()
        replay.close()
    assert budget.used == 0
    assert list(tmp_path.iterdir()) == []
