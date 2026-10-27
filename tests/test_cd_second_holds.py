"""Timed user holds retain the CD's pitches, rests and physical MIDI contract."""
from __future__ import annotations

import ast
import copy
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from osaka_cd.lyrics import (HOLD_MILLISECONDS, POLICY, _phonemes,
                             automatic_render_plan, prepare_message_arrangement)


def segment(text, **effects):
    return {'text': text, 'fade_in': False, 'fade_out': False,
            'hold': False, 'pause_after_ms': 0, **effects}


def template(duration=250, *, gap=0, accompaniment=True):
    notes = [{'midi_note': 60 + i, 'start_ms': i * (duration + gap),
              'duration_ms': duration, 'velocity': 80 + i}
             for i in range(4)]
    parts = [{'id': 'lead', 'role': 'lead', 'lyrics_source': 'message', 'notes': notes}]
    if accompaniment:
        parts.append({'id': 'harmony', 'role': 'harmony', 'lyrics_source': 'vowel',
                      'notes': [dict(note, midi_note=note['midi_note'] + 7) for note in notes]})
    return {'score_schema': 2, 'duration_ms': notes[-1]['start_ms'] + duration,
            'parts': parts}


def hold_notes(prepared, word=0, identity='lead'):
    groups = prepared['lyrics_adaptation']['note_groups'][identity]
    start = sum(group['core'] + group['hold'] for group in groups[:word])
    group = groups[word]
    notes = next(part['notes'] for part in prepared['parts'] if part['id'] == identity)
    return notes[start + group['core']:start + group['core'] + group['hold']]


