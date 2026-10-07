from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading

import pytest

from cogs.musica.integracoes.pcm_tts import BufferedPCMSource


FRAME = b"\x01\x00" * 1920
SILENCE = bytes(3840)


class GatedPCMSource:
    """A decoder whose first output is controlled by an explicit event."""

    def __init__(self, *outputs: bytes | BaseException) -> None:
        self.outputs = iter(outputs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.cleanup_calls = 0

    def read(self) -> bytes:
        self.entered.set()
        self.release.wait()
        if self.closed.is_set():
            return b""
        output = next(self.outputs, b"")
        if isinstance(output, BaseException):
            raise output
        return output

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.closed.set()
        self.release.set()


def wait_for(event: threading.Event) -> None:
    assert event.wait(2.0), "a operação controlada por evento não terminou"


def test_underrun_returns_silence_without_waiting_for_decoder() -> None:
    decoder = GatedPCMSource(FRAME)
    source = BufferedPCMSource(decoder)
    completed = threading.Event()
    output: list[bytes] = []

    def read_on_audio_thread() -> None:
        try:
            output.append(source.read())
        finally:
            completed.set()

    try:
        wait_for(decoder.entered)
        reader = threading.Thread(target=read_on_audio_thread, daemon=True)
        reader.start()
        wait_for(completed)
        assert output == [SILENCE]
        assert not source.last_read_had_audio
        assert not source.finished.is_set()
    finally:
        source.cleanup()
        wait_for(source.finished)


@pytest.mark.asyncio
async def test_ready_and_first_audio_marker_require_a_real_pcm_frame() -> None:
    decoder = GatedPCMSource(FRAME)
    source = BufferedPCMSource(decoder)
    ready = asyncio.create_task(source.wait_ready(2.0))
    try:
        await asyncio.to_thread(wait_for, decoder.entered)
        assert source.read() == SILENCE
        assert not source.last_read_had_audio
        await asyncio.sleep(0)
        assert not ready.done()

        decoder.release.set()
        await asyncio.wait_for(ready, 2.0)
        await asyncio.to_thread(wait_for, source.finished)
        assert source.read() == FRAME
        assert source.last_read_had_audio
        assert source.read() == b""
        assert not source.last_read_had_audio
    finally:
        source.cleanup()
        await asyncio.to_thread(wait_for, source.finished)
        if not ready.done():
            ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)


@pytest.mark.asyncio
async def test_decoder_silence_filler_does_not_satisfy_first_pcm_readiness() -> None:
    class MarkedPCMSource(GatedPCMSource):
        def __init__(self) -> None:
            super().__init__(FRAME)
            self.read_calls = 0
            self.last_read_had_audio = False

        def read(self) -> bytes:
            self.read_calls += 1
            if self.read_calls == 1:
                self.last_read_had_audio = False
                return SILENCE
            output = super().read()
            self.last_read_had_audio = bool(output)
            return output

    decoder = MarkedPCMSource()
    source = BufferedPCMSource(decoder)
    ready = asyncio.create_task(source.wait_ready(2.0))
    try:
        await asyncio.to_thread(wait_for, decoder.entered)
        assert source.frames.empty()
        assert source.read() == SILENCE
        assert not source.last_read_had_audio
        await asyncio.sleep(0)
        assert not ready.done()

        decoder.release.set()
        await asyncio.wait_for(ready, 2.0)
        assert source.read() == FRAME
        assert source.last_read_had_audio
    finally:
        source.cleanup()
        await asyncio.to_thread(wait_for, source.finished)
        if not ready.done():
            ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)


def test_decoder_error_is_reported_after_queued_pcm_is_drained() -> None:
    error = OSError("decoder caiu depois do primeiro frame")
    decoder = GatedPCMSource(FRAME, error)
    decoder.release.set()
    source = BufferedPCMSource(decoder)
    try:
        wait_for(source.finished)
        assert source.error is error
        assert source.read() == FRAME
        assert source.last_read_had_audio
        with pytest.raises(OSError) as raised:
            source.read()
        assert raised.value is error
        assert not source.last_read_had_audio
    finally:
        source.cleanup()


def test_provider_error_after_pcm_is_not_silently_treated_as_eof() -> None:
    error = RuntimeError("provider falhou durante a síntese")
    decoder = GatedPCMSource(FRAME)
    decoder.release.set()
    source = BufferedPCMSource(decoder, error_getter=lambda: error)
    try:
        wait_for(source.finished)
        assert source.read() == FRAME
        with pytest.raises(RuntimeError) as raised:
            source.read()
        assert raised.value.__cause__ is error
        assert source.error is raised.value
    finally:
        source.cleanup()


@pytest.mark.asyncio
async def test_empty_eof_cannot_be_admitted_as_ready_audio() -> None:
    decoder = GatedPCMSource()
    decoder.release.set()
    source = BufferedPCMSource(decoder)
    with pytest.raises(RuntimeError, match="sem produzir PCM"):
        await source.wait_ready(2.0)
    assert decoder.cleanup_calls == 1
    assert source.read() == b""
    source.cleanup()
    assert decoder.cleanup_calls == 1


@pytest.mark.asyncio
async def test_ready_timeout_releases_a_decoder_blocked_in_read() -> None:
    decoder = GatedPCMSource(FRAME)
    source = BufferedPCMSource(decoder)
    try:
        await asyncio.to_thread(wait_for, decoder.entered)
        with pytest.raises(TimeoutError):
            await source.wait_ready(0.01)
        await asyncio.to_thread(wait_for, source.finished)
        assert decoder.cleanup_calls == 1
        assert source.read() == b""
    finally:
        source.cleanup()


