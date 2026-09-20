"""Behavior gates before moving Phone Worker state across module boundaries."""
import os
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("phone_boundaries_test", PHONE)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def test_import_has_no_runtime_startup_or_configuration_writes(tmp_path):
    env = os.environ.copy()
    env.update(PHONE_WORKER_DIR=str(tmp_path / "worker"), PHONE_WORKER_ENV=str(tmp_path / "missing.env"),
               MUSIC_AGENT_ENV=str(tmp_path / "secrets/music.env"), MUSIC_AGENT_AUTO_TOKEN="1")
    env.pop("MUSIC_AGENT_TOKEN", None)
    code = r'''
import importlib.util, os, socket, subprocess, sys, threading
from unittest.mock import patch
sys.dont_write_bytecode = True
effects = []
def audit(event, args):
    if event == "open" and ((isinstance(args[1], str) and any(c in args[1] for c in "wa+"))
                            or (isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR))):
        effects.append((event, str(args[0])))
        raise AssertionError("write during import")
sys.addaudithook(audit)
before = dict(os.environ)
with patch.object(threading.Thread, "start", side_effect=AssertionError("thread during import")), \
     patch.object(subprocess, "Popen", side_effect=AssertionError("process during import")), \
     patch.object(socket.socket, "connect", side_effect=AssertionError("network during import")), \
     patch.object(socket.socket, "bind", side_effect=AssertionError("bind during import")):
    spec = importlib.util.spec_from_file_location("phone_boundary", sys.argv[1])
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
assert not effects, effects
assert dict(os.environ) == before, "import changed environment/credentials"
assert worker._APK_IDENTITY_MODULE is None
assert worker._TETO_RENDERER is None
assert "music_agent" not in sys.modules and "tts_transport" not in sys.modules
assert not any(name.startswith("teto_renderer") for name in sys.modules)
assert worker._CORE_JOB_ACTIVE == {} and worker._PENDING_CORE_JOB_RESULTS == {}
assert worker._TTS_CACHE_MAINTENANCE_EXECUTOR._threads == set()
'''
    result = subprocess.run([sys.executable, "-c", code, str(PHONE)], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_job_consumers_share_live_maps_and_locks(monkeypatch):
    worker = load_worker()
    lock, active = worker._CORE_JOB_LOCK, worker._CORE_JOB_ACTIVE
    worker._set_core_job_active({"job_id": "first", "type": "sha256"})
    assert worker._core_job_runtime_snapshot()["active_job_id"] == "first"
    worker._finish_core_job("first", "sha256", "succeeded", summary="done", sent_ok=False)
    snapshot = worker._core_job_runtime_snapshot()
    assert not snapshot["active"] and snapshot["last_result_job_id"] == "first"
    assert worker._CORE_JOB_ACTIVE is active and worker._CORE_JOB_LOCK is lock
    monkeypatch.setattr(worker, "_PENDING_CORE_JOB_RESULTS", {"one": {}, "two": {}})
    assert worker._core_job_runtime_snapshot()["pending_results"] == 2


def test_status_observes_reassigned_runtime_scalars(tmp_path, monkeypatch):
    worker = load_worker()
    monkeypatch.setattr(worker, "_runtime_state_dir", lambda: tmp_path)
    monkeypatch.setattr(worker, "_phone_worker_source_hash", lambda: "a" * 64)
    monkeypatch.setenv("CORE_WORKER_ID", "phone-test")
    for port, state in [(8768, "listening"), (None, "port_conflict_control_plane_alive")]:
        monkeypatch.setattr(worker, "_EFFECTIVE_HTTP_PORT", port)
        monkeypatch.setattr(worker, "_DIRECT_HTTP_STATE", state)
        worker._write_runtime_status(control_plane_alive=True, heartbeat_ok=True)
        status = json.loads((tmp_path / "runtime-status.json").read_text())
        assert status["http_port"] == port and status["direct_http_state"] == state
        assert status["last_heartbeat_ok_at"] == worker._LAST_HEARTBEAT_OK_AT
        assert status["control_plane_alive"]


def test_optional_tts_transport_has_one_owner_under_concurrent_loading(monkeypatch):
    worker = load_worker()
    monkeypatch.setitem(sys.modules, "tts_transport", None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = list(pool.map(lambda _: worker._tts_transport_module(), range(32)))
    assert loaded[0] is not None and all(item is loaded[0] for item in loaded)
    assert sys.modules["tts_transport"] is loaded[0]
    assert worker._TETO_RENDERER is None


def test_configuration_reads_environment_after_import(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("PHONE_BOUNDARY_FLAG", "true")
    assert worker._env_bool("PHONE_BOUNDARY_FLAG")
    monkeypatch.setenv("PHONE_BOUNDARY_FLAG", "false")
    assert not worker._env_bool("PHONE_BOUNDARY_FLAG", True)


def test_webserver_wsgi_server_dependency_is_declared():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower().splitlines()
    declared = [line.strip() for line in requirements if line.strip() and not line.lstrip().startswith("#")]
    assert any(line == "waitress" or line.startswith(("waitress=", "waitress<", "waitress>", "waitress[")) for line in declared)
