"""UTAU migration, voice identity and deadlines through the real phone facade."""
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


@pytest.mark.parametrize("backend", ("utau", "voicepeak", " VOICEPEAK "))
def test_retired_backend_setting_selects_local_utau_only(tts, monkeypatch, backend):
    created = []

    def renderer(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", backend)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_COMMAND", "/must/never/be/executed")
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", "http://must-never-connect.invalid")
    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(TetoRenderer=renderer))
    monkeypatch.setattr(tts.worker, "_phone_worker_tts_providers_module",
                        lambda: pytest.fail("retired provider selected"))
    first = tts.get_teto_renderer()
    assert tts.worker._teto_backend() == "utau"
    assert first.resource_guard is tts.worker._teto_resource_snapshot
    assert tts.get_teto_renderer() is first and len(created) == 1


def test_unknown_backend_fails_before_loading_a_renderer(tts, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "typo")
    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(
        TetoRenderer=lambda **kw: pytest.fail("renderer loaded for invalid backend")))
    with pytest.raises(ValueError, match="deve ser utau"):
        tts.get_teto_renderer()


def test_legacy_remote_settings_do_not_bypass_phone_resource_guard(tts, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "voicepeak")
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", "http://retired.invalid")
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot",
                        lambda: {"ok": False, "reason": "memória insuficiente"})
    monkeypatch.setattr(tts.worker, "_get_teto_renderer",
                        lambda: SimpleNamespace(status=lambda **kw: {"ready": True}))
    status = tts.teto_status(force=True)
    assert status["backend"] == "utau" and status["ready"]
    assert status["resources"] == {"ok": False, "reason": "memória insuficiente"}


@pytest.mark.parametrize("engine", ("teto", "kasane_teto", "utau"))
def test_missing_teto_rejects_without_other_provider_discovery(tts, monkeypatch, engine):
    tts.deps["teto"].update(ready=False, last_error="resampler ausente")
    monkeypatch.setattr(tts.worker, "_turbo_dependency_snapshot",
                        lambda: pytest.fail("another voice was probed"))
    with pytest.raises(RuntimeError, match="Teto indisponível: resampler ausente"):
        tts.handler._task_tts_agent_synthesize(
            {"engine": engine, "fallback_engine": "gtts", "text": "Olá."})
    assert not tts.calls


def test_utau_pitch_and_voicebank_revision_keep_distinct_audio_cache(tts, monkeypatch):
    renders = []

    def render(text, **kwargs):
        renders.append(kwargs)
        pitch = kwargs["pitch_offset_semitones"]
        return {"audio": str(pitch).encode(), "audio_format": "wav",
                "pitch_offset_semitones": pitch, "voicebank_profile": "english-cvvc"}

    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "voicepeak")
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=render))
    body = {"engine": "teto", "text": "Olá.", "teto_pitch_semitones": -1}
    cold = tts.handler._task_tts_agent_synthesize(body)
    warm = tts.handler._task_tts_agent_synthesize(body)
    other_pitch = tts.handler._task_tts_agent_synthesize({**body, "teto_pitch_semitones": 2})
    assert not cold["cache_hit"] and warm["cache_hit"] and not other_pitch["cache_hit"]
    assert audio(cold) == audio(warm) == b"-1.0" and audio(other_pitch) == b"2.0"
    assert [item["pitch_offset_semitones"] for item in renders] == [-1.0, 2.0]
    assert warm["teto_backend"] == "utau" and warm["teto_pitch_mode"] == "utau-semitones"
    assert warm["teto_pitch_offset_semitones"] == -1.0
    tts.deps["teto"]["fingerprint"] = "another-bank"
    changed = tts.handler._task_tts_agent_synthesize(body)
    assert not changed["cache_hit"] and len(renders) == 3


@pytest.mark.parametrize("preflight_seconds", (1.5, 2.5))
def test_explicit_teto_preflight_consumes_request_deadline(tts, monkeypatch, preflight_seconds):
    clock = [100.0]
    tts.worker.time.monotonic = lambda: clock[0]
    renders = []

    def status():
        clock[0] += preflight_seconds
        return dict(tts.deps["teto"])

    def render(text, **kwargs):
        renders.append(kwargs["timeout_seconds"])
        return {"audio": b"wave", "audio_format": "wav"}

    monkeypatch.setattr(tts.worker, "_teto_status", status)
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=render))
    body = {"engine": "teto", "text": "Olá.", "timeout_seconds": 2, "cache_mode": "bypass"}
    if preflight_seconds < 2:
        assert tts.handler._task_tts_agent_synthesize(body)["ok"]
        assert renders == [0.5]
    else:
        with pytest.raises(TimeoutError, match="prazo total"):
            tts.handler._task_tts_agent_synthesize(body)
        assert not renders
        assert tts.worker._TTS_AGENT_ACTIVE == 0 and tts.worker._TTS_AGENT_FAILED == 1


@pytest.mark.parametrize("scenario", ("prebuild-failed", "prebuild-disabled", "other-voice", "teto-ready"))
def test_direct_playback_requires_teto_audio_before_forwarding(tts, monkeypatch, scenario):
    monkeypatch.setenv("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_WITH_TTS_AGENT",
                       "false" if scenario == "prebuild-disabled" else "true")
    tts.worker.time.perf_counter = lambda: 100.0
    monkeypatch.setattr(tts.worker, "_voice_agent_prune_transfers", lambda: {})
    monkeypatch.setattr(tts.worker, "_voice_agent_set_connection", lambda *a, **kw: dict(kw))
    monkeypatch.setattr(tts.worker, "_music_agent_snapshot", lambda: {})
    monkeypatch.setattr(tts.worker, "_tts_agent_snapshot", lambda: {})
    monkeypatch.setattr(tts.worker, "_voice_agent_snapshot", lambda **kw: {})
    forwarded = []
    monkeypatch.setattr(tts.handler, "_task_music_agent_proxy",
                        lambda body: forwarded.append(body) or {"ok": True, "engine": "teto"})

    def synthesize(body):
        if scenario == "prebuild-failed":
            raise RuntimeError("Teto indisponível")
        return {"ok": True, "data_b64": "d2F2ZQ==", "audio_format": "wav",
                "selected_engine": "gtts" if scenario == "other-voice" else "teto"}

    monkeypatch.setattr(tts.handler, "_task_tts_agent_synthesize", synthesize)
    body = {"guild_id": 1, "channel_id": 2, "engine": "teto", "text": "Olá."}
    if scenario == "teto-ready":
        assert tts.handler._task_voice_agent_play_tts(body)["ok"]
        assert len(forwarded) == 1 and forwarded[0]["audio_b64"] == "d2F2ZQ=="
    else:
        with pytest.raises(RuntimeError, match="Teto"):
            tts.handler._task_voice_agent_play_tts(body)
        assert not forwarded
