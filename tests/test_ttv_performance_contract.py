"""Check the score/effect wire contract across bot and worker boundaries."""
from __future__ import annotations

from copy import deepcopy
import importlib
import importlib.util
from pathlib import Path

import pytest

from cogs.tts.performance import parse_performance_text, performance_hash


WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker"


@pytest.fixture
def contract(monkeypatch):
    monkeypatch.syspath_prepend(str(WORKER_DIR))
    prosody = importlib.import_module("teto_renderer.prosody")
    assert Path(prosody.__file__).resolve() == WORKER_DIR / "teto_renderer/prosody.py"
    spec = importlib.util.spec_from_file_location(
        "_performance_contract_policy", WORKER_DIR / "phone_worker_runtime/tts_policy.py",
    )
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return prosody, policy


def _score():
    return {
        "version": 1, "track_index": 0, "duration_ms": 1000,
        "lyrics_source": "message", "notes": [
            {"midi_note": 60, "start_ms": 0, "duration_ms": 500, "velocity": 80},
            {"midi_note": 64, "start_ms": 500, "duration_ms": 500, "velocity": 90},
        ],
    }


def _body(text, *, score=None, segments=None):
    return {
        "engine": "teto", "text": text, "ttv_score": score, "ttv_segments": segments,
        "ttv_performance_hash": performance_hash(text=text, score=score, segments=segments),
        "ttv_voice_id": "hatsune-miku", "ttv_language": "pt-BR",
        "ttv_pitch_semitones": -2.0, "ttv_speech_rate": 1.25,
    }


@pytest.mark.parametrize("source,singing", [
    ("<Olá aaa>", False), ("Olá|aaa~!", False), (r"\~ <a>", False),
    ("<a> ~", True), ("a | a", True), (r"a \~ a", True), (r"a \| a", True),
])
def test_parser_segments_validate_and_have_identical_bot_worker_hash(contract, source, singing):
    prosody, policy = contract
    text, segments = parse_performance_text(source, singing=singing)
    score = _score() if singing else None
    body = _body(text, score=score, segments=segments)
    assert " ".join(segment["text"] for segment in segments) == text
    prosody.validate_performance(text, score=score, segments=segments)
    assert prosody.performance_hash(text, score=score, segments=segments) == body["ttv_performance_hash"]
    assert policy.performance_request(body) == {
        "score": score, "segments": segments, "hash": body["ttv_performance_hash"],
    }


def test_both_hashes_collapse_whitespace_and_accept_utf8(contract):
    prosody, policy = contract
    text, segments = parse_performance_text("<Olá Miku~>")
    body = _body(text, segments=segments)
    body["text"] = "\n Olá\t Miku  "
    assert policy.performance_request(body)["hash"] == body["ttv_performance_hash"]
    assert prosody.performance_hash(body["text"], segments=segments) == performance_hash(
        text=body["text"], segments=segments,
    )


@pytest.mark.parametrize("component", ["text", "score", "segments"])
def test_worker_rejects_modified_performance_before_cache_admission(contract, component):
    _, policy = contract
    text, segments = parse_performance_text("<a a>", singing=True)
    body = _body(text, score=_score(), segments=segments)
    if component == "text":
        body["text"] = "a b"
    elif component == "score":
        body["ttv_score"]["notes"][0]["midi_note"] = 62
    else:
        body["ttv_segments"][0]["fade_in"] = False
    with pytest.raises(ValueError, match="identidade|correspondem"):
        policy.performance_request(body)


def _cache_key(policy, body):
    return policy.standard_cache_key(
        body, engine="teto", sanitize_key=policy.sanitize_cache_key,
        normalize_rate=policy.normalize_edge_rate, normalize_pitch=policy.normalize_edge_pitch,
        normalize_language=policy.normalize_gtts_language, teto_fingerprint="test-bank-fingerprint",
        teto_base_pitch="C4", teto_speech_rate=1.0,
    )


def test_effect_and_midi_cache_keys_bind_score_effects_and_character(contract):
    _, policy = contract
    text, segments = parse_performance_text("<a a>", singing=True)
    original = _body(text, score=_score(), segments=segments)
    variant = deepcopy(original)
    variant["ttv_score"]["notes"][0]["midi_note"] = 62
    variant["ttv_performance_hash"] = performance_hash(
        text=text, score=variant["ttv_score"], segments=segments,
    )
    assert _cache_key(policy, original) != _cache_key(policy, variant)
    different_voice = {**original, "ttv_voice_id": "kasane-teto"}
    assert _cache_key(policy, original) != _cache_key(policy, different_voice)
    fades = _body(text, segments=segments)
    assert _cache_key(policy, original) != _cache_key(policy, fades)


def test_singing_cache_and_controls_preserve_key_and_tempo_despite_speech_preferences(contract):
    _, policy = contract
    original = _body("a a", score=_score())
    variant = {**original, "ttv_pitch_semitones": 4.0, "ttv_speech_rate": 0.5}
    settings = policy.effective_ttv_settings(original)
    assert settings == {
        "ttv_voice_id": "hatsune-miku", "ttv_language": "pt-BR",
        "ttv_pitch_semitones": 0.0, "ttv_speech_rate": 1.0,
    }
    assert policy.effective_ttv_settings(variant) == settings
    assert _cache_key(policy, original) == _cache_key(policy, variant)


def test_plain_speech_requests_keep_the_legacy_route_without_performance_validation(contract):
    _, policy = contract
    original = "Olá " * 100
    text, segments = parse_performance_text(original)
    assert segments is None
    assert policy.performance_request({"engine": "teto", "text": text}) == {}
    assert performance_hash(text=text) == ""


def test_worker_accepts_the_same_128_character_embedded_lyric_limit_as_midi_parser(contract):
    prosody, _ = contract
    score = _score()
    score["lyrics_source"] = "midi"
    score["notes"][0]["lyric"] = "a" * 128
    score["notes"][1]["lyric"] = "~"
    prosody.validate_performance("", score=score)
