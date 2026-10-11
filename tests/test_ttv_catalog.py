from dataclasses import FrozenInstanceError

import pytest

from cogs.tts import ttv


def test_catalog_has_teto_and_miku_routes_and_is_immutable():
    assert [(voice.id, voice.name, voice.engine) for voice in ttv.VOICE_CATALOG] == [
        ("kasane-teto", "Kasane Teto", "teto"),
        ("hatsune-miku", "Hatsune Miku", "teto"),
    ]
    with pytest.raises(FrozenInstanceError):
        ttv.get_voice().name = "Outra voz"


@pytest.mark.parametrize("value", [None, "", "kasane-teto", " KASANE_TETO ", "teto", "kasane-teto-standard", "kasane-teto-english-cvvc"])
def test_legacy_bank_profiles_resolve_to_one_character(value):
    assert ttv.normalize_voice_id(value) == "kasane-teto"
    assert ttv.voice_label(value) == "Kasane Teto"


@pytest.mark.parametrize("value", ["another-character", "voicepeak", "utau", "../../teto"])
def test_unknown_explicit_voice_never_becomes_another_character(value):
    with pytest.raises(ValueError):
        ttv.normalize_voice_id(value)
    with pytest.raises(ValueError):
        ttv.resolve_preferences({"ttv_voice_id": value})


@pytest.mark.parametrize("value,expected", [(0, "+0.0"), (-0.0, "+0.0"), ("-2.0", "-2.0"), ("−1,5 semitons", "-1.5"), ("+4", "+4.0"), ("0.5 st", "+0.5")])
def test_pitch_normalizes_half_steps_without_resetting_saved_values(value, expected):
    assert ttv.normalize_pitch(value) == expected


@pytest.mark.parametrize("value", ["1.25", 4.5, -4.5, "NaN", "Infinity", "oops"])
def test_invalid_pitch_is_rejected(value):
    with pytest.raises(ValueError):
        ttv.normalize_pitch(value)


@pytest.mark.parametrize("value,expected", [(None, 1.0), ("", 1.0), ("0,85", 0.85), (1.15, 1.15), (1.07, 1.07), (0.5, 0.5), (0.74, 0.74), (0.75, 0.75), (1.25, 1.25), (1.5, 1.5)])
def test_rate_preserves_custom_valid_values(value, expected):
    assert ttv.normalize_speech_rate(value) == expected


@pytest.mark.parametrize("value", [0, 0.49, 1.51, "NaN", "-Infinity", "115%"])
def test_invalid_speed_is_rejected(value):
    with pytest.raises(ValueError):
        ttv.normalize_speech_rate(value)


def test_new_preferences_win_over_legacy_and_do_not_read_edge_controls():
    assert ttv.resolve_preferences({
        "ttv_pitch_semitones": "-1.5", "teto_pitch_semitones": "-2.0",
        "ttv_speech_rate": "1.07", "voice": "EdgeVoice", "rate": "+25%", "pitch": "+35Hz",
    }) == {"ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR", "ttv_pitch_semitones": "-1.5", "ttv_speech_rate": 1.07}


def test_missing_new_fields_use_legacy_minus_two_without_mutation():
    original = {"teto_pitch_semitones": "-2.0"}
    assert ttv.resolve_preferences(original)["ttv_pitch_semitones"] == "-2.0"
    assert original == {"teto_pitch_semitones": "-2.0"}


def test_malformed_saved_controls_recover_without_changing_the_voice():
    assert ttv.resolve_preferences({"ttv_pitch_semitones": "nan", "teto_pitch_semitones": "-2", "ttv_speech_rate": "inf"}) == {
        "ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR", "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.0,
    }
    assert ttv.resolve_preferences({}, default_pitch=-1.5)["ttv_pitch_semitones"] == "-1.5"
    assert ttv.resolve_preferences({"ttv_pitch_semitones": 0}, default_pitch=-2)["ttv_pitch_semitones"] == "+0.0"


@pytest.mark.parametrize("value", [None, "", "  ", "pt", "PT-br", "pt_BR"])
def test_portuguese_adapted_is_default_and_language_label_is_canonical(value):
    assert ttv.normalize_language(value) == "pt-BR"
    assert ttv.language_label(value) == "Português adaptado"


@pytest.mark.parametrize("value", ["en", "en-US", " EN_us "])
def test_english_language_has_its_own_canonical_selection(value):
    assert ttv.normalize_language(value) == "en-US"
    assert ttv.language_label(value) == "Inglês"
    assert ttv.resolve_preferences({"ttv_language": value})["ttv_language"] == "en-US"


@pytest.mark.parametrize("value", ["ja-JP", "en-GB", "português", "../../en", 123, True])
def test_unknown_explicit_language_never_becomes_portuguese(value):
    with pytest.raises(ValueError):
        ttv.normalize_language(value)
    with pytest.raises(ValueError):
        ttv.resolve_preferences({"ttv_language": value})


def test_language_choices_and_legacy_default_are_isolated_from_edge_language():
    assert ttv.LANGUAGE_CHOICES == (("pt-BR", "Português adaptado"), ("en-US", "Inglês"))
    legacy = {"language": "en", "android_language": "en-US", "teto_pitch_semitones": "-2.0"}
    assert ttv.resolve_preferences(legacy)["ttv_language"] == "pt-BR"
    assert legacy == {"language": "en", "android_language": "en-US", "teto_pitch_semitones": "-2.0"}
