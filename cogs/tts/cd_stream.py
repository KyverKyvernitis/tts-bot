"""RAM-backed PCM playback for progressive CD singing.

The producer can append complete synchronized windows from any thread. The
Discord audio thread consumes one 20 ms frame at a time. This module has no
Discord dependency; ``as_audio_source`` creates the SDK adapter at the edge.
"""
from __future__ import annotations

import asyncio
from collections import deque
import math
import threading
import time


class PCMStreamCancelled(RuntimeError):
    """The owner stopped a progressive singing presentation."""


class PCMStreamBuffer:
    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2
    FRAME_MS = 20
    FRAME_BYTES = 3840
    BYTES_PER_MS = 192

    def __init__(self, total_duration_ms, *, max_bytes=512 * 1024 * 1024,
                 prebuffer_ms=500, safety_ms=250, read_timeout_seconds=120,
                 clock=time.monotonic):
        self.total_duration_ms = self._number(total_duration_ms, 'total_duration_ms', positive=True)
        self.prebuffer_ms = self._number(prebuffer_ms, 'prebuffer_ms')
        self.safety_ms = self._number(safety_ms, 'safety_ms')
        self.read_timeout_seconds = self._number(read_timeout_seconds, 'read_timeout_seconds', positive=True)
        if type(max_bytes) is not int or max_bytes < self.FRAME_BYTES:
            raise ValueError('max_bytes must hold at least one PCM frame')
        self.max_bytes = max_bytes
        self._clock = clock
        self.started_at = clock()
        self._last_append_at = self.started_at
        self._condition = threading.Condition(threading.RLock())
        self._chunks = deque()
        self._head_offset = 0
        self._buffered_bytes = 0
        self._produced_bytes = 0
        self._consumed_bytes = 0
        self._generation_ms = 0.0
        self._done = False
        self._closed = False
        self._cancelled = False
        self._error = None
        self._ready = False
        self.ready_at = None
        self.first_frame_at = None
        self.first_non_silent_at = None
        self.frames_read = 0
        self.stop = threading.Event()
        self.foreground = threading.Event()

    @staticmethod
    def _number(value, label, *, positive=False):
        if type(value) not in (int, float):
            raise ValueError(label + ' must be finite')
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError(label + ' must be finite') from exc
        if not math.isfinite(number) or (number <= 0 if positive else number < 0):
            raise ValueError(label + ' must be finite and nonnegative')
        return number

    def _required_prebuffer_locked(self):
        produced_ms = self._produced_bytes / self.BYTES_PER_MS
        if not produced_ms:
            return min(self.total_duration_ms, self.prebuffer_ms + self.safety_ms)
        ratio = self._generation_ms / produced_ms
        remaining_ms = max(0.0, self.total_duration_ms - produced_ms)
        # If production is slower than playback, accumulate its estimated
        # remaining deficit before starting. Extra RAM stores that head start;
        # it cannot make missing future samples arrive faster.
        deficit_ms = max(0.0, ratio - 1.0) * remaining_ms
        return min(self.total_duration_ms,
                   max(self.prebuffer_ms, deficit_ms) + self.safety_ms)

    def _update_ready_locked(self):
        if (not self._ready and self._buffered_bytes and not self._error
                and (self._done or self._buffered_bytes / self.BYTES_PER_MS
                     >= self._required_prebuffer_locked())):
            self._ready = True
            self.ready_at = self._clock()

    def append(self, pcm, *, generation_ms=None):
        """Append immutable little-endian stereo PCM16 without disk spill.

        ``generation_ms`` optionally supplies active generation time for this
        window; otherwise wall time since the previous append is measured.
        The byte cap limits queued PCM, rather than total lifetime output.
        """
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise TypeError('PCM must be bytes-like')
        if len(pcm) % (self.CHANNELS * self.SAMPLE_WIDTH):
            raise ValueError('PCM must contain complete stereo samples')
        supplied_ms = None if generation_ms is None else self._number(generation_ms, 'generation_ms')
        with self._condition:
            if self._closed or self._cancelled:
                raise PCMStreamCancelled('progressive CD stream stopped')
            if self._done:
                raise RuntimeError('cannot append to a finished PCM stream')
            if not pcm:
                return
            if self._buffered_bytes + len(pcm) > self.max_bytes:
                error = BufferError('progressive CD PCM memory budget exceeded')
                self._error, self._done = error, True
                self.stop.set()
                self._condition.notify_all()
                raise error
            now = self._clock()
            elapsed_ms = max(0.0, (now - self._last_append_at) * 1000)
            self._generation_ms += elapsed_ms if supplied_ms is None else supplied_ms
            self._last_append_at = now
            chunk = bytes(pcm)
            self._chunks.append(chunk)
            self._produced_bytes += len(chunk)
            self._buffered_bytes += len(chunk)
            self._update_ready_locked()
            self._condition.notify_all()

    async def append_async(self, pcm, *, generation_ms=None):
        # In-memory append never waits on the consumer or performs file I/O.
        self.append(pcm, generation_ms=generation_ms)

    def finish(self, error=None):
        if error is not None and not isinstance(error, BaseException):
            raise TypeError('stream error must be an exception')
        if error is not None and not isinstance(error, Exception):
            # Discord catches Exception in its player thread. Task cancellation
            # must surface there without escaping its completion callback.
            error = PCMStreamCancelled('progressive CD producer cancelled')
        with self._condition:
            if self._closed or self._cancelled or self._done:
                return
            self._error, self._done = error, True
            if error is not None:
                self.stop.set()
            self._update_ready_locked()
            self._condition.notify_all()

    def cancel(self, error=None):
        if error is not None and not isinstance(error, BaseException):
            raise TypeError('stream error must be an exception')
        if error is not None and not isinstance(error, Exception):
            error = PCMStreamCancelled('progressive CD producer cancelled')
        with self._condition:
            if self._closed:
                return
            self._cancelled = True
            self._done = True
            self._error = error if error is not None else PCMStreamCancelled('progressive CD stream cancelled')
            self.stop.set()
            self._chunks.clear()
            self._head_offset = 0
            self._buffered_bytes = 0
            self._condition.notify_all()

    def cleanup(self):
        """Release queued PCM and wake readers during normal source cleanup."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self.stop.set()
            self._chunks.clear()
            self._head_offset = 0
            self._buffered_bytes = 0
            self._condition.notify_all()

    close = cleanup

    def promote(self):
        self.foreground.set()

    async def wait_ready(self, timeout=None):
        """Wait for enough estimated runway or for successful complete audio.

        A short asyncio timer keeps the wait cancellation-safe without leaving
        executor threads blocked after a cancelled consumer or timeout.
        """
        duration = None if timeout is None else self._number(timeout, 'timeout', positive=True)
        deadline = None if duration is None else time.monotonic() + duration
        while True:
            with self._condition:
                if self._error is not None:
                    raise self._error
                if self._closed:
                    raise PCMStreamCancelled('progressive CD stream stopped')
                self._update_ready_locked()
                if self._ready:
                    return self
                if self._done:
                    raise RuntimeError('progressive CD produced no PCM')
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('progressive CD first audio timed out')
            await asyncio.sleep(.01)

    def read(self):
        """Read one Discord PCM frame, padding only a true final short frame."""
        deadline = time.monotonic() + self.read_timeout_seconds
        with self._condition:
            while self._buffered_bytes < self.FRAME_BYTES and not self._done:
                if self._closed:
                    return b''
                if self._error is not None:
                    raise self._error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = TimeoutError('progressive CD playback stalled')
                    self._error, self._done = error, True
                    self.stop.set()
                    self._condition.notify_all()
                    raise error
                self._condition.wait(remaining)
            if self._closed:
                return b''
            if self._error is not None:
                raise self._error
            if not self._buffered_bytes:
                return b''
            size = min(self.FRAME_BYTES, self._buffered_bytes)
            output = bytearray()
            remaining = size
            while remaining:
                chunk = self._chunks[0]
                count = min(remaining, len(chunk) - self._head_offset)
                output.extend(chunk[self._head_offset:self._head_offset + count])
                self._head_offset += count
                remaining -= count
                if self._head_offset == len(chunk):
                    self._chunks.popleft()
                    self._head_offset = 0
            self._buffered_bytes -= size
            self._consumed_bytes += size
            self.frames_read += 1
            now = self._clock()
            if self.first_frame_at is None:
                self.first_frame_at = now
            if self.first_non_silent_at is None and any(output):
                self.first_non_silent_at = now
            if size < self.FRAME_BYTES:
                output.extend(b'\0' * (self.FRAME_BYTES - size))
            self._condition.notify_all()
            return bytes(output)

    def is_opus(self):
        return False

    def as_audio_source(self, base_class):
        """Create a genuine ``discord.AudioSource`` adapter lazily."""
        stream = self

        class PCMSource(base_class):
            def read(self):
                return stream.read()

            def is_opus(self):
                return False

            def cleanup(self):
                stream.cleanup()

            def __getattr__(self, name):
                return getattr(stream, name)

        return PCMSource()

    @property
    def done(self):
        with self._condition:
            return self._done

    @property
    def ended(self):
        return self.done

    @property
    def closed(self):
        with self._condition:
            return self._closed

    @property
    def cancelled(self):
        with self._condition:
            return self._cancelled

    @property
    def error(self):
        with self._condition:
            return self._error

    @property
    def ready(self):
        with self._condition:
            return self._ready and not self._error and not self._closed

    @property
    def produced_bytes(self):
        with self._condition:
            return self._produced_bytes

    @property
    def buffered_bytes(self):
        with self._condition:
            return self._buffered_bytes

    @property
    def consumed_bytes(self):
        with self._condition:
            return self._consumed_bytes

    @property
    def produced_ms(self):
        return self.produced_bytes / self.BYTES_PER_MS

    @property
    def buffered_ms(self):
        return self.buffered_bytes / self.BYTES_PER_MS

    @property
    def playback_ms(self):
        return self.consumed_bytes / self.BYTES_PER_MS

    @property
    def generation_ms(self):
        with self._condition:
            return self._generation_ms

    @property
    def generation_ratio(self):
        with self._condition:
            return self._generation_ms / (self._produced_bytes / self.BYTES_PER_MS) if self._produced_bytes else 0.0

    @property
    def required_prebuffer_ms(self):
        with self._condition:
            return self._required_prebuffer_locked()
