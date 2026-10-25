"""Uma fonte PCM para uma música dividida em anexos do mesmo post.

Só a parte atual e a seguinte possuem decoders e buffers transitórios. O
VoiceClient e o mixer permanecem os mesmos durante todas as partes da faixa.
"""
from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import hashlib
import threading
import time
from typing import Any, Awaitable, Callable

import discord

from .buffer_pcm import FRAME_BYTES, SILENCE
from .buffer_pcm import ArchiveOpusPacketReader, BufferedOpusPCMSource, BufferedPCMSource, OpusPCMDecoderSource
from .ciclo_vida import consume_task_result


class _ArchiveOpusHTTPStream:
    """Bounded async CDN download exposed only to the decoder's worker thread.

    Closing this object wakes its reader and schedules cancellation on the
    event loop; voice callbacks never wait for HTTP/socket cleanup.
    """
    MAX_BYTES = 64 * 1024

    def __init__(self, *, loop, index, resolve_segment, stall_seconds):
        self.loop = loop
        self.index = index
        self.resolve_segment = resolve_segment
        self.stall_seconds = stall_seconds
        self._condition = threading.Condition()
        self._chunks = deque()
        self._buffered = 0
        self._closed = False
        self._done = False
        self._error = None
        self._space = asyncio.Event()
        # Construction is performed by _ArchiveOpusStreams on the event loop.
        self._task = loop.create_task(self._pump())
        self._task.add_done_callback(consume_task_result)

    async def _pump(self):
        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=None, connect=min(5.0, self.stall_seconds),
                                            sock_read=min(5.0, self.stall_seconds))
            async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar(),
                                             auto_decompress=False, trust_env=False, read_bufsize=16384) as session:
                for attempt in range(2):
                    segment = await self.resolve_segment(self.index, force_refresh=bool(attempt))
                    # The resolver verifies the attachment ID, author, post,
                    # filename/size and signed CDN path before returning a URL.
                    from .validade_stream import valid_cdn_url
                    url = valid_cdn_url(segment.get("url"))
                    async with session.get(url, allow_redirects=False,
                                           headers={"Accept-Encoding": "identity"}) as response:
                        if response.status in {403, 404} and not attempt:
                            continue  # renew a signed URL before reading bytes
                        if response.status != 200:
                            raise RuntimeError("não consegui abrir a parte Opus do arquivo")
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise ValueError("codificação HTTP incompatível com a parte Opus")
                        expected_size = int(segment["size_bytes"])
                        expected_hash = str(segment["sha256"]).lower()
                        digest, received = hashlib.sha256(), 0
                        while True:
                            with self._condition:
                                if self._closed:
                                    return
                                available = self.MAX_BYTES - self._buffered
                            if not available:
                                self._space.clear()
                                with self._condition:
                                    if self._closed:
                                        return
                                    if self._buffered < self.MAX_BYTES:
                                        continue
                                await self._space.wait()
                                continue
                            chunk = await response.content.read(min(16384, available))
                            if not chunk:
                                if received != expected_size or digest.hexdigest() != expected_hash:
                                    raise ValueError("integridade da parte Opus diferente do manifesto")
                                return
                            received += len(chunk)
                            if received > expected_size:
                                raise ValueError("parte Opus excedeu o tamanho do manifesto")
                            digest.update(chunk)
                            with self._condition:
                                if self._closed:
                                    return
                                self._chunks.append(chunk)
                                self._buffered += len(chunk)
                                self._condition.notify_all()
                raise RuntimeError("URL da parte Opus indisponível")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self._condition:
                self._error = exc
        finally:
            with self._condition:
                self._done = True
                self._condition.notify_all()

    def read(self, size):
        if size <= 0:
            return b""
        deadline = time.monotonic() + self.stall_seconds
        with self._condition:
            while not self._chunks and not self._closed and not self._done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("a parte Opus parou de fornecer bytes")
                self._condition.wait(min(.1, remaining))
            if self._closed:
                return b""
            if self._chunks:
                was_full = self._buffered == self.MAX_BYTES
                chunk = self._chunks.popleft()
                head, tail = chunk[:size], chunk[size:]
                if tail:
                    self._chunks.appendleft(tail)
                self._buffered -= len(head)
                if was_full and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self._space.set)
                return head
            if self._error is not None:
                raise self._error
            return b""

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._chunks.clear()
            self._buffered = 0
            self._condition.notify_all()
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._task.cancel)


