"""Optional bounded cache of complete 20 ms Opus packets for frequent phrases."""
from collections import OrderedDict
import asyncio
import contextlib
import os
import threading
import time
import discord
from .runtime import await_physical_completion, cancel_task_once

class OpusFramesSource(getattr(discord, 'AudioSource', object)):
    def __init__(self, frames):
        self.frames = frames
        self.index = 0
    def read(self):
        if self.index >= len(self.frames):
            return b''
        frame = self.frames[self.index]
        self.index += 1
        return frame
    def is_opus(self):
        return True
    def cleanup(self):
        self.frames = ()

class PreparedOpusCache:
    def __init__(self, *, max_bytes=8 * 1024 * 1024, max_entries=128, ttl=600,
                 max_pending=8, idle_poll_seconds=.2):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl
        self.entries = OrderedDict()
        self.total = 0
        self.hits = OrderedDict()
        self.pending = set()
        self.busy = False
        self.max_pending = max(1, int(max_pending))
        self.idle_poll_seconds = max(.01, float(idle_poll_seconds))
        self._encoder_lock = asyncio.Lock()
        self._tasks = set()
        self.closed = False

    def key(self, path, options, *, before_options='-nostdin'):
        return self._stat_key(path, options, os.stat(path), before_options=before_options)

    @staticmethod
    def _stat_key(path, options, st, *, before_options='-nostdin'):
        return (os.path.abspath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                options, before_options)

    def get(self, key):
        value = self.entries.get(key)
        if value is None:
            return None
        created, size, frames = value
        if time.monotonic() - created > self.ttl:
            self.entries.pop(key)
            self.total -= size
            return None
        self.entries.move_to_end(key)
        return OpusFramesSource(frames)

    def put(self, key, frames):
        frames = tuple(frames)
        size = sum(len(frame) for frame in frames)
        if not frames or size > min(512 * 1024, self.max_bytes):
            return
        old = self.entries.pop(key, None)
        if old:
            self.total -= old[1]
        while self.entries and (self.total + size > self.max_bytes or len(self.entries) >= self.max_entries):
            _, (_, removed, _) = self.entries.popitem(last=False)
            self.total -= removed
        self.entries[key] = (time.monotonic(), size, frames)
        self.total += size

    def repeated(self, key):
        self.hits[key] = self.hits.get(key, 0) + 1
        self.hits.move_to_end(key)
        while len(self.hits) > 256:
            self.hits.popitem(last=False)
        return self.hits[key] >= 2

    def close(self):
        """Stop admission; pending tasks retain leases until physical cleanup."""
        self.closed = True
        for task in tuple(self._tasks):
            cancel_task_once(task)
        self.entries.clear()
        self.hits.clear()
        self.total = 0

    async def aclose(self):
        self.close()
        tasks = tuple(task for task in self._tasks if task is not asyncio.current_task())
        if tasks:
            await await_physical_completion(asyncio.gather(*tasks, return_exceptions=True))

    async def prepare(self, key, path, options, *, idle, before_options='-nostdin'):
        if self.closed or key in self.pending or len(self.pending) >= self.max_pending:
            return
        self.pending.add(key)
        task = asyncio.current_task()
        self._tasks.add(task)
        audio_file = None
        try:
            # Keep the selected inode alive while waiting for playback to finish.
            # The cleanup process also respects this shared file lock.
            import fcntl
            audio_file = open(path, 'rb')
            fcntl.flock(audio_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            if self._stat_key(path, options, os.fstat(audio_file.fileno()), before_options=before_options) != key:
                return
            queued_until = time.monotonic() + max(1.0, float(self.ttl))
            async with self._encoder_lock:
                while not idle():
                    if (self.closed or time.monotonic() >= queued_until
                            or self.key(path, options, before_options=before_options) != key):
                        return
                    await asyncio.sleep(self.idle_poll_seconds)
                if (self.closed or time.monotonic() >= queued_until
                        or self.key(path, options, before_options=before_options) != key):
                    return
                self.busy = True
                try:
                    frames = await self._encode(audio_file, options, idle=idle, before_options=before_options)
                    if frames and not self.closed and self.key(path, options, before_options=before_options) == key:
                        self.put(key, frames)
                finally:
                    self.busy = False
        except (OSError, RuntimeError, asyncio.TimeoutError):
            pass
        finally:
            if audio_file is not None:
                audio_file.close()
            self.pending.discard(key)
            self._tasks.discard(task)

    async def _encode(self, audio_file, options, *, idle, before_options='-nostdin'):
        stop = threading.Event()
        holder = []
        holder_lock = threading.Lock()
        future = None

        def cleanup_source():
            with holder_lock:
                source = holder.pop() if holder else None
            if source is not None:
                with contextlib.suppress(Exception):
                    source.cleanup()
                writer = getattr(source, '_pipe_writer_thread', None)
                if writer is not None and writer is not threading.current_thread():
                    # discord.py owns a second thread when pipe=True. Its last
                    # read must finish before the leased descriptor is closed.
                    writer.join()

        try:
            def encode():
                if stop.is_set():
                    return None
                # An explicit codec='libopus' means COPY in discord.py. Omit it
                # to encode MP3/WAV, and use the leased descriptor rather than
                # reopening a pathname that an atomic cache update may replace.
                source = discord.FFmpegOpusAudio(audio_file, pipe=True,
                                               before_options=before_options,
                                               options=options, bitrate=64)
                with holder_lock:
                    holder.append(source)
                frames, size = [], 0
                try:
                    while not stop.is_set():
                        packet = source.read()
                        if not packet:
                            process = getattr(source, '_process', None)
                            if process is not None and process.wait(timeout=.5) != 0:
                                return None
                            return frames
                        if not frames and packet.startswith((b'OpusHead', b'OpusTags')):
                            # FFmpegOpusAudio exposes the Ogg headers as well
                            # as audio packets. Discord playback needs only the
                            # complete 20 ms Opus frames stored in this cache.
                            continue
                        size += len(packet)
                        if size > min(512 * 1024, self.max_bytes) or len(frames) >= 2000:
                            return None
                        frames.append(packet)
                finally:
                    cleanup_source()
                return None
            future = asyncio.create_task(asyncio.to_thread(encode))
            deadline = time.monotonic() + 3.0
            while not future.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                if self.closed or not idle():
                    return None
                await asyncio.wait({future}, timeout=min(self.idle_poll_seconds, remaining))
            return future.result()
        finally:
            stop.set()
            cleanup = asyncio.create_task(asyncio.to_thread(cleanup_source))
            with contextlib.suppress(BaseException):
                await await_physical_completion(cleanup)
            if future is not None and not future.done():
                with contextlib.suppress(BaseException):
                    await await_physical_completion(future)
