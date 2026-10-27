"""Compact syllable sustains keep every authored marker and literal boundary."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ttv_tilde_compact_performance", ROOT / "cogs/tts/performance.py")
performance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(performance)
parse = performance.parse_performance_text


def hold_count(segments):
    return sum(bool(part["hold"]) for part in segments or [])


class CompactTildeTests(unittest.TestCase):
    def test_internal_marker_targets_the_preceding_syllable(self):
        for singing in (False, True):
            with self.subTest(singing=singing):
                clean, parts = parse("a~mor", singing=singing)
                self.assertEqual(clean, "a mor")
                self.assertEqual([part["text"] for part in parts], ["a", "mor"])
                self.assertEqual([part["hold"] for part in parts], [True, False])

    def test_compact_runs_preserve_every_marker(self):
        for singing in (False, True):
            for count in (1, 2, 3, 12):
                with self.subTest(singing=singing, count=count):
                    _, parts = parse("a" + "~" * count + "mor", singing=singing)
                    self.assertEqual(hold_count(parts), count)
                    self.assertEqual(parts[0]["text"], "a")
                    self.assertEqual(parts[-1]["text"], "mor")
                    self.assertFalse(parts[-1]["hold"])

    def test_compact_and_spaced_forms_have_the_same_effect(self):
        for singing in (False, True):
            forms = ("a~~~mor", "a~ ~~ mor", "a ~~~ mor", "a ~ ~ ~ mor")
            for source in forms:
                with self.subTest(singing=singing, source=source):
                    clean, parts = parse(source, singing=singing)
                    self.assertEqual(hold_count(parts), 3)
                    self.assertEqual([part["text"] for part in parts if part["text"] != "~"],
                                     ["a", "mor"])
                    self.assertEqual(clean, " ".join(part["text"] for part in parts))

    def test_last_symbol_requests_singing_and_its_own_sustain(self):
        for source, expected in (("a~", 1), ("a~~", 2), ("a~~~", 3),
                                 ("a~~~mor~", 4), ("a~mor~~", 3)):
            with self.subTest(source=source):
                lyrics, singing = performance.singing_request(source)
                self.assertTrue(singing)
                _, parts = parse(lyrics, singing=singing)
                self.assertEqual(hold_count(parts), expected)

    def test_fades_and_punctuation_stay_on_the_correct_syllables(self):
        clean, parts = parse("<a~~mor~~~>!", singing=True)
        self.assertEqual(clean, "a ~ mor! ~ ~")
        words = [part for part in parts if part["text"] != "~"]
        self.assertEqual([part["text"] for part in words], ["a", "mor!"])
        self.assertTrue(words[0]["fade_in"])
        self.assertFalse(words[0]["fade_out"])
        self.assertTrue(words[1]["fade_out"])
        self.assertFalse(words[1]["fade_in"])
        self.assertEqual(hold_count(parts), 5)

    def test_escaped_quoted_code_and_url_markers_remain_literal(self):
        for text in (r"a\~mor", '"a~~mor"', "`a~~mor`", "https://example.test/a~~~mor"):
            with self.subTest(text=text):
                clean, parts = parse(text, singing=True)
                self.assertIsNone(parts)
                self.assertEqual(clean, text.replace("\\", ""))
        clean, parts = parse(r"a\~mor~~ fim", singing=True)
        self.assertEqual(parts[0]["text"], "a~mor")
        self.assertEqual(hold_count(parts), 2)

    def test_holds_cannot_precede_any_syllable_or_cross_phrase_boundary(self):
        for text in ("~a", "~~a", "~~~", "a | ~~mor", "a|~mor"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "Use|efeito"):
                parse(text, singing=True)
        clean, parts = parse("a~~~|mor", singing=True)
        self.assertEqual(hold_count(parts), 3)
        self.assertEqual(parts[2]["pause_after_ms"], performance.PAUSE_MILLISECONDS)
        self.assertEqual(clean, "a ~ ~ | mor")

    def test_existing_single_suffix_keeps_its_wire_shape(self):
        clean, parts = parse("Olá <Miku~> tudo bem", singing=True)
        self.assertEqual(clean, "Olá Miku tudo bem")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[1], {"text": "Miku", "fade_in": True,
                                   "fade_out": True, "hold": True, "pause_after_ms": 0})
        self.assertTrue(all(set(part) == {"text", "fade_in", "fade_out", "hold", "pause_after_ms"}
                            for part in parts))

    def test_plain_punctuation_and_non_effect_words_keep_their_old_route(self):
        for text in ("!!!", "olá !", "a-mor", "tudo bem?", "...", "a < b"):
            with self.subTest(text=text):
                self.assertEqual(parse(text), (text, None))

    def test_long_runs_obey_the_existing_physical_request_limit(self):
        with self.assertRaisesRegex(ValueError, "128"):
            parse("a" + "~" * 129, singing=True)


if __name__ == "__main__":
    unittest.main()
