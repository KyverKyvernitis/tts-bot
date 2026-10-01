"""Uma fonte PCM para uma música dividida em anexos do mesmo post.

Só a parte atual e a seguinte possuem decoders e buffers transitórios. O
VoiceClient e o mixer permanecem os mesmos durante todas as partes da faixa.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any, Awaitable, Callable

import discord

from .buffer_pcm import FRAME_BYTES, SILENCE
from .buffer_pcm import BufferedPCMSource
from .ciclo_vida import consume_task_result


class ExactSegmentPCMSource(discord.AudioSource):
    """Preserva o último bloco curto que FFmpegPCMAudio.read descartaria."""
    def __init__(self, decoder: Any) -> None:
        self.decoder = decoder

    @property
    def _current_error(self) -> Any:
        return getattr(self.decoder, "_current_error", None)

    def read(self) -> bytes:
        stdout = getattr(self.decoder, "_stdout", None)
        if stdout is None:
            return self.decoder.read()
        frame = stdout.read(FRAME_BYTES)
        if not frame:
            checker = getattr(self.decoder, "_check_process_returncode", None)
            if callable(checker):
                checker()
        return frame

    def cleanup(self) -> None:
        self.decoder.cleanup()


class ArchiveSegmentedPCMSource(discord.AudioSource):
    def __init__(
        self, *, segments: list[dict], start_index: int, start_offset: float,
        create_segment: Callable[[int, float], Awaitable[Any]], loop: asyncio.AbstractEventLoop,
        max_frames: int = 75, stall_seconds: float = 12.0, playback_speed: float = 1.0,
        lead_seconds: float = 12.0, preparing: bool = False,
        claim_prepare: Callable[[Any], bool] | None = None,
        release_prepare: Callable[[Any], None] | None = None,
    ) -> None:
        self.segments = segments
        self.index = start_index
        self._offset = start_offset
        self._create_segment = create_segment
        self._loop = loop
        self._max_frames = max_frames
        self._stall_seconds = stall_seconds
        self._speed = max(0.1, playback_speed)
        self._lead_seconds = max(1.0, lead_seconds)
        self._playback_active = not preparing
        self._claim_prepare = claim_prepare
        self._release_prepare = release_prepare
        self._prepare_owner = ("segment", id(self))
        self._prepare_slot_owned = False
        self._lock = threading.RLock()
        self._closed = False
        self._current: Any = None
        self._next: Any = None
        self._next_task: asyncio.Task | None = None
        self._initial_task = loop.create_task(self._prepare_initial())
        self._initial_task.add_done_callback(consume_task_result)
        self._next_error: BaseException | None = None
        self._frames_read = 0
        self._tail = b""
        self._gap_since: float | None = None
        self._transition_wait_frames = 0
        self._segments_completed = 0
        self.last_read_had_audio = False

    def is_opus(self) -> bool:
        return False

    async def _make_ready(self, index: int, offset: float) -> Any:
        source = await self._create_segment(index, offset)
        try:
            source.set_max_frames(self._max_frames)
            await source.wait_ready(timeout=self._stall_seconds)
            return source
        except BaseException:
            source.cleanup()
            raise

    async def _prepare_initial(self) -> None:
        source = await self._make_ready(self.index, self._offset)
        with self._lock:
            if self._closed:
                source.cleanup()
                return
            self._current = source
        self._maybe_prepare_next()

    def _maybe_prepare_next(self, required: bool = False) -> None:
        with self._lock:
            if (self._closed or not self._playback_active or self.index + 1 >= len(self.segments)
                    or self._next is not None or self._next_task is not None or self._prepare_slot_owned):
                return
            remaining = (float(self.segments[self.index]["duration"]) - self._offset) / self._speed - self._frames_read * 0.02
            if remaining > self._lead_seconds:
                return
            if not required:
                if self._claim_prepare is not None and not self._claim_prepare(self._prepare_owner):
                    return
                self._prepare_slot_owned = True
            # No EOF a parte seguinte substitui o decoder de playback. Ela
            # não é mais uma fonte extra e não espera outra call liberar cota.
            self._next_task = self._loop.create_task(self._prepare_next(self.index + 1, replace_current=required))
            self._next_task.add_done_callback(consume_task_result)

    async def _prepare_next(self, index: int, *, replace_current: bool = False) -> None:
        try:
            if replace_current:
                with self._lock:
                    old = self._current
                # Mantém a referência ao EOF para read() continuar verificando
                # erro, timeout e cauda, mas fecha os recursos antes de abrir
                # o substituto. A thread de voz apenas agenda este trabalho.
                if old is not None:
                    await asyncio.to_thread(old.cleanup)
                with self._lock:
                    if self._closed:
                        return
            source = await self._make_ready(index, 0.0)
            with self._lock:
                if self._closed or self.index + 1 != index:
                    source.cleanup()
                else:
                    self._next = source
        except asyncio.CancelledError:
            self._release_prepare_slot()
            raise
        except Exception as exc:
            with self._lock:
                self._next_error = exc
            self._release_prepare_slot()

    def _release_prepare_slot(self) -> None:
        with self._lock:
            if self._prepare_slot_owned:
                self._prepare_slot_owned = False
                if self._release_prepare is not None:
                    self._release_prepare(self._prepare_owner)

    def activate_playback(self) -> None:
        with self._lock:
            self._playback_active = True
        self._loop.call_soon_threadsafe(self._maybe_prepare_next)

    async def wait_ready(self, *, timeout: float, min_frames: int = 1) -> None:
        started = time.monotonic()
        await asyncio.wait_for(asyncio.shield(self._initial_task), timeout=timeout)
        with self._lock:
            source = self._current
        if source is None:
            raise RuntimeError("fonte segmentada encerrada")
        await source.wait_ready(timeout=max(.01, timeout - (time.monotonic() - started)), min_frames=min_frames)

    def set_max_frames(self, max_frames: int) -> None:
        with self._lock:
            self._max_frames = max_frames
            for source in (self._current, self._next):
                if source is not None:
                    source.set_max_frames(max_frames)

    def read(self) -> bytes:
        with self._lock:
            self.last_read_had_audio = False
            if self._closed:
                return b""
            while self._current is not None:
                frame = self._current.read()
                if frame:
                    self.last_read_had_audio = bool(getattr(self._current, "last_read_had_audio", True))
                    if self.last_read_had_audio:
                        self._frames_read += len(frame) / FRAME_BYTES
                        self._gap_since = None
                        # Só agenda no loop; a thread de voz nunca faz REST ou
                        # inicia subprocessos de preparação.
                        remaining = (float(self.segments[self.index]["duration"]) - self._offset) / self._speed - self._frames_read * .02
                        if (self.index + 1 < len(self.segments) and self._next_task is None
                                and self._next is None and remaining <= self._lead_seconds):
                            self._loop.call_soon_threadsafe(self._maybe_prepare_next)
                        if self._tail or len(frame) < FRAME_BYTES:
                            joined = self._tail + frame
                            if len(joined) < FRAME_BYTES:
                                self._tail = joined
                                continue
                            self._tail = joined[FRAME_BYTES:]
                            return joined[:FRAME_BYTES]
                    return frame
                expected = max(0.0, float(self.segments[self.index]["duration"]) - self._offset) / self._speed
                if expected - self._frames_read * 0.02 > max(.12, expected * .0001):
                    raise RuntimeError("parte do arquivo encerrou antes da duração verificada")
                if self.index + 1 >= len(self.segments):
                    if self._tail:
                        frame, self._tail = self._tail.ljust(FRAME_BYTES, b"\0"), b""
                        self.last_read_had_audio = True
                        return frame
                    return b""
                if self._next_error is not None:
                    raise self._next_error
                if self._next is not None:
                    old = self._current
                    self._current, self._next = self._next, None
                    self._next_task = None
                    self.index += 1
                    self._segments_completed += 1
                    self._offset = 0.0
                    self._frames_read = 0
                    try:
                        old.cleanup()
                    finally:
                        self._release_prepare_slot()
                    continue  # Primeiro frame da parte nova neste mesmo tick.
                self._loop.call_soon_threadsafe(self._maybe_prepare_next, True)
                now = time.monotonic()
                self._gap_since = self._gap_since or now
                if now - self._gap_since >= self._stall_seconds:
                    raise TimeoutError("a próxima parte do arquivo não ficou pronta")
                self._transition_wait_frames += 1
                self.last_read_had_audio = False
                return SILENCE
            return SILENCE

    def audio_buffer_metrics(self) -> dict[str, Any]:
        with self._lock:
            sources = [source for source in (self._current, self._next) if source is not None]
            return {
                "archive_segment_index": self.index,
                "archive_segments_completed": self._segments_completed,
                "archive_transition_wait_frames": self._transition_wait_frames,
                "buffer_max_bytes": 2 * self._max_frames * FRAME_BYTES,
                "buffer_underruns": sum(source.audio_buffer_metrics().get("buffer_underruns", 0) for source in sources),
            }

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._release_prepare_slot()
            for source in (self._current, self._next):
                if source is not None:
                    with contextlib.suppress(Exception):
                        source.cleanup()
            self._current = self._next = None
            self._tail = b""
            tasks = (self._initial_task, self._next_task)
        for task in tasks:
            if task is not None and not task.done():
                self._loop.call_soon_threadsafe(task.cancel)


class _PCMInputPipe:
    """Reader consumido pelo writer FFmpeg, com backpressure e sem disco."""
    def __init__(self, source: ArchiveSegmentedPCMSource) -> None:
        self.source = source
        self.error: Exception | None = None

    def read(self, size: int) -> bytes:
        try:
            while True:
                frame = self.source.read()
                if not frame or self.source.last_read_had_audio:
                    return frame
                # O pipe é um consumidor sem relógio de voz. Não gere segundos
                # de silêncio artificial enquanto o CDN prepara a parte nova.
                time.sleep(0.005)
        except Exception as exc:
            self.error = exc
            return b""  # Fecha stdin; EOF do decoder entrega o erro ao buffer.


class _FilteredPCM(discord.AudioSource):
    def __init__(self, decoder: Any, adapter: _PCMInputPipe) -> None:
        self.decoder = decoder
        self.adapter = adapter

    @property
    def _current_error(self) -> Any:
        return getattr(self.decoder, "_current_error", None)

    def read(self) -> bytes:
        frame = self.decoder.read()
        if not frame and self.adapter.error is not None:
            raise self.adapter.error
        return frame

    def cleanup(self) -> None:
        self.adapter.source.cleanup()
        self.decoder.cleanup()


class ContinuousArchiveDSPSource(discord.AudioSource):
    """Aplica nightcore/reverb uma vez, com estado contínuo entre as partes."""
    def __init__(self, raw: ArchiveSegmentedPCMSource, *, executable: str, options: str,
                 max_frames: int, stall_seconds: float, loop: asyncio.AbstractEventLoop) -> None:
        self.raw = raw
        self._loop = loop
        self._max_frames = max_frames
        self._stall_seconds = stall_seconds
        self._output: BufferedPCMSource | None = None
        self._closed = False
        self._lock = threading.Lock()
        self.last_read_had_audio = False
        self._task = loop.create_task(self._start(executable, options))
        self._task.add_done_callback(consume_task_result)

    async def _start(self, executable: str, options: str) -> None:
        await self.raw.wait_ready(timeout=self._stall_seconds)
        with self._lock:
            if self._closed:
                return
            adapter = _PCMInputPipe(self.raw)
            decoder = discord.FFmpegPCMAudio(adapter, pipe=True, executable=executable,
                                             before_options="-f s16le -ar 48000 -ac 2", options=options)
            self._output = BufferedPCMSource(_FilteredPCM(ExactSegmentPCMSource(decoder), adapter), max_frames=self._max_frames,
                                            stall_seconds=self._stall_seconds)

    async def wait_ready(self, *, timeout: float, min_frames: int = 1) -> None:
        started = time.monotonic()
        await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        with self._lock:
            output = self._output
        if output is None:
            raise RuntimeError("DSP do arquivo encerrado")
        await output.wait_ready(timeout=max(.01, timeout - (time.monotonic() - started)), min_frames=min_frames)

    def is_opus(self) -> bool:
        return False

    def activate_playback(self) -> None:
        self.raw.activate_playback()

    def read(self) -> bytes:
        with self._lock:
            self.last_read_had_audio = False
            if self._closed:
                return b""
            if self._output is None:
                return SILENCE
            frame = self._output.read()
            self.last_read_had_audio = self._output.last_read_had_audio
            return frame

    def set_max_frames(self, value: int) -> None:
        with self._lock:
            self._max_frames = value
            self.raw.set_max_frames(value)
            if self._output is not None:
                self._output.set_max_frames(value)

    def audio_buffer_metrics(self) -> dict[str, Any]:
        with self._lock:
            metrics = self.raw.audio_buffer_metrics()
            if self._output is not None:
                output = self._output.audio_buffer_metrics()
                metrics["buffer_max_bytes"] += output["buffer_max_bytes"]
                metrics["buffer_underruns"] += output["buffer_underruns"]
            metrics["archive_continuous_dsp"] = True
            return metrics

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.raw.cleanup()
            if self._output is not None:
                self._output.cleanup()
        if not self._task.done():
            self._loop.call_soon_threadsafe(self._task.cancel)
