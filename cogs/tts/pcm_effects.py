"""Apply personal playback effects once to a progressive stereo PCM mix."""
from __future__ import annotations

import contextlib
import subprocess
import threading


class _FilteredPCM:
    FRAME_BYTES = 3840

    def __init__(self, buffer, effect_filter):
        self.buffer = buffer
        self._closed = threading.Event()
        self._ended = False
        self._feed_error = None
        self._cleanup_lock = threading.Lock()
        self._process = subprocess.Popen([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-probesize', '32', '-analyzeduration', '0',
            '-f', 's16le', '-ar', '48000', '-ac', '2', '-i', 'pipe:0',
            '-vn', '-af', str(effect_filter), '-ac', '2', '-ar', '48000',
            '-f', 's16le', '-flush_packets', '1', 'pipe:1',
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0)
        self._feeder = threading.Thread(target=self._feed, name='ttv-pcm-effects', daemon=True)
        self._feeder.start()

    def _feed(self):
        try:
            while not self._closed.is_set():
                frame = self.buffer.read()
                if not frame:
                    break
                remaining = memoryview(frame)
                while remaining and not self._closed.is_set():
                    written = self._process.stdin.write(remaining)
                    if not written:
                        raise BrokenPipeError('PCM effects input closed')
                    remaining = remaining[written:]
        except BaseException as error:
            if not self._closed.is_set():
                self._feed_error = error if isinstance(error, Exception) else RuntimeError('PCM producer cancelled')
        finally:
            with contextlib.suppress(OSError, ValueError):
                self._process.stdin.close()

    def read(self):
        if self._closed.is_set() or self._ended:
            return b''
        output = bytearray()
        try:
            while len(output) < self.FRAME_BYTES:
                data = self._process.stdout.read(self.FRAME_BYTES - len(output))
                if not data:
                    self._ended = True
                    code = self._process.wait(timeout=5)
                    self._feeder.join(timeout=1)
                    if self._closed.is_set():
                        return b''
                    if self._feed_error is not None:
                        raise self._feed_error
                    if code:
                        raise RuntimeError(f'FFmpeg PCM effects failed (exit {code})')
                    break
                output.extend(data)
        except (OSError, ValueError):
            if self._closed.is_set():
                return b''
            raise
        if output and len(output) < self.FRAME_BYTES:
            output.extend(b'\0' * (self.FRAME_BYTES - len(output)))
        return bytes(output)

    def cleanup(self):
        with self._cleanup_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            # Wake a feeder waiting for native synthesis before stopping its
            # pipe, and stop FFmpeg before waiting for a potentially full pipe.
            self.buffer.cleanup()
            if self._process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=5)
            self._feeder.join(timeout=5)
            for pipe in (self._process.stdin, self._process.stdout):
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()


def filtered_pcm_source(buffer, base_class, effect_filter):
    """Adapt RAM PCM to Discord, retaining the neutral path without FFmpeg."""
    if not effect_filter:
        return buffer.as_audio_source(base_class)
    filtered = _FilteredPCM(buffer, effect_filter)

    class FilteredSource(base_class):
        def read(self):
            return filtered.read()

        def is_opus(self):
            return False

        def cleanup(self):
            filtered.cleanup()

        def __getattr__(self, name):
            if hasattr(filtered, name):
                return getattr(filtered, name)
            return getattr(buffer, name)

    return FilteredSource()
