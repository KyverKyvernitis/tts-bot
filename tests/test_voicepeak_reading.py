from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.phonemizer import VOICEPEAK_READING_VERSION, prepare_voicepeak_text


def test_japanese_mode_passes_original_text_through_without_translation():
    text = "  重音テト、こんにちは！\nカタカナとひらがな？  "
    assert prepare_voicepeak_text(text) == text
    assert prepare_voicepeak_text("Português 12", mode="ja") == "Português 12"


@pytest.mark.parametrize(
    ("word", "expected"),
    (("banho", "ばにゅ"), ("filha", "ふぃりゃ"), ("não", "なんう"), ("mãe", "まんい"), ("bem", "べんい")),
)
def test_ptbr_kana_reuses_palatal_nasal_and_glide_units_once(word, expected):
    assert prepare_voicepeak_text(word, mode="ptbr-kana") == expected


def test_ptbr_kana_preserves_sentence_punctuation_and_whitespace():
    assert prepare_voicepeak_text("Olá, mãe!\nNão? Café.", mode="ptbr-kana") == "おら, まんい!\nなんう? かふぇ."


def test_ptbr_kana_does_not_drop_digits_and_reads_them_one_by_one():
    assert prepare_voicepeak_text("12, 3!", mode="ptbr-kana") == "うん どいす, とれす!"
    assert prepare_voicepeak_text("１２", mode="ptbr-kana") == "うん どいす"
    assert prepare_voicepeak_text("١٢", mode="ptbr-kana") == "うん どいす"


def test_ptbr_kana_keeps_existing_kana_and_legitimate_repeated_vowels():
    assert prepare_voicepeak_text("テトー!", mode="ptbr-kana") == "てとー!"
    assert prepare_voicepeak_text("Saara", mode="ptbr-kana") == "さあら"


def test_ptbr_kana_output_contains_only_kana_punctuation_and_spacing():
    reading = prepare_voicepeak_text("A Teto vê 25 pães, filha!", mode="ptbr-kana")
    assert re.fullmatch(r"[ぁ-ゖァ-ヺー\s,.!?]+", reading)
    assert VOICEPEAK_READING_VERSION == "ptbr-kana-v1"


@pytest.mark.parametrize("text", ("Привет", "你好", "日本語", "olá 🙂", "a + b", "æ"))
def test_ptbr_kana_rejects_unsupported_scripts_or_symbols_explicitly(text):
    with pytest.raises(ValueError, match="não é suportado no modo ptbr-kana"):
        prepare_voicepeak_text(text, mode="ptbr-kana")


@pytest.mark.parametrize("text", ("", "  ", "?!", "TTS"))
def test_ptbr_kana_rejects_unpronounceable_input(text):
    with pytest.raises(ValueError, match="pronunciável|leitura fonética"):
        prepare_voicepeak_text(text, mode="ptbr-kana")


def test_unknown_reading_mode_has_clear_error():
    with pytest.raises(ValueError, match="Use 'ja' ou 'ptbr-kana'"):
        prepare_voicepeak_text("Olá", mode="pt-br")
