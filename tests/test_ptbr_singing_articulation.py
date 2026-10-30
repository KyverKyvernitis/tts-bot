"""Audible CVVC marker regressions, without a proprietary voicebank or ESPER."""
from __future__ import annotations

import array
from dataclasses import replace
import math
from pathlib import Path
import sys
import time
import wave

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "deploy" / "termux" / "phone-worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.prosody import RenderNote
from teto_renderer.renderer import TetoRenderer
from teto_renderer.errors import TetoSynthesisError
from teto_renderer.voicebank import OtoEntry


RATE = TetoRenderer.SAMPLE_RATE
VOWEL, NASAL, GLIDE, SECOND_VOWEL = 220, 660, 1540, 440


def _note(role, duration, syllable=0):
    return RenderNote((f"{role}-{syllable}",), "C4", duration, 0,
                      role=role, syllable_index=syllable, word_index=syllable)


def _entry(note, lead=0):
    return OtoEntry(note.candidates[0], Path("synthetic.wav"), 0, lead + 10,
                    -1000, lead, min(18, lead))


def _score(events):
    return {"version": 1, "track_index": 0, "lyrics_source": "message",
            "duration_ms": max(start + duration for start, duration in events),
            "notes": [{"midi_note": 60, "start_ms": start,
                       "duration_ms": duration, "velocity": 100}
                      for start, duration in events]}


def _render(tmp_path, notes, entries, spans, score, frequencies, *, on_prefix=None):
    renderer = TetoRenderer()
    renderer._index_profile = "english-cvvc"
    placements, preroll = renderer._singing_placements(notes, entries, spans, score)
    notes = [replace(note, duration_ms=placement.get("duration_ms", note.duration_ms))
             for note, placement in zip(notes, placements)]
    fragments = {}
    for number, (note, entry, placement, frequency) in enumerate(
            zip(notes, entries, placements, frequencies)):
        if entry is None:
            continue
        lead_frames = round(RATE * placement["lead_ms"] / 1000)
        count = round(RATE * (note.duration_ms + placement["lead_ms"]) / 1000)
        # OTO leads contain the preceding recorded vowel. The cue identifying
        # a nasal/glide/coda only starts at its actual preutterance boundary.
        samples = array.array("h", (
            round(6000 * math.sin(2 * math.pi *
                                 (VOWEL if frame < lead_frames else frequency) * frame / RATE))
            for frame in range(count)))
        if sys.byteorder != "little":
            samples.byteswap()
        path = tmp_path / f"fragment-{number}.wav"
        with wave.open(str(path), "wb") as out:
            out.setparams((1, 2, RATE, 0, "NONE", ""))
            out.writeframes(samples.tobytes())
        fragments[number] = (path, False, False)
    callback = None
    if on_prefix is not None:
        callback = lambda samples, number, intervals: on_prefix(
            renderer, samples, number, intervals, placements, preroll)
    composed, missing, _, _, _, _ = renderer._compose_english_cvvc(
        notes=notes, entries=entries, fragments=fragments,
        deadline=time.monotonic() + 30, placements=placements,
        planned_end_ms=score["duration_ms"] + preroll, on_prefix=callback)
    return composed, preroll, missing


def _audible_centers(samples, frequency):
    """Find an actual marker's PCM, regardless of the chosen scheduling policy."""
    width, step = round(RATE * .008), round(RATE * .004)
    candidates = (VOWEL, NASAL, GLIDE, SECOND_VOWEL)
    windows = {
        hz: [(math.cos(2 * math.pi * hz * frame / RATE),
              math.sin(2 * math.pi * hz * frame / RATE)) for frame in range(width)]
        for hz in candidates}
    centers = []
    for start in range(0, len(samples) - width + 1, step):
        section = samples[start:start + width]
        if max((abs(value) for value in section), default=0) < 100:
            continue
        powers = {}
        for hz, basis in windows.items():
            real = sum(sample * point[0] for sample, point in zip(section, basis))
            imag = sum(sample * point[1] for sample, point in zip(section, basis))
            powers[hz] = real * real + imag * imag
        if max(powers, key=powers.get) == frequency:
            centers.append((start + width / 2) * 1000 / RATE)
    return centers


