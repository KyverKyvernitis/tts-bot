"""Mixer PCM do Music Agent.

Mantém música e overlays de TTS na mesma sessão de voz do Discord. O caminho
normal sem overlay usa audioop quando disponível para evitar loops Python por
amostra a cada frame de 20 ms.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from array import array
from typing import Any

import discord

PCM_FRAME_BYTES = 3840


class AgentMixedAudioSource(discord.AudioSource):
    def __init__(self, *, loop: asyncio.AbstractEventLoop, music_source: discord.AudioSource, music_volume: float, duck_factor: float = 0.08) -> None:
        self.loop = loop
        self.music_source = music_source
        self.normal_music_volume = max(0.0, min(2.0, float(music_volume)))
        self.duck_factor = max(0.0, min(1.0, float(duck_factor)))
        self._overlays: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._closed = False
        self._music_ended = False
        self._started_monotonic = time.monotonic()
        self.first_frame_ms: float | None = None
        self.first_frame_monotonic: float | None = None

    def is_opus(self) -> bool:
        return False

    def set_music_volume(self, volume: float) -> None:
        self.normal_music_volume = max(0.0, min(2.0, float(volume)))

    def set_duck_factor(self, factor: float) -> None:
        self.duck_factor = max(0.0, min(1.0, float(factor)))

    def add_tts(self, source: discord.AudioSource, *, volume: float = 1.0) -> asyncio.Future:
        future = self.loop.create_future()
        with self._lock:
            self._overlays.append({"source": source, "volume": max(0.0, min(2.0, float(volume))), "future": future, "ended": False})
        return future

    def has_tts(self) -> bool:
        with self._lock:
            return bool(self._overlays)

    def _future_result(self, future: asyncio.Future, value: object = None) -> None:
        def _set() -> None:
            if not future.done():
                future.set_result(value)
        self.loop.call_soon_threadsafe(_set)

    def _future_exception(self, future: asyncio.Future, error: Exception) -> None:
        def _set() -> None:
            if not future.done():
                future.set_exception(error)
        self.loop.call_soon_threadsafe(_set)

    def cancel_tts(self, future: asyncio.Future) -> None:
        target = None
        with self._lock:
            for overlay in self._overlays:
                if overlay["future"] is future:
                    target = overlay
                    self._overlays.remove(overlay)
                    break
        if target is not None:
            with contextlib.suppress(Exception):
                target["source"].cleanup()
        if not future.done():
            future.cancel()

    def _limit(self, value: int) -> int:
        return max(-32768, min(32767, int(value)))

    def _audioop(self) -> Any | None:
        cached = getattr(self, "_audioop_module", ...)
        if cached is not ...:
            return cached
        try:
            import audioop as module  # type: ignore[import-not-found]
        except Exception:
            module = None
        self._audioop_module = module
        return module

    def _scale_frame(self, frame: bytes, volume: float) -> bytes:
        if not frame or abs(volume - 1.0) <= 0.001:
            return frame
        module = self._audioop()
        if module is not None:
            return module.mul(frame, 2, volume)
        return self._samples(frame, volume).tobytes()

    def _samples(self, frame: bytes, volume: float) -> array:
        samples = array("h")
        samples.frombytes(frame)
        if abs(volume - 1.0) > 0.001:
            for i, sample in enumerate(samples):
                samples[i] = self._limit(int(sample * volume))
        return samples

    def _mix_into(self, base: array, frame: bytes, volume: float) -> None:
        if not frame:
            return
        other = self._samples(frame, volume)
        if len(other) < len(base):
            other.extend([0] * (len(base) - len(other)))
        elif len(other) > len(base):
            del other[len(base):]
        for i, sample in enumerate(other):
            base[i] = self._limit(int(base[i]) + int(sample))

    def _mark_first_frame(self, frame: bytes) -> bytes:
        if frame and self.first_frame_ms is None:
            now = time.monotonic()
            self.first_frame_monotonic = now
            self.first_frame_ms = (now - self._started_monotonic) * 1000.0
        return frame

    def _mix_bytes(self, base: bytes, frame: bytes, volume: float) -> bytes:
        if not frame:
            return base
        target_size = len(base)
        if len(frame) < target_size:
            frame = frame + (b"\x00" * (target_size - len(frame)))
        elif len(frame) > target_size:
            frame = frame[:target_size]
        module = self._audioop()
        if module is not None:
            return module.add(base, self._scale_frame(frame, volume), 2)
        base_samples = array("h")
        base_samples.frombytes(base)
        self._mix_into(base_samples, frame, volume)
        return base_samples.tobytes()

    def read(self) -> bytes:
        if self._closed:
            return b""
        with self._lock:
            overlays = list(self._overlays)
        music_frame = b""
        if not self._music_ended:
            music_frame = self.music_source.read()
            if not music_frame:
                self._music_ended = True
                with contextlib.suppress(Exception):
                    self.music_source.cleanup()
        if not music_frame and not overlays:
            self.cleanup()
            return b""
        music_volume = self.normal_music_volume * (self.duck_factor if overlays else 1.0)
        if music_frame and not overlays:
            return self._mark_first_frame(self._scale_frame(music_frame, music_volume))
        if music_frame:
            base = self._scale_frame(music_frame, music_volume)
        else:
            base = b"\x00" * PCM_FRAME_BYTES
        ended: list[dict[str, Any]] = []
        for overlay in overlays:
            source = overlay.get("source")
            future = overlay.get("future")
            try:
                frame = source.read() if source is not None else b""
            except Exception as exc:
                if isinstance(future, asyncio.Future):
                    self._future_exception(future, exc)
                ended.append(overlay)
                continue
            if frame:
                base = self._mix_bytes(base, frame, float(overlay.get("volume") or 1.0))
            else:
                with contextlib.suppress(Exception):
                    source.cleanup()
                if isinstance(future, asyncio.Future):
                    self._future_result(future, None)
                ended.append(overlay)
        if ended:
            with self._lock:
                self._overlays = [ov for ov in self._overlays if ov not in ended]
        return self._mark_first_frame(base)

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.music_source.cleanup()
        with self._lock:
            overlays = list(self._overlays)
            self._overlays.clear()
        for overlay in overlays:
            with contextlib.suppress(Exception):
                overlay.get("source").cleanup()
            future = overlay.get("future")
            if isinstance(future, asyncio.Future):
                self._future_result(future, None)
