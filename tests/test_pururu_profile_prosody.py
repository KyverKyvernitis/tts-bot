"""Pururu expression follows Portuguese syllables without changing CVVC layout."""
from __future__ import annotations

from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import sys

import pytest

WORKER = Path(__file__).resolve().parents[1] / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.phonemizer import Mora, phonemize
from teto_renderer.prosody import align_pitch_to_timeline, build_notes

PROFILE = 'pururu-ptbr-v1'


def legacy_contract(profile):
    return [
        Mora(('ka',), duration_ms=135, word_index=0, mora_index=0, word_moras=3,
             word_syllables=2, stressed=True, source_phonemes=('k', 'a'), profile=profile, syllable_index=0),
        Mora(('az',), duration_ms=58, role='vowel-exit', word_index=0, mora_index=1,
             word_moras=3, word_syllables=2, source_phonemes=('a', 'z'), profile=profile, syllable_index=1),
        Mora(('za',), duration_ms=135, word_index=0, mora_index=2, word_moras=3,
             word_syllables=2, word_end=True, source_phonemes=('z', 'a'), profile=profile, syllable_index=1),
        Mora(('-a',), duration_ms=130, word_index=1, word_moras=2, word_syllables=1,
             stressed=True, source_phonemes=('a',), profile=profile),
        Mora(('as',), duration_ms=58, role='coda', word_index=1, mora_index=1,
             word_moras=2, word_syllables=1, word_end=True, phrase_end='?', pause_after_ms=180,
             source_phonemes=('s',), profile=profile),
    ]


@pytest.mark.parametrize(('layout', 'expected'), [
    ('standard', '93aa5d45ff2337fa32b58cd4f83d195990adbfa624ed01775f0a85cc6b61b960'),
    ('english-cvvc', '6a575104ec30bdbda8829dec5c52823a351cd156bb4d377b03cb3b6ae8bbf834'),
])
def test_existing_speech_profiles_keep_the_complete_pre_profile_note_contract(layout, expected):
    notes = build_notes(legacy_contract(layout), speech_rate=.85, pitch_offset_semitones=1.5, tempo=125)
    payload = json.dumps([asdict(note) for note in notes], sort_keys=True).encode()
    assert sha256(payload).hexdigest() == expected
    assert build_notes(legacy_contract(layout), speech_rate=.85, pitch_offset_semitones=1.5,
                       tempo=125, speech_profile='') == notes


@pytest.mark.parametrize('moras', [[], [Mora(('a',))]])
def test_unknown_speech_profile_is_rejected_even_for_empty_input(moras):
    with pytest.raises(ValueError, match='perfil de prosódia'):
        build_notes(moras, speech_profile='unknown-profile')


def native(text):
    return phonemize(text, inventory_profile='pururu-purpura', voicebank_profile='english-cvvc')


def nuclei(notes):
    return [note for note in notes if note.role == 'nucleus']


def targets(note):
    return note.pitch_start_cents, note.pitch_peak_cents, note.pitch_end_cents, note.gain


def test_portuguese_statement_and_question_have_distinct_expression_with_the_same_native_layout():
    moras = native('Olá. Como você está?')
    before = list(moras)
    old = build_notes(moras)
    notes = build_notes(moras, speech_profile=PROFILE)
    statement = next(note for note in notes if note.contour == 'statement-fall')
    question = next(note for note in notes if note.contour == 'question-rise')
    old_question = next(note for note in old if note.contour == 'question-rise')
    assert statement.pitch_end_cents == -42
    assert question.pitch_end_cents == 64
    assert question.pitch_peak_cents > old_question.pitch_peak_cents
    assert question.pitch_peak_cents >= 68
    assert all(note.profile == 'english-cvvc' for note in notes)
    assert [note.duration_ms for note in notes] == [note.duration_ms for note in old]
    assert [note.pause_after_ms for note in notes] == [note.pause_after_ms for note in old]
    assert [note.candidates for note in notes] == [note.candidates for note in old]
    assert moras == before, 'the voice profile must not rewrite source phonemes or OTO alias choices'
    assert max(abs(value) for note in notes for value in targets(note)[:3]) <= 84


