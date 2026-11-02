"""Phone telemetry stays diagnostic across all native TTV voice routes."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401

WORKER_DIRECTORY = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker"
if str(WORKER_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(WORKER_DIRECTORY))

from teto_renderer import TetoRenderer


VOICES = ("kasane-teto", "hatsune-miku", "anna-nyui", "pururu-purpura")


@pytest.fixture
def unrestricted_phone(tts, monkeypatch):
    worker = tts.worker
    for key in list(os.environ):
        if key.startswith(("PHONE_WORKER_MIKU_", "PHONE_WORKER_ANNA_", "PHONE_WORKER_PURURU_")):
            monkeypatch.delenv(key)
    for prefix in ("TETO", "MIKU", "ANNA", "PURURU"):
        monkeypatch.setenv(f"PHONE_WORKER_{prefix}_ENABLED", "true")
    # A configuration written by an older release must not reinstate its
    # environmental admission policy after an update.
    monkeypatch.setenv("PHONE_WORKER_TETO_MIN_BATTERY_PERCENT", "100")
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_BATTERY_TEMP_C", "30")
    monkeypatch.setenv("PHONE_WORKER_TETO_MIN_FREE_MEMORY_MB", "999999")
    monkeypatch.setenv("PHONE_WORKER_TETO_ALLOW_LOW_BATTERY_WHEN_CHARGING", "false")
    monkeypatch.setattr(worker, "_available_memory_mb", lambda: 64)
    monkeypatch.setattr(worker, "_teto_cached_battery_snapshot", lambda: ({
        "level": 1, "temperature_c": 55.0, "charging": False,
        "status": "discharging", "source": "fixture"}, 12.0))
    monkeypatch.setattr(worker, "_core_job_runtime_snapshot", lambda: {"active_type": "maintenance_heavy"})
    calls, renderers = [], {}

    class RendererBoundary:
        PERFORMANCE_PROTOCOL = 2

        def __init__(self, *, resource_guard, voice_id="kasane-teto", **_):
            self._resource_guard = resource_guard
            self.voice_id = voice_id
            self.ready = True
            renderers[voice_id] = self

        def status(self, **_):
            return {"ok": self.ready, "available": self.ready, "ready": self.ready,
                    "ttv_languages": ["pt-BR"], "fingerprint": self.voice_id + "-bank",
                    "last_error": "" if self.ready else "voicebank ausente"}

        def fingerprint(self, **_):
            return self.voice_id + "-bank"

        def singing_fingerprint(self, **_):
            return self.voice_id + "-song" if self.ready else "unavailable"

        def _render(self, text, mode, options):
            # Exercise the production guard that runs before native synthesis,
            # replacing only the voicebank/resampler boundary with fixed bytes.
            TetoRenderer._check_resources(self)
            calls.append((self.voice_id, mode, text))
            assert worker._HEAVY_RESOURCE_LOCK.locked()
            return {"audio": (self.voice_id + ":" + mode).encode(), "audio_format": "wav",
                    "voice_id": self.voice_id, "voice_profile": "legacy",
                    "pitch_offset_semitones": options["pitch_offset_semitones"],
                    "speech_rate": options.get("speech_rate", 1.0),
                    "reading_language": options.get("reading_language", "pt-BR")}

        def synthesize(self, text, **options):
            return self._render(text, "speech", options)

        def synthesize_performance(self, text, *, score, segments, **options):
            result = self._render(text, "singing", options)
            result["ttv_performance_hash"] = worker._phone_worker_tts_policy_module().performance_request({
                "engine": "teto", "text": text, "ttv_score": score, "ttv_segments": segments})["hash"]
            return result.pop("audio"), result

    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(TetoRenderer=RendererBoundary))
    monkeypatch.setattr(worker, "_get_teto_renderer", tts.get_teto_renderer)
    monkeypatch.setattr(worker, "_teto_status", tts.teto_status)
    return SimpleNamespace(**vars(tts), renders=calls, renderers=renderers)


def test_hot_low_battery_and_low_memory_remain_visible_without_denial(unrestricted_phone):
    worker = unrestricted_phone.worker
    snapshot = worker._teto_resource_snapshot()
    assert snapshot["ok"] and snapshot["reason"] == "ok"
    assert snapshot["limits_enforced"] is False
    assert snapshot["minimum_memory_mb"] == 0
    assert snapshot["memory_available_mb"] == 64
    assert snapshot["battery_level"] == 1
    assert snapshot["battery_temperature_c"] == 55.0
    assert snapshot["active_heavy_job"] == "maintenance_heavy"
    status = worker._teto_status(force=True)
    assert {row["id"] for row in status["ttv_voices"] if row["ready"]} == set(VOICES)


@pytest.mark.parametrize("voice_id", VOICES)
@pytest.mark.parametrize("mode", ("speech", "singing"))
def test_each_voice_can_speak_and_sing_despite_phone_resource_conditions(unrestricted_phone, voice_id, mode):
    state = unrestricted_phone
    body = {"engine": "teto", "text": "a", "ttv_voice_id": voice_id,
            "ttv_language": "pt-BR", "cache_mode": "bypass"}
    if mode == "singing":
        body["ttv_score"] = {"version": 1, "track_index": 0, "duration_ms": 400,
                             "lyrics_source": "message", "notes": [
                                 {"midi_note": 60, "start_ms": 0, "duration_ms": 400, "velocity": 100}]}
    result = state.handler._task_tts_agent_synthesize(body)
    assert result["ok"] and result["ttv_voice_id"] == voice_id
    assert audio(result) == (voice_id + ":" + mode).encode()
    assert state.renders == [(voice_id, mode, "a")]
    assert state.renderers[voice_id]._resource_guard is state.worker._teto_resource_snapshot
    assert not state.worker._HEAVY_RESOURCE_LOCK.locked()


@pytest.mark.parametrize("probe", ("_available_memory_mb", "_teto_cached_battery_snapshot",
                                   "_core_job_runtime_snapshot"))
def test_failed_diagnostic_probe_does_not_deny_synthesis(unrestricted_phone, monkeypatch, probe):
    state = unrestricted_phone

    def unavailable():
        raise OSError("telemetry unavailable")

    monkeypatch.setattr(state.worker, probe, unavailable)
    assert state.worker._teto_resource_snapshot()["ok"]
    result = state.handler._task_tts_agent_synthesize({"engine": "teto", "text": "a",
        "ttv_voice_id": "pururu-purpura", "ttv_language": "pt-BR", "cache_mode": "bypass"})
    assert result["ok"] and audio(result) == b"pururu-purpura:speech"


def test_missing_voice_assets_still_have_independent_readiness(unrestricted_phone):
    state = unrestricted_phone
    state.worker._teto_status(force=True)
    state.renderers["pururu-purpura"].ready = False
    status = state.worker._teto_status(force=True)
    ready = {row["id"] for row in status["ttv_voices"] if row["ready"]}
    assert ready == set(VOICES) - {"pururu-purpura"}
    with pytest.raises(RuntimeError, match="voicebank ausente"):
        state.handler._task_tts_agent_synthesize({"engine": "teto", "text": "a",
                                               "ttv_voice_id": "pururu-purpura"})
    assert state.renders == []


@pytest.mark.parametrize("failed_snapshot", (False, True))
def test_catalog_readiness_is_independent_of_diagnostic_snapshot(unrestricted_phone, monkeypatch,
                                                              failed_snapshot):
    worker = unrestricted_phone.worker

    def snapshot():
        if failed_snapshot:
            raise OSError("diagnostic snapshot unavailable")
        return {"ok": False, "reason": "stale diagnostic state"}

    monkeypatch.setattr(worker, "_teto_resource_snapshot", snapshot)
    status = worker._teto_status(force=True)
    assert {row["id"] for row in status["ttv_voices"] if row["ready"]} == set(VOICES)
    deps = worker._teto_dependency_snapshot_fast()
    assert deps["ok"] and deps["teto_tts"]


@pytest.mark.parametrize("voice_id", VOICES)
def test_task_admission_ignores_unhealthy_diagnostic_flag(unrestricted_phone, monkeypatch, voice_id):
    state = unrestricted_phone
    state.worker._teto_status(force=True)
    # Keep the renderer boundary ready while injecting an old diagnostic
    # envelope into discovery. Admission must use the selected voice's actual
    # readiness rather than interpreting that diagnostic flag as a refusal.
    for renderer in state.renderers.values():
        renderer._resource_guard = lambda: {"ok": True}
    monkeypatch.setattr(state.worker, "_teto_resource_snapshot", lambda: {
        "ok": False, "reason": "stale diagnostic state"})
    result = state.handler._task_tts_agent_synthesize({"engine": "teto", "text": "a",
        "ttv_voice_id": voice_id, "ttv_language": "pt-BR", "cache_mode": "bypass"})
    assert result["ok"] and audio(result) == (voice_id + ":speech").encode()
    assert state.renders == [(voice_id, "speech", "a")]


def test_general_discovery_keeps_ready_ttv_despite_unhealthy_diagnostic_flag(monkeypatch):
    from test_phone_worker_telemetry import load_worker

    worker = load_worker()
    monkeypatch.setattr(worker, "_current_core_worker_profile", lambda: "turbo")
    monkeypatch.setattr(worker, "_module_import_ok", lambda *_: (True, ""))
    monkeypatch.setattr(worker, "_android_tts_status", lambda **_: {"ok": False})
    monkeypatch.setitem(sys.modules, "gtts", SimpleNamespace())
    monkeypatch.setattr(worker, "_ensure_edge_tts_runtime_dependency",
                        lambda: pytest.fail("discovery must not start a dependency install"))
    monkeypatch.setattr(worker, "_teto_status", lambda: {
        "ready": True, "enabled": True, "resources": {"ok": False, "reason": "stale diagnostic state"}})
    result = worker._turbo_dependency_snapshot()
    assert result["teto_tts"] and result["teto_enabled"]
    assert result["teto"]["resources"]["ok"] is False