@pytest.mark.asyncio
async def test_cancelling_readiness_cleans_up_blocked_decoder_and_reader_thread() -> None:
    decoder = GatedPCMSource(FRAME)
    source = BufferedPCMSource(decoder)
    ready = asyncio.create_task(source.wait_ready(5.0))
    try:
        await asyncio.to_thread(wait_for, decoder.entered)
        await asyncio.sleep(0)
        assert not ready.done()
        ready.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ready
        assert decoder.cleanup_calls == 1
        await asyncio.to_thread(wait_for, source.finished)
        source._reader.join(timeout=1.0)
        assert not source._reader.is_alive()
        assert source.read() == b""
    finally:
        source.cleanup()
        await asyncio.to_thread(wait_for, source.finished)
        source._reader.join(timeout=1.0)
        assert not source._reader.is_alive()
        if not ready.done():
            ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)


@pytest.mark.asyncio
async def test_ready_rejects_an_error_already_known_after_pcm_frames() -> None:
    error = OSError("decoder falhou depois do primeiro frame")
    decoder = GatedPCMSource(FRAME, error)
    decoder.release.set()
    source = BufferedPCMSource(decoder)
    try:
        await asyncio.to_thread(wait_for, source.finished)
        assert source.frames.qsize() == 1
        with pytest.raises(RuntimeError, match="falhou durante o preparo") as raised:
            await source.wait_ready(2.0)
        assert raised.value.__cause__ is error
        assert decoder.cleanup_calls == 1
    finally:
        source.cleanup()


@pytest.mark.asyncio
async def test_ffmpeg_starts_pcm_before_progressive_mp3_input_reaches_eof(tmp_path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg indisponível para o teste de streaming PCM offline")

    import discord

    fixture_path = tmp_path / "frase_sintetica.mp3"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(fixture_path),
        ],
        check=True,
        capture_output=True,
        timeout=10.0,
    )
    fixture = fixture_path.read_bytes()
    assert len(fixture) > 32768

    class ProgressiveMP3Input:
        def __init__(self) -> None:
            self.position = 0
            self.allow_eof = threading.Event()
            self.waiting_for_eof = threading.Event()

        def read(self, size: int) -> bytes:
            # Supply only part of the MP3; keep its remaining audio and EOF
            # withheld until cleanup. No synthesis or network service is used.
            if self.position < 32768:
                end = min(self.position + size, 32768)
                data = fixture[self.position:end]
                self.position = end
                return data
            self.waiting_for_eof.set()
            self.allow_eof.wait()
            return b""

    mp3_input = ProgressiveMP3Input()
    decoder = discord.FFmpegPCMAudio(
        mp3_input,
        executable=ffmpeg,
        pipe=True,
        before_options="-f mp3 -probesize 32k -analyzeduration 0",
        options="-loglevel error",
    )
    process = decoder._process
    writer = decoder._pipe_writer_thread

    class DecoderWithInputCleanup:
        _process = process

        def read(self) -> bytes:
            return decoder.read()

        def is_opus(self) -> bool:
            return False

        def cleanup(self) -> None:
            mp3_input.allow_eof.set()
            decoder.cleanup()

    source = BufferedPCMSource(DecoderWithInputCleanup(), max_frames=2)
    try:
        await source.wait_ready(5.0)
        assert not mp3_input.allow_eof.is_set()
        assert writer is not None and writer.is_alive()
        assert process.poll() is None
        assert source.frames.maxsize == 2
        assert 1 <= source.frames.qsize() <= 2
        frame = source.read()
        assert len(frame) == 3840
        assert frame != SILENCE
        assert source.last_read_had_audio
        assert not source.finished.is_set()
    finally:
        source.cleanup()
        await asyncio.to_thread(wait_for, source.finished)
        if writer is not None:
            await asyncio.to_thread(writer.join, 2.0)
            assert not writer.is_alive()
        assert mp3_input.allow_eof.is_set()
        assert process.poll() is not None


def test_bounded_queue_stops_read_ahead_and_cleanup_unblocks_producer() -> None:
    class EndlessPCMSource:
        def __init__(self) -> None:
            self.read_calls = 0
            self.third_read = threading.Event()
            self.cleanup_calls = 0

        def read(self) -> bytes:
            self.read_calls += 1
            if self.read_calls == 3:
                self.third_read.set()
            return FRAME

        def is_opus(self) -> bool:
            return False

        def cleanup(self) -> None:
            self.cleanup_calls += 1

    decoder = EndlessPCMSource()
    source = BufferedPCMSource(decoder, max_frames=2)
    try:
        wait_for(decoder.third_read)
        assert source.frames.maxsize == 2
        assert source.frames.qsize() == 2
        assert decoder.read_calls == 3
        assert not source.finished.is_set()

        source.cleanup()
        source.cleanup()
        wait_for(source.finished)
        source._reader.join(timeout=1.0)
        assert not source._reader.is_alive()
        assert decoder.cleanup_calls == 1
        assert decoder.read_calls == 3
        assert source.read() == b""
    finally:
        source.cleanup()
        wait_for(source.finished)
        source._reader.join(timeout=1.0)
        assert not source._reader.is_alive()


def test_cleanup_is_idempotent_while_decoder_waits_for_output() -> None:
    decoder = GatedPCMSource(FRAME)
    source = BufferedPCMSource(decoder)
    try:
        wait_for(decoder.entered)
        source.cleanup()
        source.cleanup()
        wait_for(source.finished)
        source._reader.join(timeout=1.0)
        assert not source._reader.is_alive()
        assert decoder.cleanup_calls == 1
        assert source.error is None
        assert source.read() == b""
        assert not source.last_read_had_audio
    finally:
        source.cleanup()
        wait_for(source.finished)
        source._reader.join(timeout=1.0)
        assert not source._reader.is_alive()