def test_consonant_inventory_density_does_not_move_or_emphasize_lexical_pitch_targets():
    raw = [
        Mora(('ka',), stressed=True, source_phonemes=('k','a'), profile='english-cvvc',
             coverage='cvvc-direct', word_index=0, word_syllables=2),
        Mora(('za',), source_phonemes=('z','a'), profile='english-cvvc',
             coverage='cvvc-direct', word_index=0, syllable_index=1, word_syllables=2, word_end=True),
        Mora(('-a',), source_phonemes=('a',), profile='english-cvvc', coverage='cvvc-direct',
             word_index=1, word_syllables=1, stressed=True, word_end=True, phrase_end='?'),
    ]
    extended = [raw[0]]
    for alias, role in [('aj','glide'), ('j s','coda'), ('s z','cluster'), ('az','vowel-exit')]:
        extended.append(Mora((alias,), role=role, profile='english-cvvc', coverage='cvvc-transition',
                             word_index=0, word_syllables=2, duration_ms=58))
    extended.extend([replace(raw[1], word_moras=6), raw[2]])
    plain = build_notes(raw, speech_profile=PROFILE)
    rich = build_notes(extended, speech_profile=PROFILE)
    assert [targets(note) for note in nuclei(rich)] == [targets(note) for note in nuclei(plain)]
    auxiliaries = [note for note in rich if note.role != 'nucleus']
    boundary = plain[0].pitch_end_cents
    assert auxiliaries
    assert all((note.pitch_start_cents, note.pitch_peak_cents, note.pitch_end_cents)
               == (boundary, boundary, boundary) for note in auxiliaries)
    assert all(left.pitch_end_cents == right.pitch_start_cents for left, right in zip(rich, rich[1:]))


def test_terminal_consonant_keeps_portuguese_phrase_fall_on_the_previous_vowel():
    moras = native('Bem, paz.')
    notes = build_notes(moras, speech_profile=PROFILE)
    terminal = next(note for note in reversed(notes) if note.role == 'nucleus')
    assert terminal.contour == 'statement-fall' and terminal.pitch_end_cents == -42
    assert notes[-1].role != 'nucleus'
    assert notes[-1].pitch_start_cents == notes[-1].pitch_end_cents == terminal.pitch_end_cents


def test_punctuation_resets_profile_contours_without_joining_two_phrases():
    one = nuclei(build_notes(native('Casa?'), speech_profile=PROFILE))
    both = nuclei(build_notes(native('Casa. Casa?'), speech_profile=PROFILE))
    assert [targets(note) for note in both[-len(one):]] == [targets(note) for note in one]
    assert both[1].contour == 'statement-fall'
    assert both[-1].contour == 'question-rise'


@pytest.mark.parametrize('pitch', [-3.5, -0.5, 0.5, 2.5])
def test_personal_half_semitone_offsets_remain_separate_from_pururu_expression(pitch):
    moras = native('Olá. Como você está?')
    neutral = build_notes(moras, speech_profile=PROFILE)
    shifted = build_notes(moras, speech_profile=PROFILE, pitch_offset_semitones=pitch)
    coarse = int(pitch)
    residual = round((pitch-coarse)*100)
    from teto_renderer.prosody import _pitch_midi
    assert all(_pitch_midi(after.pitch)-_pitch_midi(before.pitch) == coarse
               for before, after in zip(neutral, shifted))
    for before, after in zip(neutral, shifted):
        assert after.pitch_start_cents-before.pitch_start_cents == residual
        assert after.pitch_peak_cents-before.pitch_peak_cents == residual
        assert after.pitch_end_cents-before.pitch_end_cents == residual
        assert after.duration_ms == before.duration_ms


def test_personal_rate_changes_duration_without_flattening_the_new_phrase_contour():
    moras = native('Eu canto com você de coração.')
    normal = build_notes(moras, speech_profile=PROFILE)
    slow = build_notes(moras, speech_profile=PROFILE, speech_rate=.5)
    fast = build_notes(moras, speech_profile=PROFILE, speech_rate=1.5)
    assert sum(note.duration_ms for note in slow) > sum(note.duration_ms for note in normal)
    assert sum(note.duration_ms for note in normal) > sum(note.duration_ms for note in fast)
    assert [targets(note) for note in normal] == [targets(note) for note in slow]
    assert [targets(note) for note in normal] == [targets(note) for note in fast]


def test_cvvc_absolute_timeline_still_aligns_shared_pitch_in_overlapping_fragments():
    notes = build_notes(native('casa?'), speech_profile=PROFILE)
    assert all(note.profile == 'english-cvvc' for note in notes)
    placements = [dict(anchor_ms=number*150, start_ms=number*150-60, lead_ms=60)
                  for number in range(len(notes))]
    aligned = align_pitch_to_timeline(notes, placements, tempo=125)
    assert any(after.pitchbend != before.pitchbend for before, after in zip(notes, aligned))
    assert [note.duration_ms for note in aligned] == [note.duration_ms for note in notes]
    assert [note.candidates for note in aligned] == [note.candidates for note in notes]
    assert [note.profile for note in aligned] == ['english-cvvc']*len(notes)
