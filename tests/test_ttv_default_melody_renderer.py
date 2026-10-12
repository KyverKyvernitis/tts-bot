from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
import shutil

import pytest

from test_ttv_performance_renderer import renderer, score, segment, pcm
from teto_renderer.prosody import default_melody_score, performance_hash, validate_performance
from cogs.tts.performance import parse_performance_text


PRESET = {"version": 1, "preset": "ttv-demo-v1"}


def test_default_pattern_is_exactly_the_two_published_singing_samples():
    assert default_melody_score(4) == {
        "version": 1, "track_index": 0, "duration_ms": 4000,
        "lyrics_source": "message", "notes": [
            {"midi_note": 60, "start_ms": 200, "duration_ms": 850, "velocity": 100},
            {"midi_note": 64, "start_ms": 1050, "duration_ms": 850, "velocity": 100},
            {"midi_note": 67, "start_ms": 1900, "duration_ms": 850, "velocity": 100},
            {"midi_note": 64, "start_ms": 2750, "duration_ms": 1050, "velocity": 100},
        ],
    }


@pytest.mark.parametrize("bad", [
    {"version": True, "preset": "ttv-demo-v1"},
    {"version": 1, "preset": "unknown"},
    {"version": 1, "preset": "ttv-demo-v1", "notes": []},
])
def test_unknown_or_mixed_presets_fail_before_native_process(bad):
    with pytest.raises(ValueError):
        validate_performance("a", score=bad)


def test_default_request_is_immutable_and_audio_hash_binds_requested_template(renderer):
    requested = deepcopy(PRESET)
    original = deepcopy(requested)
    raw, metadata = renderer.synthesize_performance("a a a a", score=requested)
    assert requested == original
    assert metadata["ttv_performance_hash"] == performance_hash("a a a a", score=requested)
    assert metadata["effective_score"] == default_melody_score(4)
    assert metadata["singing_nuclei"] == 4
    samples, rate = pcm(raw)
    assert len(samples) / rate == pytest.approx((4000 + metadata["midi_preroll_ms"]) / 1000, abs=1/rate)


def test_default_holds_get_five_notes_without_attachment(renderer):
    text, segments = parse_performance_text("a ~ ~ ~ a", singing=True)
    raw, metadata = renderer.synthesize_performance(text, score=PRESET, segments=segments)
    assert metadata["effective_score"] == default_melody_score(5)
    assert metadata["effective_score"]["duration_ms"] == 4850
    assert metadata["singing_nuclei"] == 2 and metadata["melisma_notes"] == 3
    repeat, cached = renderer.synthesize_performance(text, score=PRESET, segments=segments)
    assert repeat == raw and cached["resampler_calls"] == 0


def test_longer_lyrics_repeat_the_original_four_note_pattern(renderer):
    _, metadata = renderer.synthesize_performance("a a a a a a", score=PRESET)
    assert [note["midi_note"] for note in metadata["effective_score"]["notes"]] == [60, 64, 67, 64, 60, 64]


def test_default_phrase_boundary_produces_a_real_rest(renderer):
    text, segments = parse_performance_text("a | a", singing=True)
    raw, metadata = renderer.synthesize_performance(text, score=PRESET, segments=segments)
    left, right = metadata["effective_score"]["notes"]
    assert right["start_ms"] - left["start_ms"] - left["duration_ms"] == 180
    samples, rate = pcm(raw)
    shift = metadata["midi_preroll_ms"]
    begin = round((left["start_ms"] + left["duration_ms"] + shift + 30) * rate / 1000)
    end = round((right["start_ms"] + shift - 50) * rate / 1000)
    assert end > begin and all(sample == 0 for sample in samples[begin:end])


def test_default_limits_are_actionable_before_rendering(renderer):
    with pytest.raises(ValueError, match="60 segundos"):
        renderer.synthesize_performance(" ".join(["a"] * 70), score=PRESET)
    with pytest.raises(ValueError, match="18 segundos"):
        renderer.synthesize_performance("a " + " ".join(["~"] * 22), score=PRESET)


@pytest.mark.parametrize("music", [PRESET, score()])
def test_portuguese_singing_selects_english_bank_when_speech_uses_standard(renderer, tmp_path, music):
    english = Path(renderer._environment["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"])
    standard = tmp_path / "standard"
    shutil.copytree(english, standard)
    renderer._environment = MappingProxyType({**renderer._environment,
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(standard),
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "standard"})
    before = renderer.synthesize("a")
    assert before["voicebank_profile"] == "standard"
    _, sung = renderer.synthesize_performance("a a a a" if "preset" in music else "a ~ ~", score=music)
    assert sung["voicebank_profile"] == "english-cvvc"
    assert renderer._english_index.root == english.resolve()
    after = renderer.synthesize("a")
    assert after["voicebank_profile"] == "standard" and after["audio"] == before["audio"]
