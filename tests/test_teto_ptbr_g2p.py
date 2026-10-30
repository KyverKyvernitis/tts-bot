from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.ptbr_g2p import g2p_word


def test_g2p_resolves_portuguese_before_japanese_alias_mapping():
    assert g2p_word("casa").phonemes == ("k", "a", "z", "a")
    assert g2p_word("chave").phonemes == ("S", "a", "v", "i")
    assert g2p_word("carro").phonemes == ("k", "a", "R", "u")
    assert g2p_word("banho").phonemes == ("b", "a", "J", "u")
    assert g2p_word("filha").phonemes == ("f", "i", "L", "a")
    assert g2p_word("cidade").phonemes == ("s", "i", "d", "a", "dZ", "i")


def test_g2p_nasalizes_coda_m_n_and_keeps_intervocalic_n_consonantal():
    assert g2p_word("bom").phonemes == ("b", "o~")
    assert g2p_word("bem").phonemes == ("b", "e~", "j")
    assert g2p_word("grande").phonemes[:3] == ("g", "r", "a~")
    assert g2p_word("banana").phonemes == ("b", "a", "n", "a", "n", "a")


def test_g2p_diphthong_vs_hiatus_and_explicit_stress():
    assert g2p_word("pai").phonemes == ("p", "a", "j")
    assert g2p_word("não").phonemes == ("n", "a~", "w")
    assert g2p_word("saída").phonemes == ("s", "a", "i", "d", "a")
    assert g2p_word("saída").syllables[1].stressed
    assert g2p_word("saúde").syllables[1].stressed


def test_final_nasal_e_diphthong_keeps_glide_before_plural_s():
    assert g2p_word("também").phonemes == ("t", "a~", "b", "e~", "j")
    assert g2p_word("armazém").phonemes[-2:] == ("e~", "j")
    assert g2p_word("bens").phonemes == ("b", "e~", "j", "s")
    assert g2p_word("homens").phonemes[-3:] == ("e~", "j", "s")
    assert g2p_word("também").syllables[-1].coda == ("j",)
    assert g2p_word("bens").syllables[-1].coda == ("j", "s")


def test_final_nasal_glide_rule_does_not_guess_unstressed_verb_endings():
    assert g2p_word("falem").phonemes == ("f", "a", "l", "e~")
    assert g2p_word("fazem").phonemes == ("f", "a", "z", "e~")


def test_g2p_syllabification_preserves_legal_onset_clusters():
    brasil = g2p_word("Brasil")
    problema = g2p_word("problema")
    trabalho = g2p_word("trabalho")
    assert brasil.syllables[0].onset == ("b", "r")
    assert problema.syllables[0].onset == ("p", "r")
    assert problema.syllables[1].onset == ("b", "l")
    assert trabalho.syllables[0].onset == ("t", "r")


def test_deaccented_function_word_reduction_is_explicit_input_to_g2p():
    assert g2p_word("de").phonemes == ("dZ", "e")
    assert g2p_word("de", deaccented=True).phonemes == ("dZ", "i")
    assert not any(syllable.stressed for syllable in g2p_word("de", deaccented=True).syllables)


def test_tilde_participates_in_stress_when_it_is_the_only_written_mark():
    assert g2p_word("irmã").syllables[-1].stressed
    assert g2p_word("irmão").syllables[-1].stressed
    # An earlier acute/circumflex still wins when the word has both marks.
    assert g2p_word("órgão").syllables[0].stressed
    assert not g2p_word("órgão").syllables[-1].stressed


@pytest.mark.parametrize(
    ("word", "phonemes", "syllables", "stress", "glide_syllable"),
    [
        ("água", ("a", "g", "w", "a"), 2, 0, 1),
        ("agua", ("a", "g", "w", "a"), 2, 0, 1),
        ("quase", ("k", "w", "a", "z", "i"), 2, 0, 0),
        ("tranquilo", ("t", "r", "a~", "k", "w", "i", "l", "u"), 3, 1, 1),
        ("linguiça", ("l", "i~", "g", "w", "i", "s", "a"), 3, 1, 1),
        ("quatro", ("k", "w", "a", "t", "r", "u"), 2, 0, 0),
    ],
)
def test_velar_glides_do_not_add_syllables_or_displace_stress(
    word, phonemes, syllables, stress, glide_syllable,
):
    result = g2p_word(word)
    assert result.phonemes == phonemes
    assert len(result.syllables) == syllables
    assert [index for index, syllable in enumerate(result.syllables) if syllable.stressed] == [stress]
    assert result.syllables[glide_syllable].onset[-1] == "w"
    assert all("w" not in syllable.coda for syllable in result.syllables)


