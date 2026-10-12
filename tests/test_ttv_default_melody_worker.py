from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_ttv_performance_worker import tts, request, wav_bytes, audio, score, segments


PRESET = {"version": 1, "preset": "ttv-demo-v1"}


@pytest.fixture
def default_performer(tts, monkeypatch):
    calls = []
    tts.handler.server.max_output_bytes = 8 * 1024 * 1024
    def perform(text, **kwargs):
        calls.append({"text": text, **kwargs})
        digest = tts.worker._phone_worker_tts_policy_module().performance_request({
            "engine": "teto", "text": text, "ttv_score": kwargs["score"],
            "ttv_segments": kwargs["segments"]})["hash"]
        return wav_bytes(4000), {"voice_id": "kasane-teto", "ttv_performance_hash": digest,
            "pitch_offset_semitones": kwargs["pitch_offset_semitones"],
            "speech_rate": kwargs["speech_rate"], "reading_language": kwargs["reading_language"],
            "voicebank_profile": "english-cvvc" if kwargs["score"] is not None else "standard"}
    fake = SimpleNamespace(PERFORMANCE_PROTOCOL=2, synthesize_performance=perform)
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: fake)
    return calls


@pytest.mark.parametrize("raw", [False, True])
def test_preset_request_is_immutable_and_cached_with_strict_neutral_controls(tts, default_performer, raw):
    body = request(tts, text="Meu pai tem pão", ttv_score=PRESET, timeout_seconds=120)
    original = deepcopy(body)
    cold = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    warm = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    assert body == original and len(default_performer) == 1
    assert not cold["cache_hit"] and warm["cache_hit"] and audio(cold) == audio(warm)
    for result in (cold, warm):
        assert result["ttv_performance_hash"] == body["ttv_performance_hash"]
        assert result["ttv_performance_protocol"] == 2
        assert result["ttv_pitch_semitones"] == 0 and result["ttv_speech_rate"] == 1
    assert default_performer[0]["timeout_seconds"] == 120


def test_unknown_preset_never_loads_renderer_or_cache(tts, monkeypatch):
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: pytest.fail("renderer loaded"))
    monkeypatch.setattr(tts.handler, "_find_tts_cache_file", lambda *_: pytest.fail("cache read"))
    with pytest.raises(ValueError):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "a",
            "ttv_score": {"version": 1, "preset": "unknown"}})


def test_preset_requires_protocol_two_but_concrete_midi_still_accepts_one(tts, monkeypatch, default_performer):
    state = {**tts.deps["teto"], "ttv_voices": [{"id": "kasane-teto", "ready": True,
        "available": True, "enabled": True, "languages": ["pt-BR"], "ttv_performance_protocol": 1}]}
    monkeypatch.setattr(tts.worker, "_teto_dependency_snapshot_fast", lambda: {**tts.deps, "teto": state})
    with pytest.raises(RuntimeError, match="sem suporte"):
        tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=PRESET))
    assert not default_performer
    result = tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score()))
    assert result["ok"] and len(default_performer) == 1


def test_worker_music_cache_tracks_english_bank_while_standard_speech_is_stable(tts, monkeypatch):
    state = {"fingerprint": "standard-bank", "singing_fingerprints": {"pt-BR": "english-one"}}
    monkeypatch.setattr(tts.worker, "_teto_status", lambda **_: state)
    musical = request(tts, ttv_score=PRESET)
    spoken = {"engine": "teto", "text": "a"}
    before = tts.handler._tts_agent_standard_cache_key(musical, engine="teto")
    normal = tts.handler._tts_agent_standard_cache_key(spoken, engine="teto")
    state["singing_fingerprints"]["pt-BR"] = "english-two"
    assert before != tts.handler._tts_agent_standard_cache_key(musical, engine="teto")
    assert normal == tts.handler._tts_agent_standard_cache_key(spoken, engine="teto")


@pytest.mark.parametrize("concrete_score", [PRESET, score(), None])
@pytest.mark.parametrize("raw", [False, True])
def test_warm_profile_describes_singing_bank_instead_of_standard_speech_bank(
    tts, default_performer, concrete_score, raw
):
    tts.deps["teto"]["voicebank_profile"] = "standard"
    body = request(tts, text="aaa", ttv_score=concrete_score,
                   ttv_segments=segments() if concrete_score is None else None)
    cold = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    warm = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    expected = "english-cvvc" if concrete_score is not None else "standard"
    assert cold["teto_voicebank_profile"] == warm["teto_voicebank_profile"] == expected
    assert warm["cache_hit"] and len(default_performer) == 1
    assert audio(cold) == audio(warm)
