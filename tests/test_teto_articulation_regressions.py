from __future__ import annotations

import array
import math
import sys
import time
import wave
from pathlib import Path

import pytest


WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer import TetoRenderer
from teto_renderer.prosody import RenderNote
from teto_renderer.voicebank import OtoEntry


def _renderer(monkeypatch, tmp_path: Path, *, velocity: int = 100) -> TetoRenderer:
    monkeypatch.setenv("PHONE_WORKER_TETO_VELOCITY", str(velocity))
    monkeypatch.setenv("PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR", str(tmp_path / "cache"))
    renderer = TetoRenderer()
    renderer._index_profile = "english-cvvc"
    return renderer


def _frames(duration_ms: float) -> int:
    return round(TetoRenderer.SAMPLE_RATE * duration_ms / 1000.0)


def _wav(path: Path, samples: array.array) -> Path:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(TetoRenderer.SAMPLE_RATE)
        audio.writeframes(samples.tobytes())
    return path


def _compose(renderer, notes, entries, samples, tmp_path):
    fragments = {
        index: (_wav(tmp_path / f"fragment-{index}.wav", pcm), False, False)
        for index, pcm in samples.items()
    }
    placements, end = renderer._english_timeline_placements(notes, entries)
    result = renderer._compose_english_cvvc(
        notes=notes, entries=entries, fragments=fragments,
        deadline=time.monotonic() + 10.0,
        placements=placements, planned_end_ms=end,
    )
    return result[0], placements


@pytest.mark.parametrize("velocity", [80, 100, 120])
def test_long_oto_trim_keeps_source_end_and_attack_coordinates(monkeypatch, tmp_path, velocity):
    renderer = _renderer(monkeypatch, tmp_path, velocity=velocity)
    note = RenderNote(("e N",), "C4", 80, 0, role="nasal", profile="english-cvvc")
    # The official English bank has this 250 ms preutterance and 300 ms fixed
    # region. A negative cutoff is a retained length measured from the offset.
    original = OtoEntry("e N", tmp_path / "source.wav", 1000.0, 300.0, -420.0, 250.0, 83.333)
    effective = renderer._english_render_entry(original, note)
    stretch = 2.0 ** (1.0 - velocity / 100.0)
    removed_ms = effective.offset_ms - original.offset_ms
    lead_ms = renderer._english_lead_ms(effective, note)

    assert removed_ms > 0.0
    assert lead_ms == pytest.approx(90.0)
    # The consonant attack stays at the same source position after trimming;
    # velocity stretches the retained lead rather than the discarded audio.
    assert removed_ms * stretch + lead_ms == pytest.approx(original.preutterance_ms * stretch)
    assert effective.offset_ms + effective.preutterance_ms == pytest.approx(
        original.offset_ms + original.preutterance_ms,
    )
    assert effective.offset_ms + effective.consonant_ms == pytest.approx(
        original.offset_ms + original.consonant_ms,
    )
    assert effective.offset_ms - effective.cutoff_ms == pytest.approx(
        original.offset_ms - original.cutoff_ms,
    )
    assert 0.0 <= effective.overlap_ms <= effective.preutterance_ms
    assert original.preutterance_ms == 250.0  # Shared indexed OTO stays immutable.


def test_trimmed_official_nasal_burst_survives_pcm_composition(monkeypatch, tmp_path):
    renderer = _renderer(monkeypatch, tmp_path)
    notes = [
        RenderNote(("e",), "C4", 120, 0, profile="english-cvvc"),
        RenderNote(("e N",), "C4", 66, 0, role="nasal", profile="english-cvvc"),
    ]
    original = OtoEntry("e N", tmp_path / "source.wav", 0.0, 300.0, -340.0, 250.0, 83.333)
    effective = renderer._english_render_entry(original, notes[1])
    entries = [OtoEntry("e", tmp_path / "e.wav", 0, 20, 0, 0, 0), effective]

    # This synthetic source has a quiet preceding vowel, followed by a strong
    # nasal cue at the ORIGINAL OTO preutterance. Simulating adjusted OFFSET
    # keeps the test sensitive to the actual audible cue, not just metadata.
    source = array.array("h", [500] * _frames(340))
    for index in range(_frames(250), _frames(300)):
        source[index] = 5000 if index % 2 else -5000
    trimmed = source[_frames(effective.offset_ms - original.offset_ms):]
    pcm, placements = _compose(
        renderer, notes, entries,
        {0: array.array("h", [500] * _frames(120)), 1: trimmed}, tmp_path,
    )
    # The new fragment owns the PCM by its anchor, so the 50 ms consonantal
    # cue should remain almost intact even after the compositor bounds its tail.
    anchor = _frames(placements[1]["anchor_ms"])
    burst = pcm[anchor:anchor + _frames(50)]
    rms = math.sqrt(sum(float(value) ** 2 for value in burst) / len(burst))
    assert len(burst) == _frames(50)
    assert rms > 4500.0
    assert max(abs(value) for value in burst) == 5000


