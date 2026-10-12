"""Performance identity and admission over real worker HTTP and cache files."""
from __future__ import annotations

import base64
import copy
from http.server import ThreadingHTTPServer
import io
import json
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request
import wave

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


def score(*, pitch=60, duration=400, lyric="a"):
    return {"version": 1, "track_index": 0, "duration_ms": duration,
            "lyrics_source": "message", "notes": [
                {"midi_note": pitch, "start_ms": 0, "duration_ms": duration,
                 "velocity": 90, "lyric": lyric}]}


def segments(*, fade=True):
    return [{"text": "aaa", "fade_in": fade, "fade_out": True,
             "hold": False, "pause_after_ms": 0}]


def request(tts, **changes):
    body = {"engine": "teto", "text": "aaa", "ttv_voice_id": "kasane-teto",
            "ttv_language": "pt-BR", "ttv_pitch_semitones": -2.0,
            "ttv_speech_rate": .75, **changes}
    body["ttv_performance_hash"] = tts.worker._phone_worker_tts_policy_module().performance_request(body)["hash"]
    return body


def wav_bytes(duration_ms):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * round(44100 * duration_ms / 1000))
    return stream.getvalue()


@pytest.fixture
def performer(tts, monkeypatch):
    calls, alterations = [], {}
    tts.handler.server.max_output_bytes = 8 * 1024 * 1024

    def perform(text, **kwargs):
        assert tts.worker._HEAVY_RESOURCE_LOCK.locked()
        calls.append({"text": text, **kwargs})
        identity = tts.worker._phone_worker_tts_policy_module().performance_request({
            "engine": "teto", "text": text, "ttv_score": kwargs["score"],
            "ttv_segments": kwargs["segments"]})["hash"]
        return wav_bytes(kwargs["score"]["duration_ms"] if kwargs["score"] else 400), {
            "voice_id": "kasane-teto", "voicebank": "performance-fixture",
            "renderer_version": "performance-fixture-v1", "audio_format": "wav",
            "pitch_offset_semitones": kwargs["pitch_offset_semitones"],
            "speech_rate": kwargs["speech_rate"], "reading_language": kwargs["reading_language"],
            "ttv_performance_hash": identity, **alterations}

    renderer = SimpleNamespace(synthesize_performance=perform,
        status=lambda **kw: {"ready": True, "available": True, "ttv_languages": ["pt-BR", "en-US"]})
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: renderer)
    return SimpleNamespace(calls=calls, alterations=alterations, renderer=renderer)


@pytest.mark.parametrize("field", ["ttv_score", "ttv_segments"])
@pytest.mark.parametrize("raw", [False, True])
def test_performance_cache_identity_and_effective_controls(tts, performer, field, raw):
    body = request(tts, **{field: score() if field == "ttv_score" else segments()})
    original = copy.deepcopy(body)
    cold = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    warm = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    different = request(tts, **{field: score(pitch=64) if field == "ttv_score" else segments(fade=False)})
    changed = tts.handler._task_tts_agent_synthesize(different, raw_response=raw)
    plain_key = tts.handler._tts_agent_standard_cache_key({"engine": "teto", "text": "aaa"}, engine="teto")
    assert not cold["cache_hit"] and warm["cache_hit"] and not changed["cache_hit"]
    assert cold["cache_key"] == warm["cache_key"] != changed["cache_key"]
    assert cold["cache_key"] != plain_key[:16]
    assert audio(cold) == audio(warm) and len(performer.calls) == 2
    for result in (cold, warm):
        assert result["ttv_performance_hash"] == body["ttv_performance_hash"]
        assert result["ttv_performance_protocol"] == 1
        assert result["ttv_mode"] == ("singing" if field == "ttv_score" else "effects")
        assert result["ttv_pitch_semitones"] == (0.0 if field == "ttv_score" else -2.0)
        assert result["ttv_speech_rate"] == (1.0 if field == "ttv_score" else .75)
    assert body == original and not tts.worker._HEAVY_RESOURCE_LOCK.locked()


