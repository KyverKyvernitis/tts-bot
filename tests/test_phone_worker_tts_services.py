"""Optional service loading must preserve bootstrap recovery and one runtime."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import PHONE, tts  # noqa: F401

SERVICES = ["tts_cache", "tts_android", "tts_providers", "pcm_io"]


@pytest.mark.parametrize("service", SERVICES)
def test_service_load_is_single_under_contention_and_warm_path_does_no_io(tts, monkeypatch, service):
    worker = tts.worker
    loader = getattr(worker, "_phone_worker_" + service + "_module")
    assert getattr(worker, "_PHONE_WORKER_" + service.upper() + "_MODULE") is None
    original = importlib.util.spec_from_file_location
    loaded = []

    def spec_for(*args, **kwargs):
        spec = original(*args, **kwargs)
        execute = spec.loader.exec_module

        def execute_once(module):
            loaded.append(module)
            execute(module)

        spec.loader.exec_module = execute_once
        return spec

    monkeypatch.setattr(worker, "importlib", SimpleNamespace(util=SimpleNamespace(
        spec_from_file_location=spec_for, module_from_spec=importlib.util.module_from_spec)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        modules = list(pool.map(lambda _: loader(), range(32)))
    assert len(loaded) == 1 and all(module is loaded[0] for module in modules)
    monkeypatch.setattr(worker, "Path", lambda *a: pytest.fail("warm service read source path"))
    monkeypatch.setattr(worker, "_PHONE_WORKER_" + service.upper() + "_LOCK", None)
    assert all(loader() is loaded[0] for _ in range(10))
    assert not tts.calls and not tts.executor.jobs


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("initial", ["missing", "incomplete", "syntax_error"])
def test_late_service_arrival_keeps_minimal_control_alive_and_retries(tts, monkeypatch, tmp_path, service, initial):
    worker = tts.worker
    phone = tmp_path / "late/phone_worker.py"
    target = phone.parent / "phone_worker_runtime" / (service + ".py")
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(worker, "__file__", str(phone))
    if initial != "missing":
        target.write_text("prune_audio_cache = None\n" if initial == "incomplete" else "broken !!!")
    loader = getattr(worker, "_phone_worker_" + service + "_module")
    with pytest.raises((RuntimeError, SyntaxError)):
        loader()
    assert getattr(worker, "_PHONE_WORKER_" + service.upper() + "_MODULE") is None
    monkeypatch.setattr(worker, "_runtime_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(worker, "_phone_worker_source_hash", lambda: "fixture-hash")
    monkeypatch.setattr(worker, "_default_worker_name", lambda: "isolated service fixture")
    payload = worker._core_worker_payload(host="0.0.0.0", port=8766)
    assert payload["health"]["control_plane_alive"] and payload["health"]["payload_mode"] == "bootstrap"
    shutil.copy2(PHONE.parent / "phone_worker_runtime" / target.name, target)
    assert loader().__file__ == str(target)
    assert not tts.calls and not tts.executor.jobs


@pytest.mark.parametrize("service", SERVICES)
def test_service_import_starts_no_io_network_threads_or_runtime(tmp_path, service):
    path = PHONE.parent / "phone_worker_runtime" / (service + ".py")
    code = r'''
import builtins, importlib.util, os, socket, subprocess, sys, threading, urllib.request
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True
def forbidden(*a, **kw):
    raise AssertionError("service import touched runtime")
before = dict(os.environ)
with patch.object(threading.Thread, "start", forbidden), patch.object(socket.socket, "connect", forbidden), \
     patch.object(subprocess, "Popen", forbidden), patch.object(os, "getenv", forbidden), \
     patch.object(builtins, "open", forbidden), patch.object(os, "scandir", forbidden), \
     patch.object(os, "utime", forbidden), patch.object(urllib.request, "urlopen", forbidden):
    spec = importlib.util.spec_from_file_location("isolated_tts_service", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
assert dict(os.environ) == before
assert not any(n.startswith(("phone_worker", "music_agent", "teto_renderer")) for n in sys.modules)
assert "tts_transport" not in sys.modules
assert "edge_tts" not in sys.modules and "gtts" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-S", "-c", code, str(path)], cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("available", ["all", "none", "without_policy"])
def test_startup_preloads_service_independently_of_policy_failure(tts, monkeypatch, tmp_path, service, available):
    worker = tts.worker
    if available != "all":
        phone = tmp_path / "install/phone_worker.py"
        monkeypatch.setattr(worker, "__file__", str(phone))
        if available == "without_policy":
            target = phone.parent / "phone_worker_runtime" / (service + ".py")
            target.parent.mkdir(parents=True)
            shutil.copy2(PHONE.parent / "phone_worker_runtime" / target.name, target)
    monkeypatch.setattr(worker, "_load_phone_worker_runtime_env", lambda: None)
    monkeypatch.setattr(worker, "_load_env_file", lambda: None)
    monkeypatch.setattr(worker, "_load_persisted_pending_core_job_results", lambda: None)
    called = []

    def heartbeat(**kwargs):
        assert (getattr(worker, "_PHONE_WORKER_" + service.upper() + "_MODULE") is not None) == (available != "none")
        assert (worker._PHONE_WORKER_TTS_POLICY_MODULE is not None) == (available == "all")
        called.append(kwargs)
        return True

    monkeypatch.setattr(worker, "_send_core_worker_heartbeat_once", heartbeat)
    monkeypatch.setattr(sys, "argv", ["phone-worker", "--heartbeat-once"])
    assert worker.main() == 0 and len(called) == 1
    assert not tts.calls and not tts.executor.jobs


def test_cache_binding_uses_reassigned_service_function_and_shared_lock(tts, monkeypatch):
    module = tts.worker._phone_worker_tts_cache_module()
    monkeypatch.setattr(module, "find_file", lambda root, key: (root / (key + ".wav"), "wav"))
    assert tts.handler._find_tts_cache_file("new") == (tts.root / "new.wav", "wav")
    entered = []

    class Lock:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(tts.worker, "_TTS_CACHE_MAINTENANCE_LOCK", Lock())
    tts.handler._touch_tts_cache_file(tts.root / "missing.wav")
    assert entered == [True] and str(tts.root / "missing.wav") in tts.worker._TTS_CACHE_TOUCHES