def test_cluster_attack_is_not_replaced_before_its_preutterance(monkeypatch, tmp_path):
    renderer = _renderer(monkeypatch, tmp_path)
    notes = [
        RenderNote(("- br",), "C4", 42, 0, role="cluster", profile="english-cvvc"),
        RenderNote(("ra",), "C4", 120, 0, profile="english-cvvc"),
    ]
    entries = [
        OtoEntry("- br", tmp_path / "br.wav", 0, 60, 0, 45, 18),
        OtoEntry("ra", tmp_path / "ra.wav", 0, 80, 0, 70, 18),
    ]
    cluster = array.array("h", [300] * _frames(87))
    # Distinct high-frequency attack AFTER the helper's OTO preutterance.
    # A fixed 12 ms stagger lets the following CV erase this entire burst.
    for index in range(_frames(45), _frames(60)):
        cluster[index] = round(6000 * math.sin(2 * math.pi * 1800 * index / renderer.SAMPLE_RATE))
    pcm, placements = _compose(
        renderer, notes, entries,
        {0: cluster, 1: array.array("h", [300] * _frames(190))}, tmp_path,
    )
    first = _frames(placements[0]["anchor_ms"])
    attack = pcm[first:first + _frames(15)]
    rms = math.sqrt(sum(float(sample) ** 2 for sample in attack) / len(attack))
    assert rms > 4000


def test_resampler_extra_tail_does_not_fill_punctuation_pause(monkeypatch, tmp_path):
    renderer = _renderer(monkeypatch, tmp_path)
    notes = [
        RenderNote(("a",), "C4", 120, 180, phrase_end=".", profile="english-cvvc"),
        RenderNote(("i",), "C4", 100, 0, profile="english-cvvc"),
    ]
    entries = [
        OtoEntry("a", tmp_path / "a.wav", 0, 80, 0, 70, 18),
        OtoEntry("i", tmp_path / "i.wav", 0, 80, 0, 70, 18),
    ]
    pcm, placements = _compose(
        renderer, notes, entries,
        {0: array.array("h", [1200] * _frames(250)), 1: array.array("h", [1200] * _frames(230))},
        tmp_path,
    )
    end = _frames(placements[0]["anchor_ms"] + notes[0].duration_ms)
    following = _frames(placements[1]["start_ms"])
    assert following - end == _frames(180)
    assert pcm[end:following] == array.array("h", [0] * _frames(180))


@pytest.mark.parametrize("role", ["coda", "glide", "nasal", "transition", "cluster"])
def test_missing_auxiliary_creates_no_silent_gap(monkeypatch, tmp_path, role):
    renderer = _renderer(monkeypatch, tmp_path)
    notes = [
        RenderNote(("a",), "C4", 100, 0, profile="english-cvvc"),
        RenderNote(("absent",), "C4", 46, 0, role=role, profile="english-cvvc"),
        RenderNote(("i",), "C4", 100, 0, profile="english-cvvc"),
    ]
    entries = [
        OtoEntry("a", tmp_path / "a.wav", 0, 20, 0, 0, 0), None,
        OtoEntry("i", tmp_path / "i.wav", 0, 20, 0, 0, 0),
    ]
    pcm, placements = _compose(
        renderer, notes, entries,
        {0: array.array("h", [1200] * _frames(100)), 2: array.array("h", [1200] * _frames(100))},
        tmp_path,
    )
    join = _frames(100)
    assert placements[2]["start_ms"] == pytest.approx(100.0)
    assert len(pcm) == _frames(200)
    assert min(pcm[join - _frames(10):join + _frames(10)]) > 1000


@pytest.mark.parametrize("role", ["coda", "transition"])
def test_missing_auxiliary_keeps_punctuation_pause(monkeypatch, tmp_path, role):
    renderer = _renderer(monkeypatch, tmp_path)
    notes = [
        RenderNote(("a",), "C4", 100, 0, profile="english-cvvc"),
        RenderNote(("absent",), "C4", 46, 80, role=role,
                   phrase_end=".", profile="english-cvvc"),
        RenderNote(("i",), "C4", 100, 0, profile="english-cvvc"),
    ]
    entries = [
        OtoEntry("a", tmp_path / "a.wav", 0, 20, 0, 0, 0), None,
        OtoEntry("i", tmp_path / "i.wav", 0, 20, 0, 0, 0),
    ]
    pcm, placements = _compose(
        renderer, notes, entries,
        {0: array.array("h", [1200] * _frames(100)), 2: array.array("h", [1200] * _frames(100))},
        tmp_path,
    )
    assert placements[2]["start_ms"] == pytest.approx(180.0)
    assert pcm[_frames(100):_frames(180)] == array.array("h", [0] * _frames(80))
    assert min(pcm[_frames(180):_frames(190)]) > 1000
    assert len(pcm) == _frames(280)

