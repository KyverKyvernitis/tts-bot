from __future__ import annotations

import hashlib
import json

import pytest

from cogs.tts.performance import parse_performance_text, performance_hash


def _part(text, *, fade_in=False, fade_out=False, hold=False, pause=0):
    return {
        "text": text, "fade_in": fade_in, "fade_out": fade_out,
        "hold": hold, "pause_after_ms": pause,
    }


def test_plain_text_preserves_existing_synthesis_and_normalizes_only_whitespace():
    assert parse_performance_text("  Olá.\nEu\t sou a Miku!  ") == ("Olá. Eu sou a Miku!", None)
    assert parse_performance_text("") == ("", None)
    # The existing speech queue chunks long ordinary messages.
    assert parse_performance_text("voz " * 100) == (" ".join(["voz"] * 100), None)


def test_first_word_fades_in_and_last_word_fades_out():
    assert parse_performance_text("<aaa aaa>") == (
        "aaa aaa", [_part("aaa", fade_in=True), _part("aaa", fade_out=True)],
    )


def test_combined_effects_and_word_only_fades_keep_surrounding_words():
    assert parse_performance_text("Olá <Miku~> tudo bem") == (
        "Olá Miku tudo bem", [
            _part("Olá"), _part("Miku", fade_in=True, fade_out=True, hold=True),
            _part("tudo"), _part("bem"),
        ],
    )


@pytest.mark.parametrize("text", ["<Olá~>!", "<Olá>~!", "<Olá!~>"])
def test_effects_allow_natural_sentence_punctuation(text):
    assert parse_performance_text(text) == (
        "Olá!", [_part("Olá!", fade_in=True, fade_out=True, hold=True)],
    )


@pytest.mark.parametrize("text", ["olá | tudo bem", "olá|tudo bem", "olá |tudo bem", "olá| tudo bem"])
def test_pause_attaches_to_preceding_word_and_never_becomes_spoken(text):
    assert parse_performance_text(text) == (
        "olá tudo bem", [_part("olá", pause=180), _part("tudo"), _part("bem")],
    )


def test_singing_markers_preserve_note_and_phrase_boundaries_without_empty_segments():
    clean, segments = parse_performance_text("<a ~ ~ | mor>", singing=True)
    assert clean == "a ~ ~ | mor"
    assert segments == [
        _part("a", fade_in=True), _part("~", hold=True), _part("~", hold=True, pause=180),
        _part("|"), _part("mor", fade_out=True),
    ]
    assert " ".join(segment["text"] for segment in segments) == clean


@pytest.mark.parametrize("text", [r"a \~ b", r"a \| b"])
def test_escaped_singing_symbols_request_neutral_segments_even_without_an_effect(text):
    clean, segments = parse_performance_text(text, singing=True)
    assert clean == text.replace("\\", "")
    assert segments == [_part("a"), _part(clean.split()[1]), _part("b")]
    assert performance_hash(text=clean, segments=segments)


def test_singing_preserves_distinct_literal_and_active_hold_or_boundary_identity():
    clean, segments = parse_performance_text(r"a \~ ~ b \| | c", singing=True)
    assert clean == "a ~ ~ b | | c"
    assert segments == [
        _part("a"), _part("~"), _part("~", hold=True), _part("b"),
        _part("|", pause=180), _part("|"), _part("c"),
    ]


def test_singing_code_with_isolated_symbols_forces_neutral_marker_segments():
    clean, segments = parse_performance_text("a ` ~ | ` b", singing=True)
    assert clean == "a ` ~ | ` b"
    assert segments == [_part(token) for token in clean.split()]


@pytest.mark.parametrize("text", [
    "<@123>", "<@!123>", "<@&123>", "<#123>", "<:miku:123>", "<a:miku:123>",
    "<t:1791712345:R>", "https://example.org/a~b?q=a|b", "<https://example.org/a>",
    "www.example.org/a~", "`<aaa aaa> | bbb~`", "```<aaa> | aaa~```", "`<unfinished~",
    '"<aaa aaa> | bbb~"', r'"quote \" <aaa> | bbb~"', "“<aaa aaa> | bbb~”",
    "a < b", "a > b", "2<3", "a>=b",
])
def test_literal_content_and_comparisons_do_not_activate_effects(text):
    assert parse_performance_text(text) == (text, None)


