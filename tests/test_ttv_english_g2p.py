from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.phonemizer import phonemize
from teto_renderer.ptbr_g2p import (
    CMUDICT_LICENSE,
    CMUDICT_SOURCE_COMMIT,
    CMUDICT_WORD_COUNT,
    _english_dictionary,
    expand_english_numbers,
    g2p_english_word,
    g2p_word,
)


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("hello", ("h", "@", "l", "oU")),
        ("world", ("w", "3", "r", "l", "d")),
        ("the", ("D", "@")),
        ("this", ("D", "I", "s")),
        ("you", ("j", "u")),
        ("English", ("I", "N", "g", "l", "I", "S")),
        ("thought", ("T", "O", "t")),
        ("though", ("D", "oU")),
        ("through", ("T", "r", "u")),
        ("voice", ("v", "OI", "s")),
        ("can't", ("k", "{", "n", "t")),
        ("water", ("w", "O", "t", "@", "r")),
    ],
)
def test_real_english_pronunciations_keep_english_vowels_and_consonants(word, expected):
    assert g2p_english_word(word).phonemes == expected


def test_english_stress_and_syllable_boundaries_come_from_pronunciation():
    hello = g2p_english_word("hello")
    assert [syllable.stressed for syllable in hello.syllables] == [False, True]
    assert hello.syllables[0].vowel == "@"
    assert hello.syllables[1].vowel == "oU"
    english = g2p_english_word("English")
    assert english.syllables[0].coda == ("N",)
    assert english.syllables[1].onset == ("g", "l")
    assert g2p_english_word("world").syllables[0].coda == ("r", "l", "d")
    assert not any(phone.stressed for phone in g2p_english_word("hello", deaccented=True).phones)


def test_dictionary_is_complete_pinned_offline_data_with_redistribution_license():
    lexicon = _english_dictionary()
    assert len(lexicon) == CMUDICT_WORD_COUNT == 126052
    assert CMUDICT_SOURCE_COMMIT == "74790861f652b15e4ac49015a90074ad62a27690"
    assert "Carnegie Mellon University" in CMUDICT_LICENSE
    assert "Redistribution and use in source and binary forms" in CMUDICT_LICENSE
    assert "THIS SOFTWARE IS PROVIDED" in CMUDICT_LICENSE
    # Broad lexicon entries that could not be covered by a hand-written demo list.
    for word in ("biodiversity", "microprocessor", "photosynthesis", "catastrophe", "nevertheless", "synthesizer"):
        assert word in lexicon
        assert g2p_english_word(word).syllables


@pytest.mark.parametrize("text", ["Olá. Eu sou a Teto.", "banho filha não", "Brasil, problema e trabalho.", "Hello world!"])
def test_default_and_explicit_portuguese_are_identical_in_both_banks(text):
    for profile in ("standard", "english-cvvc"):
        assert phonemize(text, voicebank_profile=profile) == phonemize(text, voicebank_profile=profile, reading_language="pt-BR")


def test_portuguese_requests_do_not_decode_english_dictionary():
    _english_dictionary.cache_clear()
    assert g2p_word("cidade").phonemes == ("s", "i", "d", "a", "dZ", "i")
    assert phonemize("Olá, você está bem?", reading_language="pt-BR")
    assert _english_dictionary.cache_info().currsize == 0


def test_english_contractions_stay_whole_and_do_not_use_portuguese_rules():
    english = phonemize("You can't.", voicebank_profile="english-cvvc", reading_language="en-US")
    assert {mora.word_index for mora in english} == {0, 1}
    assert english[0].source_phonemes == ("j", "u")
    assert any("{" in mora.source_phonemes for mora in english)
    assert any("t" in mora.source_phonemes for mora in english)
    assert english[-1].phrase_end == "."
    curly = phonemize("You can’t.", voicebank_profile="english-cvvc", reading_language="en-US")
    assert curly == english
    assert phonemize("You can't.", voicebank_profile="english-cvvc") != english


def test_english_cvvc_selection_resolves_the_intended_phonemes_and_stress():
    aliases = {"- h@", "loU", "- DI", "I s", "- ju", "- DoU", "- TO", "O t"}

    def resolve(candidates):
        return next((SimpleNamespace(alias=alias) for alias in candidates if alias in aliases), None)

    moras = phonemize("Hello this you though thought.", voicebank_profile="english-cvvc", reading_language="en-US", resolve_alias=resolve)
    assert [mora.candidates[0] for mora in moras] == ["- h@", "loU", "- DI", "I s", "- ju", "- DoU", "- TO", "O t"]
    assert not moras[0].stressed and moras[1].stressed
    assert all(resolve(mora.candidates) is not None for mora in moras)


def test_unknown_english_words_use_english_spelling_and_acronyms():
    assert "foobizzle" not in _english_dictionary()
    fallback = g2p_english_word("foobizzle")
    assert "u" in fallback.phonemes  # English oo, rather than Portuguese o-o.
    assert fallback.phonemes[:2] == ("f", "u")
    assert fallback.syllables
    assert g2p_english_word("TTV").phonemes == ("t", "i", "t", "i", "v", "i")


@pytest.mark.parametrize(
    ("source", "expected"),
    [("25", "twenty five"), ("007", "zero zero seven"), ("-3.5", "minus three point five"), ("1000000", "one million"), ("1,234.5", "one thousand two hundred thirty four point five")],
)
def test_english_numbers_are_spoken_instead_of_dropped(source, expected):
    assert expand_english_numbers(source) == expected
    assert phonemize(source, reading_language="en-US", voicebank_profile="english-cvvc")


def test_english_rejects_japanese_bank_and_unknown_language_without_silent_fallback():
    with pytest.raises(ValueError, match="English-CVVC"):
        phonemize("Hello", reading_language="en-US", voicebank_profile="standard")
    with pytest.raises(ValueError, match="idioma"):
        phonemize("Hello", reading_language="ja-JP", voicebank_profile="english-cvvc")


def test_real_teto_english_bank_has_every_planned_alias_when_assets_are_available():
    bank = os.environ.get("TETO_ENGLISH_TEST_ROOT")
    if not bank:
        pytest.skip("set TETO_ENGLISH_TEST_ROOT to the official extracted Teto English bank")
    from teto_renderer.voicebank import VoicebankIndex

    index = VoicebankIndex.load(bank)
    for phrase in ("Hello world.", "The quick brown fox jumps over the lazy dog.", "This is an English voice.", "You have 25 new messages.", "Though through thought."):
        moras = phonemize(phrase, reading_language="en-US", voicebank_profile="english-cvvc", resolve_alias=index.resolve)
        assert moras
        assert all(index.resolve(mora.candidates) is not None for mora in moras), phrase