@pytest.mark.parametrize("field", ["ttv_score", "ttv_segments"])
def test_hash_mismatch_rejected_before_discovery_or_cache(tts, monkeypatch, field):
    body = request(tts, **{field: score() if field == "ttv_score" else segments()})
    body["ttv_performance_hash"] = "f" * 64
    monkeypatch.setattr(tts.worker, "_teto_dependency_snapshot_fast", lambda: pytest.fail("discovery happened"))
    monkeypatch.setattr(tts.handler, "_find_tts_cache_file", lambda *a: pytest.fail("cache IO happened"))
    with pytest.raises(ValueError, match="identidade da performance"):
        tts.handler._task_tts_agent_synthesize(body)
    assert not tts.calls and not tts.root.exists()


def test_score_cache_ignores_speech_controls_and_normalizes_lyric_whitespace(tts):
    body = request(tts, text="a  mor", ttv_score=score())
    same = {**body, "text": "a mor", "ttv_pitch_semitones": 4.0, "ttv_speech_rate": 1.5}
    no_personal_settings = {key: value for key, value in same.items()
                            if key not in {"ttv_pitch_semitones", "ttv_speech_rate"}}
    keys = [tts.handler._tts_agent_standard_cache_key(item, engine="teto")
            for item in (body, same, no_personal_settings)]
    assert len(set(keys)) == 1


def test_legacy_renderer_rejects_new_request_once_without_cache(tts):
    with pytest.raises(RuntimeError, match="motor sem suporte MIDI/efeitos"):
        tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score()))
    assert not tts.calls and not tts.root.exists()


@pytest.mark.parametrize("malformed", [
    {"ttv_score": {}}, {"ttv_segments": []}, {"ttv_performance_hash": "a" * 64},
    {"ttv_score": score(duration=60001)},
    {"ttv_segments": [{**segments()[0], "pause_after_ms": 60000}]},
])
def test_invalid_performance_rejected_before_provider(tts, monkeypatch, malformed):
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: pytest.fail("renderer loaded"))
    with pytest.raises(ValueError):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "aaa", **malformed})


def test_empty_lyrics_and_sixty_second_wav_keep_midi_timing_budget(tts, performer, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.5")
    body = request(tts, text="", ttv_score={**score(duration=60000, lyric=""), "lyrics_source": "vowel"},
                   ttv_speech_rate="invalid speech preference", timeout_seconds=200)
    result = tts.handler._task_tts_agent_synthesize(body)
    assert performer.calls[0]["timeout_seconds"] == 120
    assert performer.calls[0]["speech_rate"] == 1 and performer.calls[0]["pitch_offset_semitones"] == 0
    with wave.open(io.BytesIO(audio(result)), "rb") as wav:
        assert wav.getnframes() == 44100 * 60
    assert result["size"] > 5 * 1024 * 1024


@pytest.mark.parametrize("control", ["ttv_performance_hash", "pitch_offset_semitones", "reading_language"])
def test_renderer_identity_mismatch_never_publishes_cache(tts, performer, control):
    performer.alterations[control] = "other" if control != "pitch_offset_semitones" else 4.0
    body = request(tts, ttv_score=score())
    with pytest.raises(RuntimeError, match="outra performance|ajustes TTV diferentes"):
        tts.handler._task_tts_agent_synthesize(body)
    assert not tts.root.exists() and not tts.worker._HEAVY_RESOURCE_LOCK.locked()


@pytest.mark.parametrize("voice", [None, "hatsune-miku"])
@pytest.mark.parametrize("transport", ["raw", "json"])
def test_real_http_performance_requires_renderer_voice_before_cache(tts, performer, voice, transport):
    performer.alterations["voice_id"] = voice
    server = ThreadingHTTPServer(("127.0.0.1", 0), tts.worker.WorkerHandler)
    server.daemon_threads = True
    server.worker_token = "voice-identity-local-test"
    server.max_body_bytes = 1024 * 1024
    server.max_output_bytes = 8 * 1024 * 1024
    server.job_timeout = 30
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    body = request(tts, ttv_score=score())
    if transport == "json":
        body["task"] = "tts_agent_synthesize"
    path = "/tts-agent/synthesize.raw" if transport == "raw" else "/task"
    url = f"http://127.0.0.1:{server.server_port}" + path
    wire = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + server.worker_token, "Content-Type": "application/json"})
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(wire, timeout=3)
        assert error.value.code in {400, 502}
        assert "outra vocaloid TTV" in error.value.read().decode()
        assert len(performer.calls) == 1 and not tts.root.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive() and not tts.worker._HEAVY_RESOURCE_LOCK.locked()


