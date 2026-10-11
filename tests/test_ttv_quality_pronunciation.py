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
from teto_renderer.ptbr_g2p import g2p_english_word, g2p_word
from teto_renderer.voicebank import VoicebankIndex


def _moras(text: str, aliases: set[str]):
    def resolve(candidates):
        return next((SimpleNamespace(
            alias=alias, preutterance_ms=30.0, overlap_ms=10.0, consonant_ms=40.0,
        ) for alias in candidates if alias in aliases), None)

    return phonemize(text, voicebank_profile="english-cvvc", resolve_alias=resolve)


def test_miku_name_keeps_initial_stress_in_portuguese():
    for spelling in ("Miku", "miku", "MIKU"):
        word = g2p_word(spelling)
        assert word.phonemes == ("m", "i", "k", "u")
        assert [syllable.stressed for syllable in word.syllables] == [True, False]
    assert not any(syllable.stressed for syllable in g2p_word("Miku", deaccented=True).syllables)


def test_open_vowels_are_exact_lexical_choices_without_changing_past_tense():
    assert g2p_word("pode").phonemes == ("p", "O", "dZ", "i")
    assert g2p_word("pôde").phonemes == ("p", "o", "dZ", "i")
    assert g2p_word("nossa").phonemes == ("n", "O", "s", "a")
    assert g2p_word("Teto").phonemes == ("t", "e", "t", "u")


def test_names_keep_recognizable_vowels_in_english():
    assert g2p_english_word("Miku").phonemes == ("m", "i", "k", "u")
    assert g2p_english_word("Teto").phonemes == ("t", "E", "t", "oU")
    assert [s.stressed for s in g2p_english_word("Miku").syllables] == [True, False]
    assert [s.stressed for s in g2p_english_word("Teto").syllables] == [True, False]


def test_simple_cv_retains_the_preceding_recorded_vowel_exit():
    moras = _moras("Miku", {"- mi", "i k", "ku"})
    assert [mora.candidates[0] for mora in moras] == ["- mi", "i k", "ku"]
    assert [mora.role for mora in moras] == ["nucleus", "vowel-exit", "nucleus"]
    assert moras[1].source_phonemes == ("i", "k")
    assert moras[1].coverage == "cvvc-transition"


def test_a_complete_vcv_is_not_given_a_duplicate_vc():
    moras = _moras("Miku", {"- mi", "i k", "i ku", "ku"})
    assert [mora.candidates[0] for mora in moras] == ["- mi", "i ku"]


def test_no_extra_note_is_created_when_the_vc_is_unavailable():
    moras = _moras("Miku", {"- mi", "ku"})
    assert [mora.candidates[0] for mora in moras] == ["- mi", "ku"]
    assert all(mora.role == "nucleus" for mora in moras)


def test_nasal_glide_and_cluster_already_owned_by_the_plan_are_not_duplicated():
    nasal = _moras("campo", {"- ka", "a N", "a p", "pu"})
    assert [mora.role for mora in nasal] == ["nucleus", "nasal", "nucleus"]
    glide = _moras("baile", {"- baI", "aI l", "li"})
    assert [mora.candidates[0] for mora in glide] == ["- baI", "li"]
    cluster = _moras("outro", {"- o", "o tr", "tru"})
    assert [mora.candidates[0] for mora in cluster] == ["- o", "tru"]


@pytest.mark.parametrize("language,text", [("pt-BR", "Miku"), ("en-US", "Miku")])
def test_real_miku_inventory_can_render_the_added_transition(language: str, text: str):
    configured = os.environ.get("MIKU_CVVC_TEST_BANK", "")
    if not configured:
        pytest.skip("requires the pinned Miku English CVVC bank")
    bank = VoicebankIndex.load(configured, alias_suffix="_D4", case_sensitive=True)
    moras = phonemize(
        text, voicebank_profile="english-cvvc", reading_language=language,
        inventory_profile="miku-v4-english", resolve_alias=bank.resolve,
    )
    assert [mora.candidates[0] for mora in moras] == ["m i:_D4", "i: k_D4", "k u:_D4"]
    assert all(bank.resolve(mora.candidates) is not None for mora in moras)
