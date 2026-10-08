from __future__ import annotations

import sys
from pathlib import Path

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
