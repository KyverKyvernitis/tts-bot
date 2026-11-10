"""Differential tests against the existing scalar and NumPy CD PCM contracts."""
import array
import os
import random
from unittest.mock import patch

import pytest

from osaka_cd import pcm


def flags(native):
    return patch.dict(os.environ, {"TTS_BACKEND": "python", "TTS_RENDER_RUST": "0",
                                  "TTS_CD_PCM_RUST": "1" if native else "0"})


@pytest.fixture
def cd_native():
    from cogs.tts.fluxo_rust import _carregar_nucleo
    try:
        native = _carregar_nucleo()
        if getattr(native, "CD_PCM_PROTOCOL_VERSION", None) != 1:
            raise RuntimeError("native CD PCM protocol 1 required")
    except (ImportError, OSError, RuntimeError) as error:
        if os.environ.get("TTS_NATIVE_REQUIRED") == "1":
            raise
        pytest.skip(str(error))
    return native


@pytest.mark.parametrize("gain", [0.6, 0.8, 1.0, 1.2, 2.0])
def test_reverse_peak_gain_preserves_rounding_and_clipping(cd_native, gain):
    samples = array.array("h", [0, 1, 3, -1, -3, 32767, -32768, 12345, -23456])
    raw = pcm._little_endian(samples)
    with flags(False), patch.object(pcm, "_np", None):
        expected = pcm.scale_pcm16(raw, gain)
    with flags(True):
        assert pcm.scale_pcm16(raw, gain) == expected


def test_sealed_window_stereo_mix_matches_scalar_and_numpy(cd_native):
    generator = random.Random(74)
    voices = [(array.array("h", [generator.randrange(-32768, 32768) for _ in range(1024)]), l, r)
              for l, r in [(0.40, 0.14), (0.27, 0.32), (0.13, 0.44)]]
    references = [None] + ([pcm._np] if pcm._np is not None else [])
    for implementation in references:
        with flags(False), patch.object(pcm, "_np", implementation):
            expected = pcm.stereo_mix(voices, 17, 1001)
        with flags(True), patch.object(pcm, "_np", implementation):
            assert pcm.stereo_mix(voices, 17, 1001) == expected


def test_linear_crossfade_keeps_absolute_clock_and_trailing_samples(cd_native):
    generator = random.Random(92)
    old = array.array("h", [generator.randrange(-32768, 32768) for _ in range(500)])
    incoming = array.array("h", [generator.randrange(-32768, 32768) for _ in range(800)])
    with flags(False):
        expected = pcm.linear_overlap(old, incoming[:], relative=73, total_overlap=600)
    with flags(True):
        assert pcm.linear_overlap(old, incoming[:], relative=73, total_overlap=600) == expected
        first = pcm.linear_overlap(old[:200], incoming[:200], relative=73, total_overlap=600)
        second = pcm.linear_overlap(old[200:], incoming[200:], relative=273, total_overlap=600)
        assert first + second == expected


def test_scalar_overflow_and_numpy_wrap_are_preserved(cd_native):
    voices = [(array.array("h", [32767, -32768]), 2.0, 2.0)]
    with flags(False), patch.object(pcm, "_np", None), pytest.raises(OverflowError):
        pcm.stereo_mix(voices, 0, 2)
    with flags(True), patch.object(pcm, "_np", None), pytest.raises(OverflowError):
        pcm.stereo_mix(voices, 0, 2)
    assert array.array("h", cd_native.cd_stereo_mix([(pcm._little_endian(voices[0][0]), 2.0, 2.0)], 2, True)) == array.array("h", [-2, -2, 0, 0])


def test_first_nonzero_scans_stereo_frames(cd_native):
    for values in ([], [0, 0], [0, 0, 0, -3, 1, 2], [1, 0]):
        samples = array.array("h", values)
        with flags(False):
            expected = pcm.first_nonzero_stereo_frame(samples)
        with flags(True):
            assert pcm.first_nonzero_stereo_frame(samples) == expected
