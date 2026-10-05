from __future__ import annotations

import sys
import unittest
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.phonemizer import phonemize
from teto_renderer.prosody import build_notes


def _aliases(text: str) -> list[str]:
    return [mora.candidates[0] for mora in phonemize(text)]


class TetoProsodyTests(unittest.TestCase):
    def test_cedilla_keeps_s_sound_before_accent_normalization(self):
        self.assertEqual(_aliases("aça"), ["あ", "さ"])
        self.assertEqual(_aliases("aça"), _aliases("assa"))
        self.assertNotEqual(_aliases("aça"), _aliases("aca"))

    def test_prevocalic_l_does_not_insert_an_extra_u_vowel(self):
        self.assertEqual(_aliases("Olá"), ["お", "ら"])
        self.assertEqual(_aliases("fala"), ["ふぁ", "ら"])
        self.assertEqual(_aliases("filha"), ["ふぃ", "りゃ"])

    def test_phrase_endings_follow_the_configured_base_pitch(self):
        statement = build_notes(phonemize("Olá, eu sou a Teto."), base_pitch="D4")
        question = build_notes(phonemize("Olá, eu sou a Teto?"), base_pitch="D4")
        self.assertEqual(statement[0].pitch, "D4")
        self.assertEqual(statement[-1].pitch, "C#4")
        self.assertEqual(question[-1].pitch, "D#4")
        self.assertEqual(statement[4].pitch, "D4")
        self.assertEqual(len({note.pitch for note in statement + question}), 3)

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
