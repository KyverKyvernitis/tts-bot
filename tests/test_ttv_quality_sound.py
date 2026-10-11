"""PCM regressions for pauses in continuous English CVVC speech."""

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

from teto_renderer.renderer import TetoRenderer
from teto_renderer.phonemizer import Mora
from teto_renderer.prosody import RenderNote, align_pitch_to_timeline, build_notes
from teto_renderer.voicebank import OtoEntry
from teto_renderer.worldline import WorldlineRenderer
from teto_renderer import worldline_native


def _frames(milliseconds: float) -> int:
    return round(TetoRenderer.SAMPLE_RATE * milliseconds / 1000.0)


def _wav(path: Path, samples: array.array) -> Path:
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, TetoRenderer.SAMPLE_RATE, 0, "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())
    return path


def _rms(samples: array.array) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in samples) / len(samples))


def _compose(renderer, notes, entries, fragments):
    placements, end = renderer._english_timeline_placements(notes, entries)
    result = renderer._compose_english_cvvc(
        notes=notes, entries=entries, fragments=fragments,
        placements=placements, planned_end_ms=end,
        deadline=time.monotonic() + 5,
    )
    return result, placements


def test_punctuation_fades_the_whole_exposed_edge_and_preserves_pcm_elsewhere(monkeypatch, tmp_path):
    renderer = TetoRenderer(environment={"PHONE_WORKER_TETO_VELOCITY": "100"})
    renderer._index_profile = "english-cvvc"
    notes = [
        RenderNote(("a",), "C4", 100, 180, phrase_end=".", profile="english-cvvc"),
        RenderNote(("i",), "C4", 100, 180, phrase_end=".", profile="english-cvvc"),
    ]
    entries = [
        OtoEntry("a", tmp_path / "a.wav", 0, 10, 0, 0, 0),
        OtoEntry("i", tmp_path / "i.wav", 0, 10, 0, 0, 0),
    ]
    fragments = {
        index: (_wav(tmp_path / f"fragment-{index}.wav", array.array("h", [7000] * _frames(100))), False, False)
        for index in range(2)
    }
    original_files = {index: fragment[0].read_bytes() for index, fragment in fragments.items()}
    original_fader = renderer._fade_timeline_gaps
    monkeypatch.setattr(renderer, "_fade_timeline_gaps", lambda samples, intervals: 0)
    before, placements = _compose(renderer, notes, entries, fragments)
    monkeypatch.setattr(renderer, "_fade_timeline_gaps", original_fader)
    after, after_placements = _compose(renderer, notes, entries, fragments)

    left_end = _frames(placements[0]["anchor_ms"] + notes[0].duration_ms)
    right_start = _frames(placements[1]["start_ms"])
    edge = _frames(4)
    assert after_placements == placements
    assert len(after[0]) == len(before[0]) == _frames(380)
    assert after[-1]["silence_gap_fades"] == 1
    assert after[0][left_end:right_start] == array.array("h", [0] * _frames(180))
    assert after[0][left_end - 1] == after[0][right_start] == 0
    assert before[0][left_end - 1] == before[0][right_start] == 7000

    # Several samples approach silence smoothly, rather than just replacing
    # the one exposed sample. The inner half retains more energy than the edge.
    for boundary, step in ((left_end - 1, -1), (right_start, 1)):
        edge_pcm = array.array("h", [after[0][boundary + step * index] for index in range(edge)])
        assert _rms(edge_pcm[:edge // 2]) < _rms(edge_pcm[edge // 2:]) * 0.5
        assert max(abs(edge_pcm[index + 1] - edge_pcm[index]) for index in range(edge - 1)) < 100
        assert edge_pcm[-1] == 7000
    assert after[0][:left_end - edge] == before[0][:left_end - edge]
    assert after[0][right_start + edge:] == before[0][right_start + edge:]
    # Mixing edits neither the resampled fragment files nor their cache payloads.
    assert {index: fragment[0].read_bytes() for index, fragment in fragments.items()} == original_files


@pytest.mark.parametrize("intervals", [
    [(0, 1000), (900, 2000)],  # Typical CVVC overlap.
    [(0, 1000), (1000, 2000)],  # Exact adjacent fragments.
    [(0, 1100), (400, 1800), (1700, 2000)],  # Auxiliary aliases bridge regions.
])
def test_connected_articulation_and_its_zero_crossings_remain_untouched(intervals):
    samples = array.array("h", [round(6000 * math.sin(index * 0.1)) for index in range(2000)])
    original = array.array("h", samples)
    assert TetoRenderer._fade_timeline_gaps(samples, intervals) == 0
    assert samples == original


def test_subframe_rounding_gap_does_not_become_a_new_envelope_attack():
    samples = array.array("h", [4000] * 1000 + [0] * 30 + [4000] * 1000)
    original = array.array("h", samples)
    assert TetoRenderer._fade_timeline_gaps(samples, [(0, 1000), (1030, 2030)]) == 0
    assert samples == original


def test_missing_auxiliary_pause_still_has_smooth_edges_without_serializing_its_duration(tmp_path):
    renderer = TetoRenderer(environment={"PHONE_WORKER_TETO_VELOCITY": "100"})
    renderer._index_profile = "english-cvvc"
    notes = [
        RenderNote(("a",), "C4", 100, 0, profile="english-cvvc"),
        RenderNote(("absent",), "C4", 46, 80, role="coda", phrase_end=".", profile="english-cvvc"),
        RenderNote(("i",), "C4", 100, 0, profile="english-cvvc"),
    ]
    entries = [
        OtoEntry("a", tmp_path / "a.wav", 0, 10, 0, 0, 0), None,
        OtoEntry("i", tmp_path / "i.wav", 0, 10, 0, 0, 0),
    ]
    fragments = {
        index: (_wav(tmp_path / f"fragment-{index}.wav", array.array("h", [4000] * _frames(100))), False, False)
        for index in (0, 2)
    }
    result, placements = _compose(renderer, notes, entries, fragments)
    assert placements[2]["start_ms"] == pytest.approx(180)
    assert len(result[0]) == _frames(280)
    assert result[0][_frames(100) - 1] == result[0][_frames(180)] == 0
    assert result[-1]["silence_gap_fades"] == 1
    assert result[1] == ["absent"]


def test_short_regions_between_pauses_keep_their_center_and_do_not_change_length():
    first = _frames(30)
    gap = _frames(80)
    short = 12
    samples = array.array("h", [5000] * first + [0] * gap + [5000] * short + [0] * gap + [5000] * first)
    middle = first + gap
    end_middle = middle + short
    intervals = [(0, first), (middle, end_middle), (end_middle + gap, len(samples))]
    length = len(samples)
    assert TetoRenderer._fade_timeline_gaps(samples, intervals) == 2
    assert len(samples) == length
    assert samples[middle] == samples[end_middle - 1] == 0
    assert samples[middle + short // 2 - 1] == samples[middle + short // 2] == 5000


@pytest.mark.parametrize("previous_duration,cv_lead", [(128, 62), (70, 52), (100, 110)])
def test_vowel_exit_overlaps_the_cv_attack_without_adding_a_second_consonant_beat(tmp_path, previous_duration, cv_lead):
    renderer = TetoRenderer(environment={"PHONE_WORKER_TETO_VELOCITY": "100"})
    renderer._index_profile = "english-cvvc"
    nuclei = [
        RenderNote(("te",), "C4", previous_duration, 0, profile="english-cvvc"),
        RenderNote(("tu",), "C4", 120, 0, profile="english-cvvc"),
    ]
    exit_note = RenderNote(("e t",), "C4", 44, 0, role="vowel-exit", profile="english-cvvc")
    nucleus_entries = [
        OtoEntry("te", tmp_path / "te.wav", 0, 30, 0, 20, 10),
        OtoEntry("tu", tmp_path / "tu.wav", 0, cv_lead + 20, 0, cv_lead, min(30, cv_lead / 2)),
    ]
    exit_entry = OtoEntry("e t", tmp_path / "et.wav", 0, 150, 0, 120, 48)
    notes = [nuclei[0], exit_note, nuclei[1]]
    entries = [nucleus_entries[0], exit_entry, nucleus_entries[1]]
    before, before_end = renderer._english_timeline_placements(nuclei, nucleus_entries)
    after, after_end = renderer._english_timeline_placements(notes, entries)
    assert after_end == pytest.approx(before_end)
    assert [after[0]["anchor_ms"], after[2]["anchor_ms"]] == pytest.approx([item["anchor_ms"] for item in before])
    assert after[1]["anchor_ms"] == pytest.approx(after[2]["start_ms"])
    assert 0 <= after[1]["trim_lead_ms"] <= after[1]["lead_ms"]
    audible_start = after[1]["start_ms"] + after[1]["trim_lead_ms"]
    assert 0 <= audible_start <= after[1]["anchor_ms"]

    # The marker has a distinct high-frequency consonant cue after a long
    # recorded vowel. Its leading vowel may be cropped, but never that cue.
    exit_pcm = array.array("h", [300] * _frames(164))
    marker = _frames(120)
    for index in range(marker, marker + _frames(30)):
        exit_pcm[index] = round(9000 * math.sin(2 * math.pi * 1800 * (index - marker) / renderer.SAMPLE_RATE))
    fragments = {
        0: (_wav(tmp_path / "first.wav", array.array("h", [300] * _frames(previous_duration + 20))), False, False),
        1: (_wav(tmp_path / "exit.wav", exit_pcm), False, False),
        2: (_wav(tmp_path / "last.wav", array.array("h", [300] * _frames(120 + cv_lead))), False, False),
    }
    result, placements = _compose(renderer, notes, entries, fragments)
    attack_start = _frames(placements[1]["anchor_ms"])
    attack = result[0][attack_start:attack_start + _frames(20)]
    assert _rms(attack) > 800
    # The fixed cue and incoming consonant share the same window. The final
    # output ends at the original incoming nucleus, with no serialized helper.
    assert len(result[0]) == _frames(after[2]["anchor_ms"] + notes[2].duration_ms)


def test_new_vowel_exit_is_transparent_to_nucleus_rhythm_and_shared_f0(tmp_path):
    renderer = TetoRenderer(environment={"PHONE_WORKER_TETO_VELOCITY": "100"})
    renderer._index_profile = "english-cvvc"
    nuclei = [
        Mora(("te",), 120, profile="english-cvvc", word_moras=2, word_syllables=2),
        Mora(("tu",), 130, profile="english-cvvc", word_moras=2, word_syllables=2, stressed=True, word_end=True, phrase_end="."),
    ]
    auxiliary = Mora(("e t",), 58, role="vowel-exit", coverage="cvvc-transition", profile="english-cvvc", deaccented=True)
    before = build_notes(nuclei)
    after = build_notes([nuclei[0], auxiliary, nuclei[1]])
    entries = [OtoEntry("te", tmp_path / "te.wav", 0, 30, 0, 20, 10), OtoEntry("tu", tmp_path / "tu.wav", 0, 82, 0, 62, 26)]
    exit_entry = OtoEntry("e t", tmp_path / "et.wav", 0, 150, 0, 120, 48)
    before_places, before_end = renderer._english_timeline_placements(before, entries)
    after_places, after_end = renderer._english_timeline_placements(after, [entries[0], exit_entry, entries[1]])
    assert before_end == pytest.approx(after_end)
    before_aligned = align_pitch_to_timeline(before, before_places)
    after_aligned = align_pitch_to_timeline(after, after_places)
    assert before_aligned == [after_aligned[0], after_aligned[2]]


def test_worldline_vowel_exit_skip_keeps_marker_anchor_and_satisfies_native_model_budget(tmp_path):
    renderer = WorldlineRenderer(environment={"PHONE_WORKER_TETO_VELOCITY": "100"})
    renderer._index_profile = "english-cvvc"
    notes = [
        RenderNote(("te",), "C4", 128, 0, profile="english-cvvc"),
        RenderNote(("e t",), "C4", 44, 0, role="vowel-exit", profile="english-cvvc"),
        RenderNote(("tu",), "C4", 120, 0, profile="english-cvvc"),
    ]
    entries = [
        OtoEntry("te", tmp_path / "te.wav", 0, 30, 0, 20, 10),
        OtoEntry("e t", tmp_path / "et.wav", 0, 150, 0, 120, 48),
        OtoEntry("tu", tmp_path / "tu.wav", 0, 82, 0, 62, 26),
    ]
    sources = {}
    for entry in entries:
        _wav(entry.wav_path, array.array("h", [round(6000 * math.sin(index * 0.1)) for index in range(_frames(500))]))
        sources[entry.wav_path] = entry.wav_path.name
    places, _ = renderer._placements(notes, entries)
    assert places[1]["skip_ms"] > 0
    assert places[1]["anchor_ms"] == places[2]["start_ms"]
    assert places[1]["skip_ms"] <= 120
    job = renderer._job(notes, entries, sources, 5)
    exit_request = next(item for item in job["requests"] if item["sample_file"] == "et.wav")
    assert exit_request["required_length_ms"] == exit_request["skip_ms"] + exit_request["length_ms"]
    assert exit_request["position_ms"] >= 0
    assert exit_request["length_ms"] >= renderer.FRAME_MS
    # Exercise the actual native wrapper's frame/OTO validation, including its
    # skip + frame mapping bound rather than just comparing Python job fields.
    checked = worldline_native._request(exit_request, tmp_path, {}, 5)
    assert checked["mapping_frames"] >= round(checked["skip_ms"] / 10) + checked["p4"] - checked["p0"]
