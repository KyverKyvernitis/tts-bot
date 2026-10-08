from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.phonemizer import phonemize
from teto_renderer.phonemizer import _english_cluster_units, _english_coda_units, _english_resolve


def _moras(text: str, aliases: set[str]):
    def resolve(candidates):
        return next((SimpleNamespace(alias=alias) for alias in candidates if alias in aliases), None)

    return phonemize(text, voicebank_profile="english-cvvc", resolve_alias=resolve)


def test_nasal_diphthong_does_not_use_oral_diphthong_then_append_nasal():
    moras = _moras("não", {"- naU", "- na", "a N", "N w"})
    assert [mora.candidates[0] for mora in moras] == ["- na", "a N", "N w"]
    assert [mora.role for mora in moras] == ["nucleus", "nasal", "glide"]
    assert moras[0].source_phonemes == ("n", "a~")
    assert moras[0].coverage == "approximation"


def test_final_nasal_e_glide_stays_after_nasal_tail():
    moras = _moras("bem", {"- beI", "- be", "e N", "N j"})
    assert [mora.candidates[0] for mora in moras] == ["- be", "e N", "N j"]
    assert [mora.source_phonemes for mora in moras] == [("b", "e~"), ("N",), ("j",)]


def test_nasal_glide_uses_standalone_when_nasal_transition_is_absent():
    moras = _moras("não", {"- na", "a N", "w", "a w"})
    assert [mora.candidates[0] for mora in moras] == ["- na", "a N", "w"]
    assert [mora.role for mora in moras] == ["nucleus", "nasal", "glide"]


def test_nasal_glide_can_use_vowel_exit_when_bank_lacks_standalone():
    moras = _moras("não", {"- na", "a N", "a w"})
    assert [mora.candidates[0] for mora in moras] == ["- na", "a N", "a w"]
    assert [mora.source_phonemes for mora in moras] == [("n", "a~"), ("N",), ("w",)]
    assert moras[-1].coverage == "approximation"


def test_original_english_bank_contextual_r_does_not_require_isolated_r_or_tap():
    # These vowel+r aliases exist in Teto English 150401; isolated r/4 do not.
    for vowel in ("i", "O", "A"):
        alias = f"{vowel} r"

        def resolve(candidates):
            return SimpleNamespace(alias=alias) if alias in candidates else None

        moras = _english_coda_units(vowel=vowel, coda=("r",), nasal=False, resolve_alias=resolve)
        assert len(moras) == 1
        assert moras[0].candidates[0] == alias
        assert moras[0].source_phonemes == ("r",)
        assert moras[0].coverage == "cvvc-transition"


def test_unrenderable_alias_is_not_reported_as_cluster_coverage():
    tiers = [("cluster-hit", ("o tr",))]
    missing = _english_resolve(tiers, lambda candidates: None)
    theoretical = _english_resolve(tiers, None)
    assert missing == (("o tr",), "approximation", 99.0)
    assert theoretical[1] == "cluster-hit"


def test_missing_vowel_to_cluster_uses_available_attack_without_duplicate_cc():
    for available in ({"- tr", "tr", "ro"}, {"tr", "t", "ro"}):
        def resolve(candidates):
            return next((SimpleNamespace(alias=alias) for alias in candidates if alias in available), None)

        moras = _english_cluster_units(
            ("t", "r"), previous_vowel="o", word_start=False, resolve_alias=resolve,
        )
        assert len(moras) == 1
        assert moras[0].candidates[0] in {"- tr", "tr"}
        assert resolve((moras[0].candidates[0],)) is not None
        assert moras[0].source_phonemes == ("t", "r")
        assert moras[0].coverage == "approximation"


def test_available_vowel_to_cluster_still_owns_cluster_without_duplicate_cc():
    available = {"o tr", "- tr", "tr"}

    def resolve(candidates):
        return next((SimpleNamespace(alias=alias) for alias in candidates if alias in available), None)

    moras = _english_cluster_units(
        ("t", "r"), previous_vowel="o", word_start=False, resolve_alias=resolve,
    )
    assert len(moras) == 1
    assert moras[0].candidates[0] == "o tr"
    assert moras[0].coverage == "cluster-hit"