@pytest.mark.parametrize("roles", [("nasal", "glide"), ("nasal", "coda"), ("coda", "coda")])
@pytest.mark.parametrize("duration", [120, 400, 1400])
def test_ordered_tail_cues_remain_audible_inside_the_original_note(tmp_path, roles, duration):
    notes = [_note("nucleus", duration), _note(roles[0], 66), _note(roles[1], 54)]
    entries = [_entry(notes[0]), _entry(notes[1], 90), _entry(notes[2], 90)]
    spans = [(0, duration, [0])] * 3
    samples, preroll, _ = _render(tmp_path, notes, entries, spans,
                                  _score([(0, duration)]), [VOWEL, NASAL, GLIDE])
    vowel = _audible_centers(samples, VOWEL)
    first, second = _audible_centers(samples, NASAL), _audible_centers(samples, GLIDE)
    assert len(first) >= 2, "the first nasal/coda was erased by the following recorded vowel lead"
    assert len(second) >= 2, "the final glide/coda disappeared"
    assert vowel and min(vowel) < min(first) < min(second)
    assert max(first) < max(second)
    assert len(samples) <= round((duration + preroll) * RATE / 1000) + 1


def test_long_incoming_vowel_lead_does_not_erase_previous_tail(tmp_path):
    notes = [_note("nucleus", 400), _note("nasal", 66), _note("glide", 54),
             _note("nucleus", 400, syllable=1)]
    entries = [_entry(notes[0]), _entry(notes[1], 90), _entry(notes[2], 90),
               _entry(notes[3], 180)]
    spans = [(0, 400, [0])] * 3 + [(400, 800, [1])]
    samples, preroll, _ = _render(tmp_path, notes, entries, spans,
                                  _score([(0, 400), (400, 400)]),
                                  [VOWEL, NASAL, GLIDE, SECOND_VOWEL])
    first, second = _audible_centers(samples, NASAL), _audible_centers(samples, GLIDE)
    following = _audible_centers(samples, SECOND_VOWEL)
    assert len(first) >= 2 and len(second) >= 2
    assert min(first) < min(second) < min(following)
    assert max(first) < 400 + preroll and max(second) < 400 + preroll


def test_absent_auxiliary_does_not_reserve_a_silent_tail(tmp_path):
    notes = [_note("nucleus", 120), _note("nasal", 66), _note("glide", 54)]
    entries = [_entry(notes[0]), None, _entry(notes[2], 90)]
    spans = [(0, 120, [0])] * 3
    with_missing, _, missing = _render(tmp_path, notes, entries, spans,
                                      _score([(0, 120)]), [VOWEL, NASAL, GLIDE])
    without_missing, _, _ = _render(tmp_path, [notes[0], notes[2]],
                                   [entries[0], entries[2]], [spans[0], spans[2]],
                                   _score([(0, 120)]), [VOWEL, GLIDE])
    assert missing == [notes[1].candidates[0]]
    assert with_missing == without_missing


def test_tail_does_not_fill_a_musical_rest(tmp_path):
    notes = [_note("nucleus", 120), _note("nasal", 66), _note("glide", 54),
             _note("nucleus", 120, syllable=1)]
    entries = [_entry(notes[0]), _entry(notes[1], 90), _entry(notes[2], 90),
               _entry(notes[3], 40)]
    spans = [(0, 120, [0])] * 3 + [(400, 520, [1])]
    samples, preroll, _ = _render(tmp_path, notes, entries, spans,
                                  _score([(0, 120), (400, 120)]),
                                  [VOWEL, NASAL, GLIDE, SECOND_VOWEL])
    middle = samples[round((160 + preroll) * RATE / 1000):
                     round((320 + preroll) * RATE / 1000)]
    assert middle and not any(middle)
    assert _audible_centers(samples, NASAL) and _audible_centers(samples, GLIDE)


