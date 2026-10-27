"""Native, stateful PCM conversion and optional vectorized sample operations.

The converter uses the same libswresample defaults as FFmpeg. Each independent
stem owns its filter state; adjacent callbacks within that stem share it.
Neither a voice boundary nor a MIDI boundary is resampled independently.
"""
from __future__ import annotations

import array
import ctypes
import ctypes.util
from functools import lru_cache
import sys

try:
    import numpy as _np
except ImportError:  # NumPy is optional on both the VPS and Termux.
    _np = None


class PCMConversionError(RuntimeError):
    pass


class _LayoutUnion(ctypes.Union):
    _fields_ = [('mask', ctypes.c_uint64), ('map', ctypes.c_void_p)]


class _ChannelLayout(ctypes.Structure):
    _fields_ = [('order', ctypes.c_int), ('nb_channels', ctypes.c_int),
                ('u', _LayoutUnion), ('opaque', ctypes.c_void_p)]


@lru_cache(maxsize=1)
def _swresample():
    """Resolve a supported public ABI once, without depending on an SDK."""
    name = ctypes.util.find_library('swresample')
    if not name:
        return None
    try:
        library = ctypes.CDLL(name)
        if hasattr(library, 'swr_alloc_set_opts2'):
            library.swr_alloc_set_opts2.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(_ChannelLayout), ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(_ChannelLayout), ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_void_p]
            library.swr_alloc_set_opts2.restype = ctypes.c_int
        elif hasattr(library, 'swr_alloc_set_opts'):
            library.swr_alloc_set_opts.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            library.swr_alloc_set_opts.restype = ctypes.c_void_p
        else:
            return None
        planes = ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8))
        library.swr_init.argtypes = [ctypes.c_void_p]
        library.swr_init.restype = ctypes.c_int
        library.swr_get_out_samples.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.swr_get_out_samples.restype = ctypes.c_int
        library.swr_convert.argtypes = [ctypes.c_void_p, planes, ctypes.c_int, planes, ctypes.c_int]
        library.swr_convert.restype = ctypes.c_int
        library.swr_free.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        library.swr_free.restype = None
        return library
    except (OSError, AttributeError):
        return None


def native_resampling_available():
    return _swresample() is not None


class PCM16Resampler:
    """One mono PCM16 stream, with bounded chunks and an explicit final flush."""
    def __init__(self, input_rate, output_rate=48000):
        self._closed = False
        self._context = ctypes.c_void_p()
        self._library = None
        if (type(input_rate) is not int or type(output_rate) is not int
                or not 8000 <= input_rate <= 96000 or not 8000 <= output_rate <= 96000):
            raise PCMConversionError('invalid_pcm_rate')
        self.input_rate, self.output_rate = input_rate, output_rate
        if input_rate == output_rate:
            return
        library = _swresample()
        if library is None:
            raise PCMConversionError('native_pcm_conversion_unavailable')
        self._library = library
        if hasattr(library, 'swr_alloc_set_opts2'):
            # AV_CHANNEL_ORDER_NATIVE=1, AV_CH_LAYOUT_MONO=4, AV_SAMPLE_FMT_S16=1.
            layout = _ChannelLayout(1, 1, _LayoutUnion(mask=4), None)
            result = library.swr_alloc_set_opts2(ctypes.byref(self._context),
                ctypes.byref(layout), 1, output_rate,
                ctypes.byref(layout), 1, input_rate, 0, None)
        else:
            self._context = ctypes.c_void_p(library.swr_alloc_set_opts(
                None, 4, 1, output_rate, 4, 1, input_rate, 0, None))
            result = 0 if self._context else -1
        if result < 0 or not self._context or library.swr_init(self._context) < 0:
            self.close()
            raise PCMConversionError('native_pcm_conversion_initialization_failed')

    def _convert(self, pcm):
        library = self._library
        frames = len(pcm) // 2
        capacity = library.swr_get_out_samples(self._context, frames)
        if capacity < 0 or capacity > 16 * 1024 * 1024:
            raise PCMConversionError('native_pcm_conversion_size_limit')
        capacity = max(capacity, 64)
        output = (ctypes.c_uint8 * (capacity * 2))()
        output_planes = (ctypes.POINTER(ctypes.c_uint8) * 1)(
            ctypes.cast(output, ctypes.POINTER(ctypes.c_uint8)))
        source = ctypes.create_string_buffer(pcm) if pcm else None
        input_planes = ((ctypes.POINTER(ctypes.c_uint8) * 1)(
            ctypes.cast(source, ctypes.POINTER(ctypes.c_uint8))) if source is not None else None)
        produced = library.swr_convert(self._context, output_planes, capacity, input_planes, frames)
        if produced < 0 or produced > capacity:
            raise PCMConversionError('native_pcm_conversion_failed')
        return ctypes.string_at(output, produced * 2)

    def feed(self, pcm):
        if self._closed:
            raise PCMConversionError('pcm_converter_closed')
        if not isinstance(pcm, bytes) or len(pcm) % 2:
            raise PCMConversionError('invalid_mono_pcm16')
        if not pcm:
            return b''
        if self.input_rate == self.output_rate:
            return pcm
        # Bound allocations even if a caller supplies a complete long stem.
        return b''.join(self._convert(pcm[index:index + 131072])
                        for index in range(0, len(pcm), 131072))

    def finish(self):
        if self._closed:
            return b''
        try:
            if self.input_rate == self.output_rate:
                return b''
            result = []
            while True:
                chunk = self._convert(b'')
                if not chunk:
                    return b''.join(result)
                result.append(chunk)
        finally:
            self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            if self._library is not None and self._context:
                self._library.swr_free(ctypes.byref(self._context))

    def __del__(self):
        self.close()


def scale_pcm16(pcm, gain):
    """Undo one constant peak gain with the established round/clipping rules."""
    if gain == 1.0:
        return pcm
    if _np is not None:
        values = _np.frombuffer(pcm, dtype='<i2').astype(_np.float64)
        return _np.clip(_np.rint(values / gain), -32768, 32767).astype('<i2').tobytes()
    samples = array.array('h', pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    for index, value in enumerate(samples):
        samples[index] = max(-32768, min(32767, round(value / gain)))
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples.tobytes()


def stereo_mix(items, start, stop):
    """Return exact PCM16 stereo samples in native endian for a sealed window."""
    if _np is not None:
        count = stop-start
        left, right = _np.zeros(count, dtype=_np.float64), _np.zeros(count, dtype=_np.float64)
        for timeline, left_gain, right_gain in items:
            values = _np.frombuffer(timeline, dtype=_np.int16, count=count, offset=start*2)
            left += values * left_gain
            right += values * right_gain
        result = _np.empty(count*2, dtype=_np.int16)
        result[::2] = _np.rint(left).astype(_np.int16)
        result[1::2] = _np.rint(right).astype(_np.int16)
        return array.array('h', result.tobytes())
    output = array.array('h')
    for frame in range(start, stop):
        left = right = 0.0
        for timeline, left_gain, right_gain in items:
            value = timeline[frame]
            left += value*left_gain
            right += value*right_gain
        output.extend((round(left), round(right)))
    return output
