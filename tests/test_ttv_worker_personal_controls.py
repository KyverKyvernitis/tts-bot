"""Personal TTV controls through the actual worker admission/cache/provider path."""
import io
import os
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


def install_renderer(tts, monkeypatch):
    rendered = []

    def synthesize(text, **kwargs):
        rate = kwargs.get("speech_rate", float(os.getenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.0")))
        pitch = kwargs["pitch_offset_semitones"]
        rendered.append({"rate": rate, "pitch": pitch, "kwargs": kwargs})
        return {"audio": f"{text}|{rate}|{pitch}".encode(), "audio_format": "wav",
                "speech_rate": rate, "pitch_offset_semitones": pitch}

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=synthesize))
    return rendered


@pytest.mark.parametrize("raw", [False, True])
def test_personal_rate_pitch_and_hit_metadata_are_isolated_from_globals(tts, monkeypatch, raw):
    monkeypatch.setenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.25")
    before = dict(os.environ)
    rendered = install_renderer(tts, monkeypatch)
    body = {"engine": "teto", "text": "Olá, Teto.", "ttv_voice_id": "kasane-teto",
            "ttv_pitch_semitones": -1.5, "teto_pitch_semitones": 3,
            "ttv_speech_rate": .85, "cache_key": "c" * 64}
    cold = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    warm = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    faster = tts.handler._task_tts_agent_synthesize({**body, "ttv_speech_rate": 1.15}, raw_response=raw)
    higher = tts.handler._task_tts_agent_synthesize({**body, "ttv_pitch_semitones": 2}, raw_response=raw)
    assert not cold["cache_hit"] and warm["cache_hit"]
    assert not faster["cache_hit"] and not higher["cache_hit"]
    assert len({cold["cache_key"], faster["cache_key"], higher["cache_key"]}) == 3
    assert audio(warm) == audio(cold) != audio(faster)
    assert [(item["rate"], item["pitch"]) for item in rendered] == [(.85, -1.5), (1.15, -1.5), (.85, 2.0)]
    for result in (cold, warm):
        assert result["ttv_voice_id"] == "kasane-teto"
        assert result["ttv_pitch_semitones"] == result["teto_pitch_offset_semitones"] == -1.5
        assert result["ttv_speech_rate"] == result["teto_speech_rate"] == .85
    assert os.environ == before


def test_legacy_request_uses_global_rate_and_old_pitch_then_personal_request_does_not(tts, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.25")
    rendered = install_renderer(tts, monkeypatch)
    body = {"engine": "teto", "text": "Velocidade antiga.", "teto_pitch_semitones": -2}
    old = tts.handler._task_tts_agent_synthesize(body)
    old_hit = tts.handler._task_tts_agent_synthesize(body)
    new = tts.handler._task_tts_agent_synthesize({**body, "ttv_voice_id": "kasane-teto", "ttv_speech_rate": 1})
    assert [(item["rate"], item["pitch"]) for item in rendered] == [(1.25, -2.0), (1.0, -2.0)]
    assert "speech_rate" not in rendered[0]["kwargs"]
    assert rendered[1]["kwargs"]["speech_rate"] == 1.0
    assert old_hit["cache_hit"] and old_hit["ttv_speech_rate"] == old["ttv_speech_rate"] == 1.25
    assert new["ttv_speech_rate"] == 1.0 and new["cache_key"] != old["cache_key"]


@pytest.mark.parametrize("voice", ["hatsune-miku", "", None, "kasane-teto-english-cvvc"])
@pytest.mark.parametrize("cache_mode", ["prefer", "bypass"])
def test_unknown_character_fails_before_discovery_cache_or_any_provider(tts, monkeypatch, voice, cache_mode):
    monkeypatch.setattr(tts.worker, "_teto_dependency_snapshot_fast", lambda: pytest.fail("voice was probed"))
    monkeypatch.setattr(tts.handler, "_find_tts_cache_file", lambda *a: pytest.fail("cache was read"))
    with pytest.raises(ValueError, match="vocaloid TTV desconhecida"):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "Oi.", "ttv_voice_id": voice,
                                              "fallback_engine": "gtts", "cache_mode": cache_mode})
    assert not tts.calls


@pytest.mark.parametrize("rate", [None, "", "invalid", True, float("nan"), float("inf"), 10 ** 400, .74, 1.51])
def test_invalid_personal_rate_is_not_replaced_silently(tts, monkeypatch, rate):
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: pytest.fail("invalid rate rendered"))
    with pytest.raises(ValueError, match="velocidade TTV"):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "Oi.", "ttv_speech_rate": rate})


def test_normalized_controls_share_cache_and_voice_implementation_separates_it(tts, monkeypatch):
    body = {"text": "Mesma voz.", "ttv_voice_id": "kasane-teto",
            "ttv_pitch_semitones": "-1,5", "ttv_speech_rate": "0.850"}
    key = tts.handler._tts_agent_standard_cache_key(body, engine="teto")
    same = tts.handler._tts_agent_standard_cache_key(
        {**body, "ttv_pitch_semitones": -1.5, "ttv_speech_rate": .85}, engine="teto")
    assert key == same
    tts.deps["teto"]["fingerprint"] = "updated-source-and-wrapper"
    assert tts.handler._tts_agent_standard_cache_key(body, engine="teto") != key


@pytest.mark.parametrize("state", ["ready", "disabled", "broken", "resource-blocked"])
def test_catalog_health_reports_actual_renderer_and_resource_readiness(tts, monkeypatch, state):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "false" if state == "disabled" else "true")
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(
        status=lambda **kw: {"ready": state != "broken", "available": state != "broken"}))
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot", lambda: {"ok": state != "resource-blocked"})
    status = tts.teto_status(force=True)
    assert status["ttv_personal_settings"] is True
    assert status["ttv_voices"] == [{"id": "kasane-teto", "name": "Kasane Teto",
                                    "ready": state == "ready",
                                    "available": state in {"ready", "resource-blocked"},
                                    "supports_personal_rate": True}]


def test_raw_transport_preserves_effective_control_headers(tts):
    headers = {}
    handler = SimpleNamespace(send_response=lambda code: None,
                              send_header=lambda key, value: headers.update({key: value}),
                              end_headers=lambda: None, wfile=io.BytesIO())
    tts.worker._audio_response(handler, 200, b"wav", {
        "audio_format": "wav", "ttv_voice_id": "kasane-teto",
        "ttv_pitch_semitones": -2.0, "ttv_speech_rate": .85})
    assert headers["X-Core-Worker-TTV-Voice-Id"] == "kasane-teto"
    assert headers["X-Core-Worker-TTV-Pitch-Semitones"] == "-2.0"
    assert headers["X-Core-Worker-TTV-Speech-Rate"] == "0.85"
    assert handler.wfile.getvalue() == b"wav"


def test_prebuilt_direct_audio_cannot_bypass_character_validation(tts, monkeypatch):
    monkeypatch.setattr(tts.worker, "_voice_agent_set_connection", lambda *a, **kw: pytest.fail("voice lease changed"))
    monkeypatch.setattr(tts.handler, "_task_music_agent_proxy", lambda body: pytest.fail("unknown character played"))
    with pytest.raises(ValueError, match="vocaloid TTV desconhecida"):
        tts.handler._task_voice_agent_play_tts({"engine": "teto", "ttv_voice_id": "hatsune-miku",
                                              "audio_b64": "d2F2ZQ==", "guild_id": 1, "channel_id": 2})
