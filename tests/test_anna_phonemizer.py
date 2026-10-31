"""Cz VCCV recordings keep their native vowels and consonant boundaries."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "deploy/termux/phone-worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.phonemizer import english_vowel_candidates, english_vowel_source, phonemize


def _resolve(aliases):
    def resolve(candidates):
        return next((SimpleNamespace(alias=alias) for alias in candidates if alias in aliases), None)
    return resolve


@pytest.mark.parametrize(("word", "native"), [
    ("cat", "-k@"), ("cut", "-ku"), ("call", "-k9"), ("cot", "-ka"),
    ("kit", "-ki"), ("key", "-kE"), ("coat", "-kO"), ("put", "-p6"),
    ("boot", "-bo"), ("cake", "-kA"), ("cow", "-k8"), ("kite", "-kI"),
    ("coin", "-kQ"), ("bird", "-b3"),
])
def test_english_words_use_recorded_cz_vowels_instead_of_delta_symbols(word, native):
    # These are literal aliases in the DIVA release's oto.ini. In particular,
    # Cz o/u and E/i are different sounds, not case-insensitive alternatives.
    aliases = {"-k@", "-ku", "-k9", "-ka", "-ki", "-kE", "-kO", "-p6",
               "-bo", "-kA", "-k8", "-kI", "-kQ", "-b3"}
    moras = phonemize(word, voicebank_profile="english-cvvc", reading_language="en-US",
                     inventory_profile="anna-nyui-diva", resolve_alias=_resolve(aliases))
    nuclei = [mora for mora in moras if mora.role == "nucleus"]
    assert len(nuclei) == 1
    assert nuclei[0].candidates[0] == native


def test_dedicated_native_hh_e_recording_avoids_an_unrenderable_english_hee():
    moras = phonemize("hear", voicebank_profile="english-cvvc", reading_language="en-US",
                     inventory_profile="anna-nyui-diva", resolve_alias=_resolve({"-hhE", "Er"}))
    assert [mora.candidates[0] for mora in moras] == ["-hhE", "Er"]
    assert [mora.source_phonemes for mora in moras] == [("h", "i"), ("r",)]


@pytest.mark.parametrize(("word", "nucleus", "nasal", "glide"), [
    ("pão", "-pa", "9ng", "ng w"),
    ("mãe", "-ma", "9ng", "ng y"),
    ("muito", "-mo", "ong", "ng y"),
])
def test_portuguese_nasal_diphthongs_keep_nasal_before_glide(word, nucleus, nasal, glide):
    aliases = {nucleus, nasal, glide, "to"}
    resolver = _resolve(aliases)
    moras = phonemize(word, voicebank_profile="english-cvvc", reading_language="pt-BR",
                     inventory_profile="anna-nyui-diva", resolve_alias=resolver)
    assert [mora.role for mora in moras[:3]] == ["nucleus", "nasal", "glide"]
    assert [mora.candidates[0] for mora in moras[:3]] == [nucleus, nasal, glide]
    assert moras[0].coverage == "approximation"  # English has no native PT nasal vowel.
    assert all(resolver(mora.candidates) is not None for mora in moras)


def test_linguica_retains_same_vowel_nasal_exit_when_english_i_ng_is_absent():
    resolver = _resolve({"-lE", "En", "Eg", "gw", "_wE", "Es", "sa"})
    moras = phonemize("linguiça", voicebank_profile="english-cvvc", reading_language="pt-BR",
                     inventory_profile="anna-nyui-diva", resolve_alias=resolver)
    nasal = next(mora for mora in moras if mora.role == "nasal")
    assert nasal.candidates[0] == "En"
    assert nasal.coverage == "approximation"
    assert sum(mora.role == "nucleus" for mora in moras) == 3
    assert all(resolver(mora.candidates) is not None for mora in moras)


def test_native_cc_and_short_glide_attack_preserve_quase_without_a_new_syllable():
    resolver = _resolve({"-kw", "_wa", "az", "zE"})
    moras = phonemize("quase", voicebank_profile="english-cvvc", reading_language="pt-BR",
                     inventory_profile="anna-nyui-diva", resolve_alias=resolver)
    assert [mora.candidates[0] for mora in moras] == ["-kw", "_wa", "az", "zE"]
    assert [mora.role for mora in moras] == ["cluster", "nucleus", "vowel-exit", "nucleus"]
    assert sum(mora.role == "nucleus" for mora in moras) == 2


@pytest.mark.parametrize(("source", "native"), [
    (("a~",), "a"), (("a", "j"), "I"), (("a~", "w"), "a"),
    (("h", "i"), "E"), (("k", "o"), "9"), (("h", "@"), "x"),
    (("oU",), "O"), (("u",), "o"), (("V",), "u"),
])
def test_steady_vowel_comes_from_lexical_phones_even_for_joined_native_aliases(source, native):
    assert english_vowel_candidates(source, inventory_profile="anna-nyui-diva") == (native,)


def test_vowel_restart_preserves_lexical_diphthong_without_cz_xsampa_ambiguity():
    assert english_vowel_source(("p", "a", "j")) == ("a", "j")
    assert english_vowel_source(("k", "a~")) == ("a~",)
    assert english_vowel_source(("h", "i")) == ("i",)
    assert english_vowel_source(("k",)) == ()
    assert english_vowel_candidates(english_vowel_source(("p", "a", "j")),
                                    inventory_profile="anna-nyui-diva") == ("I",)


def test_shared_vps_phonemizer_is_byte_identical_and_keeps_nucleus_counts():
    worker = WORKER / "teto_renderer/phonemizer.py"
    shared = ROOT / "osaka_cd/phonetics/phonemizer.py"
    assert worker.read_bytes() == shared.read_bytes()
    from osaka_cd.phonetics.phonemizer import phonemize as shared_phonemize
    for language, text in (("pt-BR", "água quase pão mãe muito"), ("en-US", "hello beautiful world")):
        options = dict(voicebank_profile="english-cvvc", reading_language=language,
                       inventory_profile="anna-nyui-diva")
        actual, planned = phonemize(text, **options), shared_phonemize(text, **options)
        assert [mora.source_phonemes for mora in actual] == [mora.source_phonemes for mora in planned]
        assert sum(mora.role == "nucleus" for mora in actual) == sum(mora.role == "nucleus" for mora in planned)