@pytest.mark.parametrize("duration", [40, 120, 400])
def test_onset_helpers_keep_their_order_before_a_fast_vowel(tmp_path, duration):
    notes = [_note("cluster", 46), _note("transition", 46),
             _note("nucleus", duration)]
    entries = [_entry(notes[0], 90), _entry(notes[1], 90), _entry(notes[2], 140)]
    spans = [(0, duration, [0])] * 3
    samples, preroll, _ = _render(tmp_path, notes, entries, spans,
                                  _score([(0, duration)]), [NASAL, GLIDE, VOWEL])
    first, second = _audible_centers(samples, NASAL), _audible_centers(samples, GLIDE)
    vowel = [center for center in _audible_centers(samples, VOWEL)
             if preroll + 4 <= center < preroll + duration - 4]
    assert first and second, "an incoming helper was replaced before its consonant cue"
    assert min(first) < min(second) and max(first) < max(second)
    assert vowel, "fast articulation swallowed the note's vowel"
    assert max(second) < min(vowel), "onset helpers ran beyond the vowel's musical anchor"
    assert 0 <= preroll <= 350
    assert len(samples) <= round((duration + preroll) * RATE / 1000) + 1


@pytest.mark.parametrize("duration", [120, 400])
def test_shared_vc_cv_boundary_preserves_previous_nasal_and_glide(tmp_path, duration):
    notes = [_note("nucleus", duration), _note("nasal", 66), _note("glide", 54),
             _note("vowel-exit", 58, syllable=1), _note("nucleus", 120, syllable=1)]
    entries = [_entry(notes[0]), _entry(notes[1], 90), _entry(notes[2], 90),
               _entry(notes[3], 120), _entry(notes[4], 180)]
    spans = [(0, duration, [0])] * 3 + [(duration, duration + 120, [1])] * 2
    samples, preroll, _ = _render(tmp_path, notes, entries, spans,
                                  _score([(0, duration), (duration, 120)]),
                                  [VOWEL, NASAL, GLIDE, SECOND_VOWEL, SECOND_VOWEL])
    first, second = _audible_centers(samples, NASAL), _audible_centers(samples, GLIDE)
    assert first and second, "the shared VC/CV transition erased the previous word's tail"
    assert max(first) < max(second) < duration + preroll
    assert len(samples) <= round((duration + 120 + preroll) * RATE / 1000) + 1


def test_infeasible_onset_list_is_rejected_instead_of_exceeding_preroll_limit():
    renderer = TetoRenderer()
    renderer._index_profile = "english-cvvc"
    notes = [_note("cluster", 30) for _ in range(128)] + [_note("nucleus", 40)]
    entries = [_entry(note, 120) for note in notes[:-1]] + [_entry(notes[-1], 180)]
    with pytest.raises(TetoSynthesisError, match="ataque consonantal"):
        renderer._singing_placements(notes, entries, [(0, 40, [0])] * len(notes),
                                     _score([(0, 40)]))


def test_articulation_does_not_require_whole_phrase_before_sealing_pcm(tmp_path):
    notes = [_note("nucleus", 400), _note("nasal", 66), _note("glide", 54)]
    entries = [_entry(notes[0]), _entry(notes[1], 90), _entry(notes[2], 90)]
    spans = [(0, 400, [0])] * 3
    for number in range(1, 5):
        notes.append(_note("nucleus", 400, syllable=number))
        entries.append(_entry(notes[-1], 40))
        spans.append((number * 400, (number + 1) * 400, [number]))
    music = _score([(number * 400, 400) for number in range(5)])
    emitted = []

    def seal(renderer, combined, number, intervals, placements, preroll):
        future = [placement["start_ms"] + placement.get("trim_lead_ms", 0.0)
                  for placement, entry in
                  zip(placements[number + 1:], entries[number + 1:]) if entry is not None]
        end = min(len(combined) - round(RATE * .004),
                  round(min(future, default=music["duration_ms"] + preroll) * RATE / 1000)
                  - round(RATE * .004))
        if end > RATE // 10:
            sealed = renderer._sealed_pcm_range(combined, 0, end, intervals,
                total_frames=round((music["duration_ms"] + preroll) * RATE / 1000),
                score=music, preroll=preroll, envelopes=[])
            emitted.append((number, sealed))

    samples, preroll, _ = _render(tmp_path, notes, entries, spans, music,
                                  [VOWEL, NASAL, GLIDE] + [SECOND_VOWEL] * 4,
                                  on_prefix=seal)
    assert emitted and emitted[0][0] < len(notes) - 1
    TetoRenderer._apply_midi_velocity(samples, music["notes"], preroll)
    for _, sealed in emitted:
        assert sealed == samples[:len(sealed)], "later articulation rewrote already sealed PCM"
