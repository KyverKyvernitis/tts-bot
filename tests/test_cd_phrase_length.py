"""Finite spoken CD phrases checked with the unchanged worker's contracts.

The renderer callback produces controlled PCM, not an installed UTAU voice.
These tests verify exact musical consumption and mixed audio duration.
"""
from __future__ import annotations

import copy
import hashlib
import io
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cogs.tts.arrangement_audio import arrangement_cache_identity
from cogs.tts.performance import parse_performance_text
from osaka_cd.lyrics import _phonemes, automatic_render_plan, prepare_message_arrangement
from osaka_cd.render import render_arrangement

try:
    from tests.test_cd_adaptive_lyrics import assert_worker_accepts, template, worker_aligner
    from tests.test_cd_multivoice_render import tone
except ModuleNotFoundError:
    from test_cd_adaptive_lyrics import assert_worker_accepts, template, worker_aligner
    from test_cd_multivoice_render import tone


def lead_requests(plan):
    return [request for chunk in plan['chunks'] for request in chunk['parts']
            if request['role'] == 'lead']


def lead_notes(value):
    return next(part['notes'] for part in value['parts'] if part['role'] == 'lead')


def syllables(text):
    return sum(mora.role == 'nucleus' for word in text.split()
               for mora in _phonemes(word, 'pt-BR'))