class _ArchiveOpusStreams:
    """Own only the current stream and one quota-controlled next prefix."""
    def __init__(self, *, loop, resolve_segment, count, stall_seconds,
                 claim_prepare=None, release_prepare=None, preparing=False):
        self.loop = loop
        self.resolve_segment = resolve_segment
        self.count = count
        self.stall_seconds = stall_seconds
        self.claim_prepare = claim_prepare
        self.release_prepare = release_prepare
        self._owner = ("segment", id(self))
        self._slot_owned = False
        self._playback_active = not preparing
        self._closed = False
        self._lock = threading.RLock()
        self._current = None
        self._next = None
        self._selection = None

    def _new(self, index):
        return _ArchiveOpusHTTPStream(loop=self.loop, index=index,
                                      resolve_segment=self.resolve_segment,
                                      stall_seconds=self.stall_seconds)

    async def _select(self, index):
        with self._lock:
            if self._closed:
                raise RuntimeError("leitura Opus do arquivo encerrada")
            if self._current is not None:
                self._current.close()
            if self._next is not None and self._next.index == index:
                current, self._next = self._next, None
                self._release_slot()
            else:
                if self._next is not None:
                    self._next.close()
                    self._next = None
                    self._release_slot()
                current = self._new(index)
            self._current = current
            return current

    def __call__(self, index):
        # Only ArchiveOpusPacketReader's producer thread calls this method.
        with self._lock:
            if self._closed:
                raise RuntimeError("leitura Opus do arquivo encerrada")
            future = asyncio.run_coroutine_threadsafe(self._select(index), self.loop)
            self._selection = future
        try:
            return future.result(timeout=self.stall_seconds)
        finally:
            with self._lock:
                if self._selection is future:
                    self._selection = None
            if not future.done():
                future.cancel()

    def prefetch(self, index):
        # This callback runs on the loop, so the existing quota may preempt a
        # future track without trying to create a task on the audio thread.
        with self._lock:
            if (self._closed or not self._playback_active or index >= self.count
                    or self._next is not None or self._current is None
                    or self._current.index + 1 != index):
                return
            if self.claim_prepare is not None and not self.claim_prepare(self._owner):
                return
            self._slot_owned = True
            try:
                self._next = self._new(index)
            except BaseException:
                self._release_slot()
                raise

    def _release_slot(self):
        if self._slot_owned:
            self._slot_owned = False
            if self.release_prepare is not None:
                self.release_prepare(self._owner)

    def activate_playback(self):
        with self._lock:
            self._playback_active = True

    def cleanup(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for stream in (self._current, self._next):
                if stream is not None:
                    stream.close()
            self._current = self._next = None
            if self._selection is not None:
                self._selection.cancel()
            self._release_slot()


class ArchiveOpusPCMSource(BufferedOpusPCMSource):
    """One continuous Opus decoder, with a position-preserving late fallback.

    The packet reader proves each part's prefix before exposing its content.
    Already buffered valid frames drain before a failure starts ordinary PCM at
    the actual consumed position. This source never restarts the song at zero.
    """
    def __init__(self, *, segments, resolve_segment, create_fallback, loop,
                 max_frames=75, stall_seconds=12.0, preparing=False,
                 claim_prepare=None, release_prepare=None, stream_factory=None,
                 decoder_factory=None, on_fallback=None):
        self.segments = segments
        self._loop = loop
        self._source_lock = threading.RLock()
        self._consumed_samples = 0
        self._create_fallback = create_fallback
        self._fallback_source = None
        self._fallback_task = None
        self._fallback_pending = False
        self._fallback_error = None
        self._fallback_offset = None
        self._on_fallback = on_fallback
        self._playback_active = not preparing
        self._streams = stream_factory or _ArchiveOpusStreams(
            loop=loop, resolve_segment=resolve_segment, count=len(segments),
            stall_seconds=stall_seconds, preparing=preparing,
            claim_prepare=claim_prepare, release_prepare=release_prepare)
        reader = ArchiveOpusPacketReader(segments, self._streams)
        self._archive_reader = reader
        raw = OpusPCMDecoderSource(reader, decoder_factory=decoder_factory)
        try:
            super().__init__(raw, max_frames=max_frames, stall_seconds=stall_seconds)
        except BaseException:
            raw.cleanup()
            self._streams.cleanup()
            raise

    def _maybe_prefetch(self):
        if not self._playback_active or self._loop.is_closed():
            return
        index = int(self._archive_reader.metadata.get("archive_segment_index", 0))
        if not 0 <= index < len(self.segments) - 1:
            return
        preskip = int(self._archive_reader.metadata.get("pre_skip_samples", 0) or 0)
        position = max(0, self._consumed_samples - preskip) / 48000
        boundary = float(self.segments[index]["offset_seconds"]) + float(self.segments[index]["duration"])
        if boundary - position <= 12.0:
            prefetch = getattr(self._streams, "prefetch", None)
            if callable(prefetch):
                self._loop.call_soon_threadsafe(prefetch, index + 1)

    def activate_playback(self):
        self._playback_active = True
        activate = getattr(self._streams, "activate_playback", None)
        if callable(activate):
            activate()
        self._maybe_prefetch()

    async def _start_fallback(self):
        candidate = None
        try:
            candidate = await self._create_fallback(self._fallback_offset)
            waiter = getattr(candidate, "wait_ready", None)
            if callable(waiter):
                await waiter(timeout=self.stall_seconds)
            with self._source_lock:
                if self._closed:
                    candidate.cleanup()
                else:
                    self._fallback_source = candidate
                    self._fallback_pending = False
                    candidate = None
        except asyncio.CancelledError:
            if candidate is not None:
                candidate.cleanup()
            raise
        except Exception as exc:
            if candidate is not None:
                candidate.cleanup()
            with self._source_lock:
                self._fallback_error = exc
                self._fallback_pending = False

    def _schedule_fallback(self):
        with self._source_lock:
            if self._closed or self._fallback_task is not None:
                return
            self._fallback_task = self._loop.create_task(self._start_fallback())
            self._fallback_task.add_done_callback(consume_task_result)
            if callable(self._on_fallback):
                self._on_fallback(self._fallback_offset)

    def read(self):
        with self._source_lock:
            self.last_opus_packet = None
            if self._closed:
                self.last_read_had_audio = False
                return b""
            fallback = self._fallback_source
            if self._fallback_error is not None:
                raise self._fallback_error
            if self._fallback_pending:
                self.last_read_had_audio = False
                return SILENCE
        if fallback is not None:
            frame = fallback.read()
            self.last_read_had_audio = bool(frame and getattr(fallback, "last_read_had_audio", True))
            self.passthrough_reason = "archive_read_fallback"
            return frame
        try:
            frame = super().read()
        except Exception:
            with self._source_lock:
                if not self._consumed_samples:
                    raise  # startup uses the ordinary preparation retry
                self.passthrough_reason = "archive_read_fallback"
                self.last_read_had_audio = False
                preskip = int(self._archive_reader.metadata.get("pre_skip_samples", 0) or 0)
                self._fallback_offset = max(0, self._consumed_samples - preskip) / 48000
                self._fallback_pending = True
            self.source.cleanup()
            self._streams.cleanup()
            self._loop.call_soon_threadsafe(self._schedule_fallback)
            return SILENCE
        if frame and self.last_read_had_audio:
            self._consumed_samples += len(frame) // 4
            self._maybe_prefetch()
        return frame

    def set_max_frames(self, value):
        super().set_max_frames(value)
        with self._source_lock:
            fallback = self._fallback_source
        setter = getattr(fallback, "set_max_frames", None)
        if callable(setter):
            setter(value)

    def audio_buffer_metrics(self):
        result = super().audio_buffer_metrics()
        metadata = self._archive_reader.metadata
        result.update({key: metadata.get(key, 0) for key in (
            "archive_segment_index", "archive_segments_completed", "archive_preroll_packets_skipped",
            "archive_preroll_samples_skipped")})
        result.update(archive_reader_continuous=True,
                      archive_http_queue_max_bytes=2 * _ArchiveOpusHTTPStream.MAX_BYTES,
                      archive_http_read_bufsize=16384,
                      archive_opus_fallback=self._fallback_offset is not None)
        return result

    def cleanup(self):
        # Stream.close only cancels async HTTP work. No socket read or thread
        # join runs on the voice callback/event loop.
        with self._source_lock:
            if self._closed:
                return
            fallback, self._fallback_source = self._fallback_source, None
            task = self._fallback_task
            # Publish the parent's closed state while holding the same lock
            # used to attach a completed fallback. A racing completion must
            # clean its candidate instead of attaching it after our capture.
            self._streams.cleanup()
            super().cleanup()
        if task is not None and not task.done() and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(task.cancel)
        if fallback is not None:
            fallback.cleanup()


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