def test_unsupported_engine_and_worldline_score_fail_without_fallback(tts, monkeypatch):
    with pytest.raises(ValueError, match="exigem a engine teto"):
        tts.handler._task_tts_agent_synthesize({"engine": "gtts", "text": "aaa", "ttv_score": score()})
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    with pytest.raises(ValueError, match="canto MIDI exige ESPER"):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "aaa", "ttv_score": score()})
    assert not tts.calls


@pytest.mark.parametrize("field", ["ttv_score", "ttv_segments"])
@pytest.mark.parametrize("transport", ["raw", "json"])
def test_real_http_performance_cold_and_warm_and_plain_compatibility(tts, performer, field, transport):
    server = ThreadingHTTPServer(("127.0.0.1", 0), tts.worker.WorkerHandler)
    server.daemon_threads = True
    server.worker_token = "performance-local-test-token"
    server.max_body_bytes = 1024 * 1024
    server.max_output_bytes = 8 * 1024 * 1024
    server.job_timeout = 30
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    body = request(tts, **{field: score() if field == "ttv_score" else segments()})
    url = f"http://127.0.0.1:{server.server_port}"

    def post(path, payload):
        request = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + server.worker_token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.headers, response.read()

    try:
        for hit in (False, True):
            if transport == "raw":
                headers, output = post("/tts-agent/synthesize.raw", body)
                assert headers["X-Core-Worker-TTV-Performance-Hash"] == body["ttv_performance_hash"]
                assert headers["X-Core-Worker-Cache-Hit"] == ("true" if hit else "false")
                assert headers["X-Core-Worker-TTV-Pitch-Semitones"] == ("0.0" if field == "ttv_score" else "-2.0")
                assert output.startswith(b"RIFF")
            else:
                _, output = post("/task", {**body, "task": "tts_agent_synthesize"})
                result = json.loads(output)
                assert result["ttv_performance_hash"] == body["ttv_performance_hash"]
                assert result["cache_hit"] is hit and base64.b64decode(result["data_b64"]).startswith(b"RIFF")
        assert len(performer.calls) == 1
        _, status_output = post("/task", {"task": "tts_agent_status"})
        assert json.loads(status_output)["ttv_performance_protocol"] == 2
        _, plain_output = post("/task", {"task": "tts_agent_synthesize", "engine": "edge", "text": "Olá."})
        assert json.loads(plain_output)["ok"] and "ttv_performance_hash" not in json.loads(plain_output)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_status_only_advertises_character_performance_when_renderer_supports_it(tts, performer, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_MIKU_ENABLED", "false")
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot", lambda: {"ok": True})
    state = tts.teto_status(force=True)
    assert state["ttv_performance_protocol"] == 2
    assert state["ttv_voices"][0]["ttv_performance_protocol"] == 1
    assert "ttv_performance_protocol" not in state["ttv_voices"][1]


def test_catalog_character_cannot_inherit_aggregate_protocol_to_read_cache(tts, monkeypatch):
    tts.deps["teto"].update({"ttv_performance_protocol": 1, "ttv_voices": [{
        "id": "kasane-teto", "ready": True, "enabled": True, "available": True,
        "languages": ["pt-BR"], "fingerprint": "legacy-renderer-without-performance"}]})
    selected = tts.worker._ttv_voice_state(tts.deps["teto"], "kasane-teto")
    assert "ttv_performance_protocol" not in selected
    monkeypatch.setattr(tts.handler, "_find_tts_cache_file", lambda *a: pytest.fail("unsupported renderer read cache"))
    with pytest.raises(RuntimeError, match="vocaloid sem suporte MIDI/efeitos"):
        tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score()))
