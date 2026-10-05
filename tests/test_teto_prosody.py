from __future__ import annotations

import sys
import unittest
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.phonemizer import phonemize
from teto_renderer.prosody import build_notes, encode_pitchbend


def _aliases(text: str) -> list[str]:
    return [mora.candidates[0] for mora in phonemize(text)]


def _stress_indexes(text: str) -> list[int]:
    return [index for index, mora in enumerate(phonemize(text)) if mora.stressed]


class TetoProsodyTests(unittest.TestCase):
    def test_cedilla_keeps_s_sound_before_accent_normalization(self):
        self.assertEqual(_aliases("aça"), ["あ", "さ"])
        self.assertEqual(_aliases("aça"), _aliases("assa"))
        self.assertNotEqual(_aliases("aça"), _aliases("aca"))

    def test_prevocalic_l_does_not_insert_an_extra_u_vowel(self):
        self.assertEqual(_aliases("Olá"), ["お", "ら"])
        self.assertEqual(_aliases("fala"), ["ふぁ", "ら"])
        self.assertEqual(_aliases("filha"), ["ふぃ", "りゃ"])

    def test_common_ptbr_final_consonants_avoid_extra_cv_syllables(self):
        self.assertEqual(_aliases("bom"), ["ぼ", "ん"])
        self.assertEqual(_aliases("sim"), ["し", "ん"])
        self.assertEqual(_aliases("quer"), ["け"])
        self.assertEqual(_aliases("Brasil")[-2:], ["じ", "う"])

    def test_ptbr_r_and_intervocalic_s_use_closer_cv_approximations(self):
        self.assertEqual(_aliases("rato"), ["は", "と"])
        self.assertEqual(_aliases("carro"), ["か", "ほ"])
        self.assertEqual(_aliases("casa"), ["か", "ざ"])

    def test_portuguese_stress_prefers_accents_then_common_default_rules(self):
        teto = phonemize("Teto")
        bonito = phonemize("bonito")
        voce = phonemize("você")
        musica = phonemize("música")
        computador = phonemize("computador")
        self.assertEqual([m.stressed for m in teto], [True, False])
        self.assertEqual([m.stressed for m in bonito], [False, True, False])
        self.assertEqual([m.stressed for m in voce], [False, True])
        self.assertEqual([m.stressed for m in musica], [True, False, False])
        self.assertTrue(computador[-1].stressed)

    def test_word_gaps_are_short_and_punctuation_keeps_hierarchy(self):
        plain = phonemize("Olá eu")
        comma = phonemize("Olá, eu")
        question = phonemize("Olá?")
        first_word_end = next(m for m in plain if m.word_end)
        comma_end = next(m for m in comma if m.phrase_end == ",")
        question_end = next(m for m in question if m.phrase_end == "?")
        self.assertLessEqual(first_word_end.pause_after_ms, 8)
        self.assertGreater(comma_end.pause_after_ms, first_word_end.pause_after_ms)
        self.assertGreater(question_end.pause_after_ms, comma_end.pause_after_ms)

    def test_function_words_are_deaccented_without_changing_aliases(self):
        plain = phonemize("de para com")
        self.assertTrue(plain)
        self.assertTrue(all(m.deaccented for m in plain))
        self.assertFalse(any(m.stressed for m in plain))
        self.assertEqual(_aliases("de"), ["で"])

        phrase = build_notes(phonemize("eu sou a Teto"))
        deaccented = [note for note in phrase if note.deaccented]
        stressed = [note for note in phrase if note.stressed]
        self.assertTrue(deaccented)
        self.assertTrue(stressed)
        self.assertLess(max(note.gain for note in deaccented), max(note.gain for note in stressed))

    def test_question_and_statement_have_distinct_but_bounded_contours(self):
        statement = build_notes(phonemize("Teto."))
        question = build_notes(phonemize("Teto?"))
        self.assertNotEqual(statement[-1].pitchbend, question[-1].pitchbend)
        self.assertEqual(statement[-1].contour, "statement-fall")
        self.assertEqual(question[-1].contour, "question-rise")
        self.assertTrue(all(len(note.pitchbend) < 256 for note in statement + question))

    def test_phrase_contours_keep_coarse_pitch_stable_and_use_pitchbend(self):
        statement = build_notes(phonemize("Olá, eu sou a Teto."), base_pitch="D4")
        question = build_notes(phonemize("Olá, eu sou a Teto?"), base_pitch="D4")
        self.assertEqual({note.pitch for note in statement + question}, {"D4"})
        self.assertEqual(statement[-1].contour, "statement-fall")
        self.assertEqual(question[-1].contour, "question-rise")
        self.assertNotEqual(statement[-1].pitchbend, question[-1].pitchbend)
        self.assertTrue(any(note.pitchbend != "AA" for note in statement))
        self.assertTrue(any(note.stressed for note in statement))

    def test_stressed_mora_is_longer_and_slightly_louder_than_neighbor(self):
        notes = build_notes(phonemize("bonito"))
        self.assertGreater(notes[1].duration_ms, notes[0].duration_ms)
        self.assertGreater(notes[1].duration_ms, notes[2].duration_ms)
        self.assertGreater(notes[1].gain, notes[0].gain)

    def test_utau_pitchbend_encoder_matches_known_signed_12bit_values(self):
        self.assertEqual(encode_pitchbend([0]), "AA")
        self.assertEqual(encode_pitchbend([1]), "AB")
        self.assertEqual(encode_pitchbend([-1]), "//")
        self.assertEqual(encode_pitchbend([1, 1, 1, 1]), "AB#4#")

    def test_rate_shortens_speech_and_pauses_without_changing_pronunciation(self):
        moras = phonemize("Olá, eu sou a Teto.")
        normal = build_notes(moras)
        faster = build_notes(moras, speech_rate=1.25)
        self.assertEqual(
            [(note.candidates, note.pitch) for note in normal],
            [(note.candidates, note.pitch) for note in faster],
        )
        self.assertLess(sum(n.duration_ms for n in faster), sum(n.duration_ms for n in normal))
        self.assertLess(sum(n.pause_after_ms for n in faster), sum(n.pause_after_ms for n in normal))
        self.assertEqual([n.pitch for n in normal], [n.pitch for n in faster])

    def test_invalid_rates_are_safe_and_extreme_rates_are_bounded(self):
        moras = phonemize("Teto.")
        normal = build_notes(moras)
        for rate in (0, -1, float("nan"), float("inf"), "invalid"):
            with self.subTest(rate=rate):
                self.assertEqual(build_notes(moras, speech_rate=rate), normal)
        for rate in (0.001, 1000):
            notes = build_notes(moras, speech_rate=rate)
            self.assertTrue(all(70 <= note.duration_ms <= 500 for note in notes))
            self.assertTrue(all(0 <= note.pause_after_ms <= 1000 for note in notes))


if __name__ == "__main__":
    unittest.main()