def test_dedicated_nasal_vowel_preserves_glide_without_extra_nasal_tail():
    moras = _moras("não", {"- na~", "- na", "a~ w", "a N", "N w"})
    assert [mora.candidates[0] for mora in moras] == ["- na~", "a~ w"]
    assert [mora.role for mora in moras] == ["nucleus", "glide"]
    assert moras[0].coverage == "cvvc-direct"


def test_dedicated_nasal_diphthong_owns_both_vowel_and_glide():
    moras = _moras("não", {"- na~w", "- na~", "- naU", "- na"})
    assert len(moras) == 1
    assert moras[0].candidates[0] == "- na~w"
    assert moras[0].source_phonemes == ("n", "a~", "w")
    assert moras[0].coverage == "cvvc-direct"


def test_oral_glide_is_consumed_only_when_bank_has_diphthong_alias():
    native = _moras("pai", {"- paI", "- pa", "a j"})
    fallback = _moras("pai", {"- pa", "a j"})
    assert len(native) == 1
    assert native[0].source_phonemes == ("p", "a", "j")
    assert [mora.candidates[0] for mora in fallback] == ["- pa", "a j"]
    assert [mora.source_phonemes for mora in fallback] == [("p", "a"), ("j",)]


def test_unknown_inventory_preserves_oral_glide_in_plan():
    moras = phonemize("pai", voicebank_profile="english-cvvc")
    assert [mora.role for mora in moras] == ["nucleus", "glide"]
    assert moras[-1].source_phonemes == ("j",)


def test_dedicated_palatal_alias_precedes_compositional_approximation():
    daughter = _moras("filha", {"- fi", "i La", "i lja"})
    bath = _moras("banho", {"- ba", "a Ju", "a nju"})
    assert [mora.candidates[0] for mora in daughter] == ["- fi", "i La"]
    assert [mora.candidates[0] for mora in bath] == ["- ba", "a Ju"]
    assert daughter[-1].coverage == bath[-1].coverage == "cvvc-direct"


def test_english_bank_without_palatal_alias_keeps_compositional_fallback():
    daughter = _moras("filha", {"- fi", "i lja"})
    bath = _moras("banho", {"- ba", "a nju"})
    assert [mora.candidates[0] for mora in daughter] == ["- fi", "i lja"]
    assert [mora.candidates[0] for mora in bath] == ["- ba", "a nju"]
    assert daughter[-1].coverage == bath[-1].coverage == "approximation"


def test_composed_palatal_does_not_repeat_left_phone_after_real_vc():
    mine = _moras("minha", {"- mi", "i n", "n", "ja"})
    daughter = _moras("filha", {"- fi", "i l", "l", "ja"})
    assert [mora.candidates[0] for mora in mine] == ["- mi", "i n", "ja"]
    assert [mora.candidates[0] for mora in daughter] == ["- fi", "i l", "ja"]
    assert mine[-1].coverage == daughter[-1].coverage == "approximation"
    assert mine[-1].source_phonemes == ("J", "a")
    assert daughter[-1].source_phonemes == ("L", "a")


def test_composed_palatal_keeps_renderable_cc_recording():
    mine = _moras("minha", {"- mi", "i n", "n", "n j", "ja"})
    daughter = _moras("filha", {"- fi", "i l", "l", "l j", "ja"})
    assert [mora.candidates[0] for mora in mine] == ["- mi", "i n", "n j", "ja"]
    assert [mora.candidates[0] for mora in daughter] == ["- fi", "i l", "l j", "ja"]


def test_word_initial_composed_palatal_retains_left_phone_helper():
    moras = _moras("nhá", {"- n", "n", "ja"})
    assert any(mora.candidates[0] in {"- n", "n"} for mora in moras)
    assert moras[-1].candidates[0] == "ja"
