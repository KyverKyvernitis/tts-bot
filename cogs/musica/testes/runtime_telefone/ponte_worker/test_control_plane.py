from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"


def _load_worker():
    spec = importlib.util.spec_from_file_location("music_phone_control_plane_test", PHONE)
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


@pytest.fixture
def control(monkeypatch):
    for key in (
        "CORE_WORKER_VPS_URL", "CORE_WORKER_BASE_URL", "CORE_WORKER_TOKEN", "CORE_WORKER_ID",
        "CORE_WORKER_WORKER_ID", "CORE_WORKER_ENDPOINT", "PHONE_WORKER_ENDPOINT",
        "CORE_WORKER_ROLES", "CORE_WORKER_CAPABILITIES",
    ):
        monkeypatch.delenv(key, raising=False)
    worker = _load_worker()
    monkeypatch.setattr(worker, "_system_status", lambda: {
        "ok": True, "pid": 123, "ffmpeg": True, "ffprobe": False,
        "scripts": {"complete": True}, "boot": {"ok": False},
        "supervisor": {"supervisor_ok": True},
    })
    monkeypatch.setattr(worker, "_music_agent_snapshot", lambda: {"available": False})
    monkeypatch.setattr(worker, "_battery_snapshot", lambda: {"available": True, "level": 42})
    monkeypatch.setattr(worker, "_network_snapshot", lambda: {"type": "unknown", "source": "inferred"})
    monkeypatch.setattr(worker, "_default_worker_name", lambda: "phone fixture")
    monkeypatch.setattr(worker, "_current_core_worker_profile", lambda: "midia")
    monkeypatch.setattr(worker, "_core_worker_profile_label", lambda profile: "label-" + profile)
    monkeypatch.setattr(worker, "_current_core_worker_roles_and_capabilities", lambda: (["phone-worker"], ["status"]))
    monkeypatch.setattr(worker, "_phone_worker_safe_mode_enabled", lambda: False)
    monkeypatch.setattr(worker, "_supported_core_worker_job_types", lambda: ["ping", "worker_update"])
    monkeypatch.setattr(worker, "_phone_worker_source_hash", lambda: "a" * 64)
    monkeypatch.setattr(worker, "_bootstrap_updater_snapshot", lambda: {"state": "idle"})
    monkeypatch.setattr(worker, "_EFFECTIVE_HTTP_PORT", 8768)
    monkeypatch.setattr(worker, "_DIRECT_HTTP_STATE", "listening_recovery_after_conflict")
    return worker


def test_payload_does_not_mutate_role_lists_when_music_changes(control, monkeypatch):
    roles, capabilities = ["phone-worker"], ["status"]
    monkeypatch.setattr(control, "_current_core_worker_roles_and_capabilities", lambda: (roles, capabilities))
    monkeypatch.setattr(control, "_music_agent_snapshot", lambda: {"available": True})
    first = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert "music" in first["roles"] and "ffmpeg" in first["capabilities"]
    assert roles == ["phone-worker"] and capabilities == ["status"]
    monkeypatch.setattr(control, "_music_agent_snapshot", lambda: {"available": False})
    assert "music" not in control._core_worker_payload(host="127.0.0.1", port=8766)["roles"]


@pytest.mark.parametrize("safe", [True, False])
def test_payload_music_policy_limits_and_job_state_stay_live(control, monkeypatch, safe):
    monkeypatch.setattr(control, "_phone_worker_safe_mode_enabled", lambda: safe)
    monkeypatch.setattr(control, "_current_core_worker_profile", lambda: "turbo")
    monkeypatch.setattr(control, "_current_core_worker_roles_and_capabilities", lambda: (
        ["music", "music-agent"] + [f"role-{i}" for i in range(20)],
        ["music-voice"] + [f"cap-{i}" for i in range(30)],
    ))
    control._set_core_job_active({"job_id": "active", "type": "worker_update"})
    monkeypatch.setattr(control, "_PENDING_CORE_JOB_RESULTS", {"one": {}, "two": {}})
    result = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert len(result["roles"]) == 16 and len(result["capabilities"]) == 24
    assert any(str(value).startswith("music") for value in result["roles"]) is (not safe)
    assert result["status"]["core_worker_jobs"]["active_job_id"] == "active"
    assert result["status"]["core_worker_jobs"]["pending_results"] == 2


def test_payload_never_advertises_legacy_lavalink(control, monkeypatch):
    assert not hasattr(control, "_music_node_snapshot")
    monkeypatch.setattr(control, "_music_agent_snapshot", lambda: {"available": True})
    result = control._core_worker_payload(host="127.0.0.1", port=8766)
    node = result["status"]["music_node"]
    assert node["state"] == "disabled" and node["deprecated"] is True
    assert node["reason"] == "playback_owned_by_music_agent"
    assert "music-node" not in result["roles"]
    assert "music-lavalink" not in result["capabilities"]


def test_turbo_profile_keeps_music_capabilities_inside_wire_limit(control, monkeypatch):
    monkeypatch.setattr(control, "_current_core_worker_profile", lambda: "turbo")
    monkeypatch.setattr(control, "_current_core_worker_roles_and_capabilities", lambda: (
        list(control.CORE_WORKER_PROFILE_PRESETS["turbo"]["roles"]),
        list(control.CORE_WORKER_PROFILE_PRESETS["turbo"]["capabilities"]),
    ))
    monkeypatch.setattr(control, "_music_agent_snapshot", lambda: {"available": True})
    result = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert len(result["roles"]) <= 16 and len(result["capabilities"]) <= 24
    assert {"music", "music-agent", "music-ytdlp"} <= set(result["roles"])
    assert {"music", "music-agent", "music-voice", "music-ytdlp", "music-ytdlp-resolve"} <= set(result["capabilities"])


def test_turbo_profile_does_not_claim_playback_without_music_agent(control, monkeypatch):
    monkeypatch.setattr(control, "_current_core_worker_profile", lambda: "turbo")
    monkeypatch.setattr(control, "_current_core_worker_roles_and_capabilities", lambda: (
        list(control.CORE_WORKER_PROFILE_PRESETS["turbo"]["roles"]),
        list(control.CORE_WORKER_PROFILE_PRESETS["turbo"]["capabilities"]),
    ))
    monkeypatch.setattr(control, "_music_agent_snapshot", lambda: {"available": False})
    result = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert "music" not in result["roles"]
    assert "music" not in result["capabilities"]
    assert "music-ytdlp-resolve" in result["capabilities"]


def test_music_agent_snapshot_failure_is_isolated(control, monkeypatch):
    def fail():
        raise PermissionError("optional music probe unavailable")
    monkeypatch.setattr(control, "_music_agent_snapshot", fail)
    result = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert result["health"]["control_plane_alive"]
    assert "error" in result["status"]["music_agent"]


def test_music_control_plane_extension_does_not_mutate_input():
    path = ROOT / "cogs/musica/runtime_telefone/ponte_worker/control_plane.py"
    spec = importlib.util.spec_from_file_location("music_control_extension_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = {"roles": ["phone-worker"], "capabilities": ["status"], "status": {}, "safe_mode": False, "profile": "turbo"}
    before = copy.deepcopy(payload)
    result = module.estender_payload(payload, music_node={"state": "disabled"}, music_agent={"available": True})
    assert payload == before
    assert "music" in result["roles"] and "music-agent" in result["capabilities"]
