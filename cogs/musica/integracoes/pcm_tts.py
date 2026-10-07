"""PCM buffering for local TTS overlays without blocking the music clock."""
from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from typing import Callable

import discord


PCM_FRAME_BYTES = 3840
PCM_SILENCE = bytes(PCM_FRAME_BYTES)


class BufferedPCMSource(discord.AudioSource):
    """One decoder reader and a bounded queue; only that reader may block."""

    def __init__(self, source, *, max_frames: int = 8, error_getter: Callable | None = None):
        if source.is_opus():
            raise ValueError("o mixer TTS exige PCM")
        self.source = source
        self.error_getter = error_getter
        self.frames = queue.Queue(maxsize=max(1, min(25, int(max_frames))))
        self.ready = threading.Event()
        self.finished = threading.Event()
        self._stop = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._cleaned = False
        self.error: BaseException | None = None
        self.last_read_had_audio = False
        self._saw_frame = False
        self._reader = threading.Thread(target=self._fill, name="local-tts-pcm", daemon=True)
        self._reader.start()

    def _fill(self) -> None:
        try:
            while not self._stop.is_set():
                frame = self.source.read()
                if not frame:
                    provider_error = self.error_getter() if self.error_getter else None
                    if provider_error is not None:
                        raise RuntimeError("síntese progressiva TTS incompleta") from provider_error
                    process = getattr(self.source, "_process", None)
                    if process is not None and process.wait(timeout=0.5) != 0:
                        raise RuntimeError("decoder TTS encerrou com erro")
                    break
                if len(frame) != PCM_FRAME_BYTES:
                    raise RuntimeError("decoder TTS retornou um frame PCM incompleto")
                if not getattr(self.source, "last_read_had_audio", True):
                    # A prepared source may itself contain a nonblocking
                    # buffer. Its underrun filler must not become real PCM.
                    self._stop.wait(0.01)
                    continue
                while not self._stop.is_set():
                    try:
                        self.frames.put(frame, timeout=0.05)
                        self._saw_frame = True
                        self.ready.set()
                        break
                    except queue.Full:
                        continue
        except BaseException as exc:
            if not self._stop.is_set():
                self.error = exc
        finally:
            self.finished.set()
            self.ready.set()

    async def wait_ready(self, timeout: float) -> None:
        """Require decoder output before ducking/admitting the overlay."""
        try:
            ready = await asyncio.to_thread(self.ready.wait, max(0.01, float(timeout)))
            if not ready:
                raise TimeoutError("TTS não preparou o primeiro PCM")
            if self.error is not None:
                raise RuntimeError("decoder TTS falhou durante o preparo") from self.error
            if not self._saw_frame:
                raise RuntimeError("decoder TTS encerrou sem produzir PCM") from self.error
        except BaseException:
            self.cleanup()
            raise

    def read(self) -> bytes:
        self.last_read_had_audio = False
        if self._stop.is_set():
            return b""
        try:
            frame = self.frames.get_nowait()
        except queue.Empty:
            if self.finished.is_set():
                # The producer may enqueue its final frame between the first
                # get and the completion flag. Drain it before reporting EOF.
                try:
                    frame = self.frames.get_nowait()
                except queue.Empty:
                    if self.error is not None:
                        raise self.error
                    return b""
            else:
                return PCM_SILENCE
        self.last_read_had_audio = True
        return frame

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            self._stop.set()
            self.ready.set()
        with contextlib.suppress(Exception):
            self.source.cleanup()