class FiniteCDPhraseTests(unittest.TestCase):
    def setUp(self):
        self.align = worker_aligner()

    def phrase(self, raw_text, value=None):
        text, segments = parse_performance_text(raw_text, singing=True)
        value = value or template(duration=250)
        before = copy.deepcopy(value)
        prepared = prepare_message_arrangement(value, text, segments)
        plan = automatic_render_plan(prepared, text, segments)
        self.assertEqual(value, before, 'Message preparation must not alter a published CD')
        self.assertEqual(prepared['message_lyrics_mode'], 'automatic-lyrics-v2')
        for chunk in plan['chunks']:
            for request in chunk['parts']:
                assert_worker_accepts(self, request, self.align)
        return text, segments, prepared, plan

    def test_single_vowel_and_each_explicit_hold_consume_exactly_one_note(self):
        value = template(duration=250)
        source = copy.deepcopy(lead_notes(value))
        for raw, expected in [('a', 1), ('a ~', 2), ('a ~ ~', 3), ('a~', 2)]:
            with self.subTest(raw=raw):
                _, _, prepared, plan = self.phrase(raw, value)
                self.assertEqual(lead_notes(prepared), source[:expected])
                self.assertEqual(sum(len(request['score']['notes']) for request in lead_requests(plan)), expected)
                self.assertEqual(prepared['duration_ms'], expected * 250)

    def test_one_word_uses_its_syllable_count_without_implicit_tail_holds(self):
        _, _, prepared, plan = self.phrase('porra')
        self.assertEqual(syllables('porra'), 2)
        self.assertEqual(len(lead_notes(prepared)), 2)
        tokens = [token for request in lead_requests(plan)
                  for token in request['adaptive_text'].split()]
        self.assertEqual(tokens, ['porra'])
        self.assertEqual(prepared['duration_ms'], 500)

    def test_long_word_repeats_the_original_motif_instead_of_subdividing_notes(self):
        value = template(duration=250)
        source = copy.deepcopy(lead_notes(value))
        expected = syllables('constitucionalmente')
        self.assertGreater(expected, len(source))
        _, _, prepared, plan = self.phrase('constitucionalmente', value)
        actual = lead_notes(prepared)
        self.assertEqual(len(actual), expected)
        for index, note in enumerate(actual):
            original = source[index % len(source)]
            self.assertEqual(note['midi_note'], original['midi_note'])
            self.assertEqual(note['duration_ms'], original['duration_ms'])
            self.assertEqual(note['start_ms'], original['start_ms'] + value['duration_ms'] * (index // len(source)))
        self.assertEqual([word for request in lead_requests(plan)
                          for word in request['adaptive_text'].split() if word != '~'], ['constitucionalmente'])
        self.assertEqual(prepared['duration_ms'], actual[-1]['start_ms'] + actual[-1]['duration_ms'])

    def test_all_guide_parts_stop_at_the_consumed_lead_boundary(self):
        value = template(duration=250)
        value['parts'][1]['notes'] = [{'midi_note': 67, 'start_ms': 0,
                                      'duration_ms': 1000, 'velocity': 90}]
        # A voice that begins after this utterance ends must not be rendered.
        value['parts'][2]['notes'] = [{'midi_note': 48, 'start_ms': 900,
                                      'duration_ms': 100, 'velocity': 90}]
        _, _, prepared, plan = self.phrase('a ~', value)
        self.assertEqual(prepared['duration_ms'], 500)
        self.assertEqual({part['role'] for part in prepared['parts']}, {'lead', 'harmony'})
        harmony = next(part for part in prepared['parts'] if part['role'] == 'harmony')
        self.assertEqual(harmony['notes'], [{'midi_note': 67, 'start_ms': 0,
                                            'duration_ms': 500, 'velocity': 90}])
        for chunk in plan['chunks']:
            for request in chunk['parts']:
                self.assertLessEqual(chunk['start_ms'] + request['score']['duration_ms'], 500)

    def test_hold_crossing_a_rest_reattacks_a_vowel_without_extra_notes(self):
        value = template(rests=True, duration=250)
        _, _, prepared, plan = self.phrase('a ~', value)
        self.assertEqual(lead_notes(prepared), lead_notes(value)[:2])
        requests = lead_requests(plan)
        self.assertEqual(sum(len(request['score']['notes']) for request in requests), 2)
        self.assertEqual(prepared['duration_ms'], 1000)
        # The original physical aligner is exercised by phrase(); no request
        # may sustain one nucleus over the 500 ms MIDI rest.
        self.assertTrue(any('a' in request['adaptive_text'] for request in requests))

    def test_long_phrase_keeps_each_word_intact_across_physical_chunks(self):
        value = template(duration=100, parts=1)
        raw = ' '.join(['amor'] * 80)
        expected = syllables(raw)
        _, _, prepared, plan = self.phrase(raw, value)
        requests = lead_requests(plan)
        self.assertGreater(len(requests), 1, 'The 128-note worker budget must be honored')
        self.assertEqual(sum(len(request['score']['notes']) for request in requests), expected)
        self.assertEqual(len(lead_notes(prepared)), expected)
        tokens = [token for request in requests for token in request['adaptive_text'].split()]
        self.assertEqual(tokens, ['amor'] * 80)
        for index, note in enumerate(lead_notes(prepared)):
            self.assertEqual(note['duration_ms'], 100)
            self.assertEqual(note['midi_note'], 60 + index % 4)

    def test_effects_survive_the_finite_cut_and_preserve_the_word(self):
        _, _, prepared, plan = self.phrase('<porra~>')
        self.assertEqual(len(lead_notes(prepared)), 3)
        emitted = [segment for request in lead_requests(plan)
                   for segment in request.get('adaptive_segments', [])]
        self.assertEqual([segment['text'] for segment in emitted if segment['text'] != '~'], ['porra'])
        self.assertTrue(emitted[0]['fade_in'])
        self.assertTrue(emitted[-1]['fade_out'])

    def test_v1_and_v2_message_audio_never_share_the_outer_cache_identity(self):
        _, segments, prepared, _ = self.phrase('a ~')
        legacy = copy.deepcopy(prepared)
        legacy['message_lyrics_mode'] = 'automatic-lyrics-v1'
        def item(arrangement):
            return SimpleNamespace(ttv_arrangement=arrangement, ttv_arrangement_hash='',
                                   engine='teto', text='a ~', ttv_segments=segments)
        self.assertNotEqual(arrangement_cache_identity(item(legacy)),
                            arrangement_cache_identity(item(prepared)))


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for controlled PCM mixing')
class FiniteCDPhraseAudioTests(unittest.TestCase):
    def test_real_mix_has_only_the_consumed_phrase_and_no_full_cd_silent_tail(self):
        text, segments = parse_performance_text('a ~', singing=True)
        prepared = prepare_message_arrangement(template(duration=250), text, segments)
        self.assertEqual(prepared['duration_ms'], 500)
        calls = []
        align = worker_aligner()
        def renderer(**request):
            calls.append(request)
            assert_worker_accepts(self, {**request, 'adaptive_text': request['text']}, align)
            raw = tone(request['score'], {'lead': 440, 'harmony': 660, 'bass': 220}[request['role']], 80)
            return raw, {'midi_preroll_ms': 80, 'sha256': hashlib.sha256(raw).hexdigest()}
        with tempfile.TemporaryDirectory() as cache:
            result = render_arrangement(prepared, text, renderer, segments=segments,
                                        cache_dir=cache, render_profile_fingerprint='finite-test-bank')
            with wave.open(io.BytesIO(result['audio']), 'rb') as decoded:
                self.assertEqual(decoded.getnchannels(), 2)
                self.assertAlmostEqual(decoded.getnframes() / decoded.getframerate(), 0.580, places=3)
                self.assertLess(decoded.getnframes() / decoded.getframerate(), 1)
            self.assertEqual(result['duration_ms'], 500)
            self.assertEqual({request['role'] for request in calls}, {'lead', 'harmony', 'bass'})
            self.assertEqual(sum(len(request['score']['notes']) for request in calls if request['role'] == 'lead'), 2)


if __name__ == '__main__':
    unittest.main()
