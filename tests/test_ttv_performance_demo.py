from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import wave

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ttv_performance_demo_tool",
    ROOT / "deploy/ttv-performance/termux/render-demo.py")
demo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = demo
spec.loader.exec_module(demo)


def test_demo_is_a_real_midi_with_legato_and_a_musical_rest():
    score = demo.score_helper().parse_midi(demo.demo_midi(), lyrics="a ~ ~ ~ a")
    assert score["lyrics_source"] == "message"
    assert score["duration_ms"] == 5500
    assert [(note["midi_note"], note["start_ms"], note["duration_ms"])
            for note in score["notes"]] == [
        (60, 0, 1000), (64, 1000, 1000), (67, 2000, 1000),
        (72, 3000, 1000), (67, 4500, 1000),
    ]
    assert all("lyric" not in note for note in score["notes"])


def make_wav(seconds, *, silent=False):
    output = io.BytesIO()
    with wave.open(output, "wb") as source:
        source.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        source.writeframes((b"\0\0" if silent else b"\x01\0") * round(seconds * 44100))
    return output.getvalue()


def test_phone_demo_accepts_musical_audio_longer_than_old_speech_limit():
    evidence = demo.wav_evidence(make_wav(60.35))
    assert evidence["wav_verified"]
    assert evidence["duration_seconds"] == pytest.approx(60.35, abs=1 / 44100)


@pytest.mark.parametrize("raw,match", [
    (b"", "vazio"), (make_wav(1, silent=True), "silencioso"),
    (make_wav(61.1), "até 61"),
])
def test_phone_demo_rejects_invalid_or_unbounded_audio(raw, match):
    with pytest.raises(ValueError, match=match):
        demo.wav_evidence(raw)
