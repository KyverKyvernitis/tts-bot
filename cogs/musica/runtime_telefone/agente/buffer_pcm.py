"""Leitura de FFmpeg fora da thread de voz, com memória e espera limitadas."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import deque
from typing import Any

import discord

FRAME_BYTES = 3840
SILENCE = bytes(FRAME_BYTES)


class BufferedPCMSource(discord.AudioSource):
    """Um produtor e um consumidor; a fila cheia aplica backpressure ao FFmpeg.

    ``read`` nunca espera a rede. Ausência temporária gera silêncio, não EOF,
    permitindo que TTS continue. Um stall prolongado vira erro recuperável.
    """

    def __init__(self, source: discord.AudioSource, *, max_frames: int = 75, stall_seconds: float = 12.0) -> None:
        self.source = source
        self.max_frames = max(1, min(150, int(max_frames)))
        self.stall_seconds = max(0.1, min(30.0, float(stall_seconds)))
        self._frames: deque[bytes] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._eof = False
        self._error: Exception | None = None
        self._empty_since: float | None = None
        self.last_read_had_audio = False
        self._underruns = 0
        self._peak_frames = 0
        self._decoder_max_ms = 0.0
        self._decoder_late_reads = 0
        self._thread = threading.Thread(target=self._produce, name="music-pcm-buffer", daemon=True)
        self._thread.start()

    def is_opus(self) -> bool:
        return False

    def _produce(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or len(self._frames) < self.max_frames)
                    if self._closed:
                        return
                started = time.monotonic()
                frame = self.source.read()
                elapsed_ms = (time.monotonic() - started) * 1000.0
                with self._condition:
                    if self._closed:
                        return
                    self._decoder_max_ms = max(self._decoder_max_ms, elapsed_ms)
                    self._decoder_late_reads += int(elapsed_ms > 20.0)
                    if not frame:
                        error = getattr(self.source, "_current_error", None)
                        if isinstance(error, Exception):
                            self._error = error
                        self._eof = True
                        self._condition.notify_all()
                        return
                    self._frames.append(frame[:FRAME_BYTES].ljust(FRAME_BYTES, b"\0"))
                    self._peak_frames = max(self._peak_frames, len(self._frames))
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                if not self._closed:
                    self._error = exc
                    self._eof = True
                    self._condition.notify_all()

    async def wait_ready(self, *, timeout: float, min_frames: int = 1) -> None:
        deadline = time.monotonic() + max(0.01, timeout)
        target = max(1, min(self.max_frames, int(min_frames)))
        while True:
            with self._condition:
                if self._closed:
                    raise RuntimeError("buffer de áudio encerrado")
                if len(self._frames) >= target or (self._eof and self._frames):
                    return
                if self._error is not None:
                    raise self._error
                if self._eof:
                    raise RuntimeError("FFmpeg encerrou sem produzir áudio")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("FFmpeg não produziu o primeiro áudio a tempo")
            await asyncio.sleep(min(0.01, remaining))

    def read(self) -> bytes:
        with self._condition:
            self.last_read_had_audio = False
            if self._closed:
                return b""
            if self._frames:
                frame = self._frames.popleft()
                self.last_read_had_audio = True
                self._empty_since = None
                self._condition.notify_all()
                return frame
            if self._error is not None:
                raise self._error
            if self._eof:
                return b""
            now = time.monotonic()
            if self._empty_since is None:
                self._empty_since = now
            if now - self._empty_since >= self.stall_seconds:
                raise TimeoutError("stream de áudio parou de fornecer frames")
            self._underruns += 1
            return SILENCE

    def audio_buffer_metrics(self) -> dict[str, Any]:
        with self._condition:
            return {
                "buffer_frames": len(self._frames),
                "buffer_peak_frames": self._peak_frames,
                "buffer_max_bytes": self.max_frames * FRAME_BYTES,
                "buffer_underruns": self._underruns,
                "decoder_read_max_ms": round(self._decoder_max_ms, 3),
                "decoder_deadline_overruns": self._decoder_late_reads,
            }

    def cleanup(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()
        # Matar FFmpeg desbloqueia a leitura do produtor; não aguardamos join
        # dentro do event loop ou da thread de áudio.
        with contextlib.suppress(Exception):
            self.source.cleanup()
