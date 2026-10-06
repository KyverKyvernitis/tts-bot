from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.phonemizer import phonemize
from teto_renderer.prosody import build_notes, encode_pitchbend


def _aliases(text: str) -> list[str]:
    return [mora.candidates[0] for mora in phonemize(text)]


def _standard_aliases(text: str, available: set[str]) -> list[str]:
    def resolve(candidates):
        for candidate in candidates:
            if candidate in available:
                return SimpleNamespace(alias=candidate)
        return None

    return [mora.candidates[0] for mora in phonemize(text, resolve_alias=resolve)]



def _english_moras(text: str, available: set[str]):
    def resolve(candidates):
        for candidate in candidates:
            if candidate in available:
                return SimpleNamespace(alias=candidate)
        return None

    return phonemize(
        text, resolve_alias=resolve, voicebank_profile="english-cvvc"
    )

def _stress_indexes(text: str) -> list[int]:
    return [index for index, mora in enumerate(phonemize(text)) if mora.stressed]


class TetoProsodyTests(unittest.TestCase):
    def test_cedilla_keeps_s_sound_before_accent_normalization(self):
        self.assertEqual(_aliases("aça"), ["あ", "さ"])
        self.assertEqual(_aliases("aça"), _aliases("assa"))
        self.assertNotEqual(_aliases("aça"), _aliases("aca"))

    def test_prevocalic_l_and_palatal_l_do_not_add_full_extra_vowels(self):
        self.assertEqual(_aliases("Olá"), ["お", "ら"])
        self.assertEqual(_aliases("fala"), ["ふぁ", "ら"])
        self.assertEqual(_aliases("filha"), ["ふぃ", "りゃ"])

    def test_nasal_vowels_and_final_consonants_are_short_auxiliary_units(self):
        standard = {"ぼ", "し", "ん", "け", "る", "ぶ", "ら", "じ", "う"}
        self.assertEqual(_standard_aliases("bom", standard), ["ぼ", "ん"])
        self.assertEqual(_standard_aliases("sim", standard), ["し", "ん"])

        quer = phonemize("quer")
        self.assertEqual(quer[-1].role, "coda")
        self.assertIn("る", quer[-1].candidates)
        notes = build_notes(quer)
        self.assertLess(notes[-1].duration_ms, notes[0].duration_ms)
        self.assertLess(notes[-1].gain, notes[0].gain)

        brasil = phonemize("Brasil")
        self.assertEqual(brasil[0].role, "epenthetic")
        self.assertEqual(brasil[-1].role, "glide")

    def test_ptbr_r_and_intervocalic_s_use_closer_cv_approximations(self):
        standard = {"は", "と", "か", "ふ", "ざ"}
        self.assertEqual(_standard_aliases("rato", standard), ["は", "と"])
        self.assertEqual(_standard_aliases("carro", standard), ["か", "ふ"])
        self.assertEqual(_standard_aliases("casa", standard), ["か", "ざ"])

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
        stressed = [m for m in computador if m.stressed]
        self.assertEqual(len(stressed), 1)
        self.assertEqual(stressed[0].source_phonemes, ("d", "o"))
        self.assertEqual(computador[-1].role, "coda")

    def test_english_cvvc_uses_real_cluster_and_vcv_aliases_without_epenthetic_vowels(self):
        available = {"- br", "ra", "a zi", "i w"}
        moras = _english_moras("Brasil", available)
        aliases = [m.candidates[0] for m in moras]
        self.assertEqual(aliases, ["- br", "ra", "a zi", "i w"])
        self.assertNotIn("epenthetic", [m.role for m in moras])
        self.assertEqual(moras[0].coverage, "cluster-hit")
        self.assertEqual(moras[2].coverage, "cvvc-direct")

    def test_english_cvvc_maps_pt_specific_sounds_to_composable_xsampa(self):
        available = {"- fi", "i lj", "lju", "- ba", "a nj", "nje", "- ka", "a hu", "hu"}
        filha = _english_moras("filha", available)
        banho = _english_moras("banho", available)
        carro = _english_moras("carro", available)
        joined = " ".join(m.candidates[0] for m in filha + banho + carro)
        self.assertNotRegex(joined, r"[ぁ-ゖァ-ヺ]")
        self.assertIn("lj", joined)
        self.assertIn("nj", joined)
        self.assertIn("h", joined)
        self.assertFalse(any(m.role == "epenthetic" for m in filha + banho + carro))

    def test_english_cvvc_transition_roles_remain_short_and_quiet(self):
        available = {"- br", "ra", "a zi", "i w"}
        notes = build_notes(_english_moras("Brasil", available))
        auxiliaries = [n for n in notes if n.role != "nucleus"]
        self.assertTrue(auxiliaries)
        self.assertTrue(all(n.duration_ms <= 56 for n in auxiliaries))
        self.assertTrue(all(n.gain < 0.9 for n in auxiliaries))
        self.assertTrue(all(n.pitchbend == "AA" for n in auxiliaries))
        self.assertTrue(all(n.coverage in {"cluster-hit", "cvvc-transition", "approximation"} for n in auxiliaries))
        nuclei = [n for n in notes if n.role == "nucleus"]
        self.assertTrue(nuclei)
        self.assertTrue(any(n.pitchbend != "AA" for n in nuclei))

    def test_clusters_use_quiet_short_epenthesis_instead_of_full_japanese_syllables(self):
        brasil = build_notes(phonemize("Brasil"))
        problema = build_notes(phonemize("problema"))
        self.assertEqual(brasil[0].role, "epenthetic")
        self.assertLess(brasil[0].duration_ms, 70)
        self.assertLess(brasil[0].gain, 0.8)
        self.assertEqual([note.role for note in problema].count("epenthetic"), 2)
        self.assertTrue(all(note.duration_ms < 70 for note in problema if note.role == "epenthetic"))

    def test_diphthongs_and_nasal_tails_are_not_full_second_vowels(self):
        nao = phonemize("não")
        mae = phonemize("mãe")
        self.assertEqual([m.role for m in nao], ["nucleus", "nasal", "glide"])
        self.assertEqual([m.role for m in mae], ["nucleus", "nasal", "glide"])
        nao_notes = build_notes(nao)
        self.assertLess(nao_notes[1].duration_ms, nao_notes[0].duration_ms)
        self.assertLess(nao_notes[2].duration_ms, nao_notes[0].duration_ms)

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

    def test_function_words_are_deaccented_and_get_spoken_ptbr_reduction(self):
        plain = phonemize("de para com")
        self.assertTrue(plain)
        self.assertTrue(all(m.deaccented for m in plain))
        self.assertFalse(any(m.stressed for m in plain))
        # Neutral Brazilian Portuguese commonly realizes unstressed 'de' as /dZi/.
        self.assertEqual(phonemize("de")[0].source_phonemes, ("dZ", "i"))

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
            self.assertTrue(all(70 <= note.duration_ms <= 500 for note in notes if note.role == "nucleus"))
            self.assertTrue(all(32 <= note.duration_ms <= 500 for note in notes if note.role != "nucleus"))
            self.assertTrue(all(0 <= note.pause_after_ms <= 1000 for note in notes))


if __name__ == "__main__":
    unittest.main()


def test_phrase_contour_stays_on_last_lexical_nucleus_when_coda_follows():
    moras = phonemize("melhor?")
    notes = build_notes(moras, base_pitch="C4")
    assert moras[-1].role == "coda"
    assert notes[-1].contour == "coda"
    nucleus_indexes = [i for i, mora in enumerate(moras) if mora.role == "nucleus"]
    assert notes[nucleus_indexes[-1]].contour == "question-rise"