@pytest.mark.parametrize("word", ["tranquila", "tranquilos", "tranquilas", "linguiças", "tranqüilo", "lingüiça"])
def test_modern_inflections_and_explicit_diaeresis_keep_pronounced_u(word):
    result = g2p_word(word)
    assert "w" in result.phonemes
    assert len(result.syllables) == 3
    assert result.syllables[1].stressed


@pytest.mark.parametrize(
    ("word", "count", "stress"),
    [("queijo", 2, 0), ("guerra", 2, 0), ("guitarra", 3, 1), ("guia", 2, 0)],
)
def test_silent_u_does_not_become_a_glide_or_change_written_stress(word, count, stress):
    result = g2p_word(word)
    assert "w" not in result.phonemes
    assert len(result.syllables) == count
    assert result.syllables[stress].stressed


@pytest.mark.parametrize(
    ("word", "phonemes"),
    [
        ("táxi", ("t", "a", "k", "s", "i")),
        ("fixo", ("f", "i", "k", "s", "u")),
        ("tóxico", ("t", "O", "k", "s", "i", "k", "u")),
        ("próximo", ("p", "r", "O", "s", "i", "m", "u")),
    ],
)
def test_lexical_x_exceptions_preserve_accent_and_do_not_guess_other_words(word, phonemes):
    assert g2p_word(word).phonemes == phonemes
    assert g2p_word(unicodedata.normalize("NFD", word.upper())).phonemes == phonemes
    assert g2p_word(word).syllables[0].stressed
    assert "S" in g2p_word("caixa").phonemes
    assert g2p_word("xícara").phonemes[0] == "S"
    assert g2p_word("exame").phonemes[1] == "z"


@pytest.mark.parametrize("word", ["muito", "muita", "muitos", "muitas"])
def test_muito_family_keeps_nasal_diphthong_and_two_syllables(word):
    result = g2p_word(word)
    assert result.phonemes[:3] == ("m", "u~", "j")
    assert len(result.syllables) == 2
    assert result.syllables[0].coda == ("j",)
    assert result.syllables[0].stressed


def test_lexicon_keeps_pode_pode_accent_distinction_and_explicit_deaccenting():
    assert g2p_word("pode").syllables[0].vowel == "O"
    assert g2p_word("pôde").syllables[0].vowel == "o"
    assert not any(syllable.stressed for syllable in g2p_word("muito", deaccented=True).syllables)


def test_vps_and_phone_share_identical_portuguese_pronunciation_source():
    shared = WORKER_DIR.parents[2] / "osaka_cd" / "phonetics" / "ptbr_g2p.py"
    assert shared.read_bytes() == (WORKER_DIR / "teto_renderer" / "ptbr_g2p.py").read_bytes()


@pytest.mark.parametrize("word", ["água", "quase", "tranquilo", "linguiça", "táxi", "fixo", "tóxico", "próximo", "muito"])
def test_pronunciation_changes_keep_vps_and_worker_nucleus_planning_in_sync(word):
    from osaka_cd.phonetics.phonemizer import phonemize as shared_phonemize
    from teto_renderer.phonemizer import phonemize

    for profile in ("standard", "english-cvvc"):
        shared = shared_phonemize(word, voicebank_profile=profile, reading_language="pt-BR")
        worker = phonemize(word, voicebank_profile=profile, reading_language="pt-BR")
        assert [mora.source_phonemes for mora in shared] == [mora.source_phonemes for mora in worker]
        assert sum(mora.role == "nucleus" for mora in worker) == len(g2p_word(word).syllables)