def test_literal_protection_still_works_when_another_word_has_an_effect():
    text = '<Olá <@123> https://example.org/a~ `aaa>` "aaa|bbb"'
    clean, segments = parse_performance_text(text)
    assert clean == 'Olá <@123> https://example.org/a~ `aaa>` "aaa|bbb"'
    assert segments[0]["fade_in"] is True
    assert all(not part["fade_in"] and not part["fade_out"] and not part["hold"]
               and part["pause_after_ms"] == 0 for part in segments[1:])


def test_escaped_symbols_remain_literal_and_do_not_request_a_performance():
    assert parse_performance_text(r"\<aaa aaa\> \| aaa\~") == ("<aaa aaa> | aaa~", None)
    assert parse_performance_text(r"\<aaa <bbb>") == (
        "<aaa bbb", [_part("<aaa"), _part("bbb", fade_in=True, fade_out=True)],
    )
    assert parse_performance_text(r"c:\dados") == (r"c:\dados", None)


@pytest.mark.parametrize("text", [
    "<", ">", "<>", "<<aaa", "aaa>>", "aaa~~", "~aaa", "<aaa>bbb", "aaa>bbb>",
    "| olá", "olá |", "olá || tudo", "olá | | tudo", "<!!!", "!!!>", "~",
])
def test_malformed_effects_are_actionable_errors_instead_of_spoken_operators(text):
    with pytest.raises(ValueError, match=r"Use|efeito|fala"):
        parse_performance_text(text)


@pytest.mark.parametrize("text", ["~ a", "a | ~ b", "| a", "a |"])
def test_singing_rejects_melisma_without_a_preceding_vowel_or_empty_boundary(text):
    with pytest.raises(ValueError, match=r"Use|canto"):
        parse_performance_text(text, singing=True)


def test_effect_limits_apply_to_clean_text_and_allow_long_sung_syllables():
    clean = "a" * 180
    assert parse_performance_text("<" + clean) == (clean, [_part(clean, fade_in=True)])
    with pytest.raises(ValueError, match="180"):
        parse_performance_text("<" + "a" * 181)
    assert parse_performance_text("<" + "a" * 181, singing=True)[0] == "a" * 181
    assert len(parse_performance_text("<a " + "a " * 127, singing=True)[1]) == 128
    with pytest.raises(ValueError, match="128"):
        parse_performance_text("<a " + "a " * 128, singing=True)


def test_plain_singing_text_needs_no_effect_segments():
    assert parse_performance_text("a-mor", singing=True) == ("a-mor", None)


def test_performance_hash_is_exact_canonical_utf8_and_binds_every_effect():
    clean, segments = parse_performance_text("<Olá | Miku>")
    payload = {"version": 1, "text": clean, "score": None, "segments": segments}
    expected = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    assert performance_hash(text=clean, segments=segments) == expected
    assert performance_hash(text="  Olá\n Miku  ", segments=segments) == expected
    changed = [dict(segment) for segment in segments]
    changed[0]["fade_in"] = False
    assert performance_hash(text=clean, segments=changed) != expected
    assert performance_hash(text=clean, segments=segments, score={"notes": [60]}) != expected


def test_plain_text_hash_remains_empty_and_dictionary_order_is_irrelevant():
    assert performance_hash(text="olá") == ""
    assert performance_hash(text="olá", segments=[]) == ""
    assert performance_hash(text="a", score={"version": 1, "notes": [60]}) == performance_hash(
        text="a", score={"notes": [60], "version": 1},
    )


def test_invalid_text_type_is_an_actionable_error():
    with pytest.raises(ValueError, match="texto"):
        parse_performance_text(None)