def original_aligner():
    """Use the real physical aligner without loading optional renderer engines."""
    path = ROOT / 'deploy/termux/phone-worker/teto_renderer/prosody.py'
    tree = ast.parse(path.read_text())
    names = {'is_default_score', 'validate_performance', 'align_singing_syllables'}
    namespace = {'math': math, 'Mora': object, 'DEFAULT_MELODY_PRESET': 'ttv-demo-v1'}
    exec(compile(ast.Module(body=[node for node in tree.body
                                 if isinstance(node, ast.FunctionDef) and node.name in names],
                            type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class SecondHoldTests(unittest.TestCase):
    def test_one_second_is_independent_of_note_length(self):
        for duration in (40, 50, 169, 231, 333, 1000, 1010, 2500):
            with self.subTest(duration=duration):
                value = template(duration)
                before = copy.deepcopy(value)
                prepared = prepare_message_arrangement(value, 'a', [segment('a', hold=True)])
                voiced = sum(note['duration_ms'] for note in hold_notes(prepared))
                self.assertLessEqual(abs(voiced - HOLD_MILLISECONDS), 39)
                self.assertEqual(value, before)
                self.assertEqual(prepared['lyrics_adaptation']['tempo_stretch'], 1)

    def test_repeated_holds_accumulate_seconds_instead_of_notes(self):
        effects = [segment('a', hold=True), segment('~', hold=True), segment('~', hold=True)]
        prepared = prepare_message_arrangement(template(169), 'a ~ ~', effects)
        self.assertEqual(prepared['lyrics_adaptation']['requested_hold_ms'], 3000)
        self.assertLessEqual(abs(sum(n['duration_ms'] for n in hold_notes(prepared)) - 3000), 39)
        self.assertGreater(len(hold_notes(prepared)), 3)
        self.assertGreater(prepared['lyrics_adaptation']['motif_repeats'], 1)

    def test_hold_edge_splits_a_long_note_and_next_word_uses_its_remainder(self):
        prepared = prepare_message_arrangement(template(2500), 'a i',
                                                [segment('a', hold=True), segment('i')])
        lead = prepared['parts'][0]['notes']
        self.assertEqual([(n['midi_note'], n['start_ms'], n['duration_ms']) for n in lead],
                         [(60, 0, 2500), (61, 2500, 1000), (61, 3500, 1500)])
        self.assertEqual(prepared['duration_ms'], 5000)
        # Harmony keeps its original note starts, duration and pitch.
        self.assertEqual(prepared['parts'][1]['notes'], template(2500)['parts'][1]['notes'][:2])

    def test_rests_stay_silent_and_do_not_count_as_held_vowel_time(self):
        prepared = prepare_message_arrangement(template(250, gap=500), 'a',
                                                [segment('a', hold=True)])
        held = hold_notes(prepared)
        self.assertEqual(sum(n['duration_ms'] for n in held), 1000)
        lead = prepared['parts'][0]['notes']
        gaps = [right['start_ms'] - left['start_ms'] - left['duration_ms']
                for left, right in zip(lead, lead[1:])]
        self.assertEqual(gaps, [500, 500, 500, 0])
        plan = automatic_render_plan(prepared, 'a', [segment('a', hold=True)])
        words = [part['adaptive_text'] for chunk in plan['chunks'] for part in chunk['parts']
                 if part['role'] == 'lead']
        self.assertIn('a', ' '.join(words))
        # A rest requires a fresh vowel attack, while the seamless motif wrap
        # may continue its vowel with the worker's physical one-note marker.
        self.assertEqual(' '.join(words).split().count('a'), 4)

    def test_note_pitch_velocity_and_motif_timing_are_preserved(self):
        value = template(231, gap=17)
        prepared = prepare_message_arrangement(value, 'a i',
                                                [segment('a', hold=True), segment('i', hold=True)])
        period = value['duration_ms']
        for part in prepared['parts']:
            source = next(p for p in value['parts'] if p['id'] == part['id'])
            for note in part['notes']:
                local = note['start_ms'] % period
                original = next(n for n in source['notes']
                                if n['start_ms'] <= local < n['start_ms'] + n['duration_ms'])
                self.assertEqual(note['midi_note'], original['midi_note'])
                self.assertEqual(note['velocity'], original['velocity'])
                self.assertLessEqual(local + note['duration_ms'],
                                     original['start_ms'] + original['duration_ms'])

    def test_dense_long_holds_obey_the_real_worker_note_and_vowel_limits(self):
        effects = [segment('a', hold=True)] + [segment('~', hold=True) for _ in range(19)]
        text = ' '.join(effect['text'] for effect in effects)
        prepared = prepare_message_arrangement(template(40), text, effects)
        plan = automatic_render_plan(prepared, text, effects,
                                     first_request_ms=3000, request_ms=8000)
        worker = original_aligner()
        total = 0
        for chunk in plan['chunks']:
            for request in chunk['parts']:
                score = request['score']
                self.assertLessEqual(len(score['notes']), 128)
                self.assertTrue(all(note['duration_ms'] >= 40 for note in score['notes']))
                if request['role'] != 'lead':
                    continue
                total += sum(note['duration_ms'] for note in score['notes'])
                worker['validate_performance'](request['adaptive_text'], score=score,
                                                segments=request['adaptive_segments'])
                nuclei, slots = [], []
                for token in request['adaptive_text'].split():
                    if token == '~':
                        slots.append(None)
                    else:
                        found = [m for m in _phonemes(token, 'pt-BR') if m.role == 'nucleus']
                        slots.extend(range(len(nuclei), len(nuclei) + len(found)))
                        nuclei.extend(found)
                groups = worker['align_singing_syllables'](nuclei, score, slots=slots)
                self.assertEqual(sum(map(len, groups)), len(score['notes']))
        self.assertEqual(total, 20040)

    def test_fixed_v2_snapshots_keep_their_existing_one_note_physical_mapping(self):
        value = template(250, accompaniment=False)
        value['parts'][0]['notes'] = value['parts'][0]['notes'][:2]
        value['duration_ms'] = 500
        value['message_lyrics_mode'] = 'automatic-lyrics-v2'
        effects = [segment('a', hold=True)]
        plan = automatic_render_plan(value, 'a', effects)
        lead = plan['chunks'][0]['parts'][0]
        self.assertEqual(lead['adaptive_text'], 'a ~')
        self.assertEqual(lead['score']['duration_ms'], 500)
        self.assertNotEqual(POLICY, value['message_lyrics_mode'])

    def test_policy_metadata_binds_each_prepared_word_to_its_physical_notes(self):
        effects = [segment('a', hold=True), segment('i')]
        prepared = prepare_message_arrangement(template(), 'a i', effects)
        broken = copy.deepcopy(prepared)
        broken['lyrics_adaptation']['note_groups']['lead'][0]['hold'] += 1
        with self.assertRaisesRegex(ValueError, 'quantidade de notas'):
            automatic_render_plan(broken, 'a i', effects)


if __name__ == '__main__':
    unittest.main()
