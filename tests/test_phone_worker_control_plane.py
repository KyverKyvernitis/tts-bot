"""Control-plane contracts, with optional services and all sends controlled locally."""
import importlib.util
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"


def load_worker(path=PHONE):
    spec = importlib.util.spec_from_file_location("phone_control_plane_test", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


@pytest.fixture
def control(monkeypatch):
    for key in ["CORE_WORKER_VPS_URL", "CORE_WORKER_BASE_URL", "CORE_WORKER_TOKEN", "CORE_WORKER_ID",
                "CORE_WORKER_WORKER_ID", "CORE_WORKER_HEARTBEAT_ENABLED", "CORE_WORKER_JOBS_ENABLED",
                "CORE_WORKER_ENDPOINT", "PHONE_WORKER_ENDPOINT", "CORE_WORKER_NAME", "CORE_WORKER_ROLES",
                "CORE_WORKER_CAPABILITIES", "PHONE_WORKER_CONFIG_SCHEMA", "PHONE_WORKER_SELF_UPDATE_ENABLED",
                "PHONE_WORKER_BOOTSTRAP_UPDATE_ENABLED"]:
        monkeypatch.delenv(key, raising=False)
    worker = load_worker()
    monkeypatch.setattr(worker, "_system_status", lambda: {"ok": True, "pid": 123, "ffmpeg": True,
        "ffprobe": False, "scripts": {"complete": True}, "boot": {"ok": False}, "supervisor": {"supervisor_ok": True}})
    monkeypatch.setattr(worker, "_music_node_snapshot", lambda: {"ok": False, "online": False})
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


def configure(monkeypatch):
    for key, value in {"CORE_WORKER_BASE_URL": " https://vps.invalid/base/// ",
                       "CORE_WORKER_WORKER_ID": " parent ", "CORE_WORKER_TOKEN": " private-token "}.items():
        monkeypatch.setenv(key, value)


def test_auth_aliases_normalization_and_live_configuration(control, monkeypatch):
    assert control._core_worker_auth_parts() == ("", "", "")
    configure(monkeypatch)
    assert control._core_worker_auth_parts() == ("https://vps.invalid/base", "private-token", "parent")
    assert control._heartbeat_configured() and control._core_worker_jobs_configured()
    monkeypatch.setenv("CORE_WORKER_VPS_URL", "http://preferred.invalid/")
    monkeypatch.setenv("CORE_WORKER_ID", "primary")
    assert control._core_worker_auth_parts() == ("http://preferred.invalid", "private-token", "primary")
    monkeypatch.setenv("CORE_WORKER_TOKEN", " changed ")
    assert control._core_worker_auth_parts()[1] == "changed"


@pytest.mark.parametrize("missing", ["CORE_WORKER_BASE_URL", "CORE_WORKER_WORKER_ID", "CORE_WORKER_TOKEN"])
def test_incomplete_configuration_does_not_start_any_control_threads(control, monkeypatch, missing):
    configure(monkeypatch)
    monkeypatch.delenv(missing)
    control.threading = SimpleNamespace(Thread=lambda **kw: pytest.fail("incomplete config started a thread"))
    assert not control._heartbeat_configured() and not control._core_worker_jobs_configured()
    control._start_core_worker_heartbeat(host="0.0.0.0", port=8766)
    control._start_apk_child_auto_enrollment()
    control._start_core_worker_jobs(host="0.0.0.0", port=8766, max_body_bytes=100, max_output_bytes=100, job_timeout=10)


@pytest.mark.parametrize(("heartbeat", "jobs", "expected"), [("false", "true", (False, False)),
    ("true", "false", (True, False)), ("1", "sim", (True, True)), ("0", "0", (False, False))])
def test_flags_keep_heartbeat_dependency_for_jobs(control, monkeypatch, heartbeat, jobs, expected):
    configure(monkeypatch)
    monkeypatch.setenv("CORE_WORKER_HEARTBEAT_ENABLED", heartbeat)
    monkeypatch.setenv("CORE_WORKER_JOBS_ENABLED", jobs)
    assert (control._heartbeat_configured(), control._core_worker_jobs_configured()) == expected


def test_configured_flag_matches_normalized_empty_base_url(control, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("CORE_WORKER_VPS_URL", " /// ")
    assert control._core_worker_auth_parts()[0] == ""
    assert not control._heartbeat_configured() and not control._core_worker_jobs_configured()


@pytest.mark.parametrize(("host", "port", "endpoint"), [("192.0.2.4", 8768, "http://192.0.2.4:8768"),
    ("2001:db8::4", 8768, "http://[2001:db8::4]:8768"), ("0.0.0.0", 8768, ""),
    ("::", 8768, ""), ("192.0.2.4", None, "")])
def test_payload_announces_effective_endpoint_and_live_port(control, monkeypatch, host, port, endpoint):
    configure(monkeypatch)
    monkeypatch.setattr(control, "_EFFECTIVE_HTTP_PORT", port)
    result = control._core_worker_payload(host=host, port=9999)
    assert result["endpoint"] == endpoint
    assert result["health"]["http_port"] == result["status"]["http_port"] == port
    assert result["health"]["control_plane_alive"] and result["status"]["control_plane_alive"]
    assert result["worker_id"] == result["physical_worker_id"] == "parent"
    assert result["runtime_kind"] == "termux" and result["source"] == "termux-phone-worker"
    assert result["supported_tasks"] == ["ping", "worker_update"]
    assert result["source_hash"] == "a" * 64 and result["version"] == control.PHONE_WORKER_VERSION
    assert result["health"]["scripts_ok"] and not result["health"]["boot_ok"]
    assert result["health"]["supervisor_ok"] and result["health"]["sshd_ok"] is None
    assert result["status"]["worker_update"]["updater"] == {"state": "idle"}
    assert "private-token" not in json.dumps(result)


def test_explicit_endpoint_and_alias_precedence(control, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_ENDPOINT", " https://alias.invalid/worker ")
    assert control._core_worker_payload(host="0.0.0.0", port=9999)["endpoint"] == "https://alias.invalid/worker"
    monkeypatch.setenv("CORE_WORKER_ENDPOINT", "https://preferred.invalid/")
    assert control._core_worker_payload(host="127.0.0.1", port=9999)["endpoint"] == "https://preferred.invalid/"


def test_payload_does_not_mutate_provided_role_and_capability_lists(control, monkeypatch):
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
    monkeypatch.setattr(control, "_current_core_worker_roles_and_capabilities", lambda:
        (["music", "music-agent"] + [f"role-{i}" for i in range(20)], ["music-voice"] + [f"cap-{i}" for i in range(30)]))
    control._set_core_job_active({"job_id": "active", "type": "worker_update"})
    monkeypatch.setattr(control, "_PENDING_CORE_JOB_RESULTS", {"one": {}, "two": {}})
    result = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert len(result["roles"]) == 16 and len(result["capabilities"]) == 24
    assert any(s.startswith("music") for s in result["roles"]) is (not safe)
    assert result["status"]["core_worker_jobs"]["active_job_id"] == "active"
    assert result["status"]["core_worker_jobs"]["pending_results"] == 2
    assert result["profile_label"] == result["status"]["profile_label"] == "label-turbo"


@pytest.mark.parametrize("probe", ["_system_status", "_music_node_snapshot", "_music_agent_snapshot"])
@pytest.mark.parametrize("failure", ["raises", "non_object"])
def test_partial_snapshot_failure_does_not_prevent_heartbeat_payload(control, monkeypatch, probe, failure):
    def fail():
        if failure == "raises":
            raise PermissionError("optional probe unavailable")
        return ["invalid snapshot"]
    monkeypatch.setattr(control, probe, fail)
    result = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert result["health"]["control_plane_alive"] and result["battery"]["level"] == 42
    assert result["network"]["type"] == "unknown"
    assert "error" in (result["status"]["music_node"] if probe == "_music_node_snapshot" else
                       result["status"]["music_agent"] if probe == "_music_agent_snapshot" else {"error": "system is summarized"})


@pytest.mark.parametrize(("code", "data", "ok"), [(200, {}, True), (200, {"ok": False}, False), (503, {"ok": True}, False)])
def test_heartbeat_records_outcome_before_flushing_results(control, monkeypatch, code, data, ok):
    configure(monkeypatch)
    calls = []
    monkeypatch.setattr(control, "_post_core_worker_json", lambda path, payload, **kw: calls.append((path, kw)) or (code, data))
    monkeypatch.setattr(control, "_write_runtime_status", lambda **kw: calls.append(("status", kw)))
    monkeypatch.setattr(control, "_flush_pending_core_worker_job_results", lambda **kw: calls.append(("flush", kw)))
    assert control._send_core_worker_heartbeat_once(host="127.0.0.1", port=8766, timeout=20) is ok
    assert calls[0] == ("/core-worker/heartbeat", {"timeout": 20})
    assert calls[1] == ("status", {"control_plane_alive": True, "heartbeat_ok": ok,
                                  "reason": "heartbeat_ok" if ok else "heartbeat_failed"})
    assert calls[2:] == ([("flush", {"timeout": 5.0})] if ok else [])


def test_heartbeat_success_survives_failed_outbox_flush(control, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(control, "_post_core_worker_json", lambda *a, **kw: (200, {"ok": True}))
    monkeypatch.setattr(control, "_write_runtime_status", lambda **kw: None)
    def fail(**kw):
        raise OSError("outbox retry later")
    monkeypatch.setattr(control, "_flush_pending_core_worker_job_results", fail)
    assert control._send_core_worker_heartbeat_once(host="127.0.0.1", port=8766)


def test_pairing_selected_identity_updates_physical_parent(control, monkeypatch):
    configure(monkeypatch)
    payloads = []
    monkeypatch.setattr(control, "_post_json_url", lambda url, payload, **kw: payloads.append(payload) or (403, {"ok": False}))
    result = control._pair_core_worker(code="CORE-TEST", vps_url="https://vps.invalid", host="127.0.0.1", port=8766,
                                       worker_id="new-parent", name="fixture")
    assert not result["ok"] and payloads[0]["worker_id"] == "new-parent"
    assert payloads[0]["physical_worker_id"] == "new-parent"


def test_rejected_pairing_does_not_change_live_role_configuration(control, monkeypatch):
    monkeypatch.setenv("CORE_WORKER_ROLES", "phone-worker")
    monkeypatch.setenv("CORE_WORKER_CAPABILITIES", "status")
    payloads = []
    monkeypatch.setattr(control, "_post_json_url", lambda url, payload, **kw: payloads.append(payload) or (403, {"ok": False}))
    result = control._pair_core_worker(code="CORE-TEST", vps_url="https://vps.invalid", host="127.0.0.1", port=8766,
                                       worker_id="selected", name="fixture", roles="APK Builder;worker", capabilities="HASH;hash, Zip")
    assert not result["ok"]
    assert payloads[0]["roles"] == ["apk-builder", "worker"] and payloads[0]["capabilities"] == ["hash", "zip"]
    assert os.environ["CORE_WORKER_ROLES"] == "phone-worker"
    assert os.environ["CORE_WORKER_CAPABILITIES"] == "status"


def test_successful_pairing_persists_acknowledged_identity_and_normalized_roles(control, monkeypatch, tmp_path):
    configure(monkeypatch)
    env_path = tmp_path / "phone.env"
    monkeypatch.setattr(control, "_post_json_url", lambda *a, **kw:
        (200, {"ok": True, "worker_id": "accepted-parent", "token": "local-test-token"}))
    result = control._pair_core_worker(code="CORE-TEST", vps_url="https://vps.invalid///", host="127.0.0.1",
        port=8766, worker_id="selected", name="fixture", roles="APK Builder;worker",
        capabilities="HASH;hash, Zip", env_file=str(env_path))
    assert result["ok"] and result["worker_id"] == "accepted-parent"
    assert control._core_worker_auth_parts() == ("https://vps.invalid", "local-test-token", "accepted-parent")
    assert os.environ["CORE_WORKER_ROLES"] == "apk-builder,worker"
    assert os.environ["CORE_WORKER_CAPABILITIES"] == "hash,zip"
    assert "CORE_WORKER_TOKEN=local-test-token" in env_path.read_text()
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_payload_matches_corrected_pre_extraction_wire_contract(control, monkeypatch):
    configure(monkeypatch)
    expected = json.loads((PHONE.parents[3] / "tests/fixtures/phone_control_payload.json").read_text())
    expected["version"] = control.PHONE_WORKER_VERSION
    assert control._core_worker_payload(host="127.0.0.1", port=9999) == expected


@pytest.mark.parametrize("probe", ["_battery_snapshot", "_network_snapshot"])
def test_invalid_optional_telemetry_keeps_object_contract(control, monkeypatch, probe):
    monkeypatch.setattr(control, probe, lambda: ["invalid measurement"])
    payload = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert isinstance(payload["battery"], dict) and isinstance(payload["network"], dict)
    assert "payload_mode" not in payload["health"]
    assert "error" in payload["battery" if probe == "_battery_snapshot" else "network"]


def test_control_module_has_one_owner_and_observes_reassigned_runtime_state(control, monkeypatch):
    with ThreadPoolExecutor(max_workers=8) as pool:
        modules = list(pool.map(lambda _: control._phone_worker_control_plane_module(), range(32)))
    assert all(module is modules[0] for module in modules)
    assert Path(modules[0].__file__) == PHONE.parent / "phone_worker_runtime/control_plane.py"
    lock, active = control._CORE_JOB_LOCK, control._CORE_JOB_ACTIVE
    for port, at in [(8768, 10.0), (None, 20.0)]:
        monkeypatch.setattr(control, "_EFFECTIVE_HTTP_PORT", port)
        monkeypatch.setattr(control, "_DIRECT_HTTP_STATE", "port_conflict_control_plane_alive")
        monkeypatch.setattr(control, "_LAST_HEARTBEAT_OK_AT", at)
        monkeypatch.setattr(control, "_PENDING_CORE_JOB_RESULTS", {str(at): {}})
        monkeypatch.setattr(control, "_CORE_WORKER_NETWORK_STATE", {"last_ok_at": at, "last_error_kind": str(at)})
        control._set_core_job_active({"job_id": str(at), "type": "worker_update"})
        payload = control._core_worker_payload(host="127.0.0.1", port=9999)
        assert payload["health"]["http_port"] == payload["status"]["http_port"] == port
        assert payload["health"]["last_heartbeat_ok_at"] == at
        assert payload["status"]["core_worker_jobs"]["active_job_id"] == str(at)
        assert payload["status"]["core_worker_jobs"]["pending_results"] == 1
        assert payload["status"]["core_worker_network"]["last_ok_age_seconds"] is not None
        assert payload["status"]["core_worker_network"]["last_error_kind"] == str(at)
    assert control._CORE_JOB_LOCK is lock and control._CORE_JOB_ACTIVE is active


@pytest.mark.parametrize("initial", ["missing", "syntax_error", "incomplete"])
def test_bare_payload_recovers_after_module_arrival_without_optional_probes(control, monkeypatch, tmp_path, initial):
    configure(monkeypatch)
    lone = tmp_path / "phone_worker.py"
    shutil.copyfile(PHONE, lone)
    monkeypatch.setattr(control, "__file__", str(lone))
    target = tmp_path / "phone_worker_runtime/control_plane.py"
    target.parent.mkdir()
    if initial != "missing":
        target.write_text("this is invalid python !!!" if initial == "syntax_error" else "partial = True\n")
    monkeypatch.setattr(control, "_EFFECTIVE_HTTP_PORT", None)
    monkeypatch.setattr(control, "_PENDING_CORE_JOB_RESULTS", {"pending": {}})
    with monkeypatch.context() as patch:
        for name in ["_system_status", "_music_node_snapshot", "_music_agent_snapshot", "_battery_snapshot", "_network_snapshot"]:
            patch.setattr(control, name, lambda: pytest.fail("probe before control module arrives"))
        for _ in range(2):
            payload = control._core_worker_payload(host="127.0.0.1", port=8766)
            assert control._PHONE_WORKER_CONTROL_PLANE_MODULE is None
            assert payload["health"]["payload_mode"] == "bootstrap"
            assert payload["health"]["control_plane_alive"] and payload["endpoint"] == ""
            assert payload["worker_id"] == payload["physical_worker_id"] == "parent"
            assert "worker_update" in payload["supported_tasks"]
            assert payload["status"]["core_worker_jobs"]["pending_results"] == 1
            assert "battery" not in payload and "music_node" not in payload["status"]
    shutil.copyfile(PHONE.parent / "phone_worker_runtime/control_plane.py", target)
    payload = control._core_worker_payload(host="127.0.0.1", port=8766)
    assert "payload_mode" not in payload["health"] and payload["battery"]["level"] == 42
    assert Path(control._phone_worker_control_plane_module().__file__) == target


def test_full_and_bootstrap_payloads_are_accepted_by_local_registry(control, monkeypatch, tmp_path):
    from utility.commands.workers_registry import CoreWorkersRegistry
    configure(monkeypatch)
    registry = CoreWorkersRegistry(tmp_path / "registry.json")
    payload = control._core_worker_payload(host="127.0.0.1", port=8766)
    pairing = registry.create_pairing(created_by_id=1, created_by_name="local fixture")
    paired = registry.redeem_pairing({**payload, "code": pairing["code"]})
    def local_send(path, data, **kwargs):
        assert path == "/core-worker/heartbeat"
        return 200, registry.heartbeat(data, token=paired["token"])
    monkeypatch.setattr(control, "_post_core_worker_json", local_send)
    monkeypatch.setattr(control, "_runtime_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(control, "_flush_pending_core_worker_job_results", lambda **kw: 0)
    assert control._send_core_worker_heartbeat_once(host="127.0.0.1", port=8766)
    before = json.loads((tmp_path / "registry.json").read_text())["workers"]["parent"]
    with monkeypatch.context() as patch:
        def unavailable():
            raise FileNotFoundError("first update stage")
        patch.setattr(control, "_phone_worker_control_plane_module", unavailable)
        patch.setattr(control, "_EFFECTIVE_HTTP_PORT", None)
        assert control._send_core_worker_heartbeat_once(host="127.0.0.1", port=8766)
        after = json.loads((tmp_path / "registry.json").read_text())["workers"]["parent"]
        assert after["battery"] == before["battery"] and after["network"] == before["network"]
        assert after["physical_worker_id"] == "parent" and after["runtime_kind"] == "termux"
        assert after["health"]["payload_mode"] == "bootstrap" and after["endpoint"] == ""
        assert after["health"]["control_plane_alive"] and after["health"]["http_port"] is None
    assert control._send_core_worker_heartbeat_once(host="127.0.0.1", port=8766)
    after = json.loads((tmp_path / "registry.json").read_text())["workers"]["parent"]
    assert "payload_mode" not in after["health"] and after["endpoint"] == "http://127.0.0.1:8768"


def test_control_module_does_not_mutate_inputs(control):
    base = json.loads((PHONE.parents[3] / "tests/fixtures/phone_control_payload.json").read_text())
    snapshots = {"system": {"ffprobe": True}, "music_node": {"online": True}, "music_agent": {},
                 "battery": {"level": 7}, "network": {"type": "wifi"}, "updater": {"state": "idle"}}
    before = copy.deepcopy((base, snapshots))
    result = control._phone_worker_control_plane_module().build_payload(base, **snapshots)
    assert (base, snapshots) == before
    assert "music" in result["roles"] and "ffprobe" in result["capabilities"]


def test_lone_entrypoint_and_control_module_import_have_no_startup_effects(tmp_path):
    lone = tmp_path / "phone_worker.py"
    shutil.copyfile(PHONE, lone)
    code = r'''
import importlib.util, os, socket, subprocess, sys, threading
from unittest.mock import patch
sys.dont_write_bytecode = True
def audit(event, args):
    if event == "open" and ((isinstance(args[1], str) and any(c in args[1] for c in "wa+"))
                            or (isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR))):
        raise AssertionError("write during import")
sys.addaudithook(audit)
before = dict(os.environ)
with patch.object(threading.Thread, "start", side_effect=AssertionError("thread during import")), \
     patch.object(subprocess, "Popen", side_effect=AssertionError("process during import")), \
     patch.object(socket.socket, "connect", side_effect=AssertionError("network during import")), \
     patch.object(socket.socket, "bind", side_effect=AssertionError("bind during import")):
    for index, path in enumerate(sys.argv[1:]):
        spec = importlib.util.spec_from_file_location("isolated_control_" + str(index), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if index == 0:
            assert module._PHONE_WORKER_CONTROL_PLANE_MODULE is None
            assert not module._heartbeat_configured()
            module._start_core_worker_heartbeat(host="0.0.0.0", port=8766)
            module._start_core_worker_jobs(host="0.0.0.0", port=8766, max_body_bytes=100, max_output_bytes=100, job_timeout=10)
assert dict(os.environ) == before
assert "phone_worker" not in sys.modules and "music_agent" not in sys.modules and "tts_transport" not in sys.modules
'''
    env = os.environ.copy()
    env.pop("CORE_WORKER_TOKEN", None)
    result = subprocess.run([sys.executable, "-S", "-c", code, str(lone),
        str(PHONE.parent / "phone_worker_runtime/control_plane.py")], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
