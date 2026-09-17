"""Battery contracts exercised through the facade and real text/JSON readers."""
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"
SYSFS = Path("/sys/class/power_supply")


def load_worker(path=PHONE):
    spec = importlib.util.spec_from_file_location("phone_telemetry_test", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


@pytest.fixture
def battery_env(tmp_path, monkeypatch):
    worker = load_worker()
    power = tmp_path / "power_supply"
    power.mkdir()
    real_glob = Path.glob
    real_read = worker._read_text_file
    real_exists = worker._safe_path_exists

    def local(path):
        return power / path.relative_to(SYSFS) if path.is_relative_to(SYSFS) else path

    # Files are real fixtures; the production reader still handles decode,
    # whitespace, limits and permission errors. No probe touches the host /sys.
    monkeypatch.setattr(worker, "_read_text_file", lambda path, **kw: real_read(local(path), **kw))
    monkeypatch.setattr(worker, "_safe_path_exists", lambda path: real_exists(local(path)))
    monkeypatch.setattr(Path, "glob", lambda path, pattern: real_glob(local(path), pattern))
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: {})

    def battery(name="battery", **fields):
        directory = power / name
        directory.mkdir(exist_ok=True)
        for key, value in fields.items():
            (directory / key).write_text(str(value) + "\n", encoding="utf-8")
        return directory

    return worker, battery


@pytest.mark.parametrize(("raw", "expected"), [("355", 35.5), ("-55", -5.5), ("0", 0.0), ("1000", 100.0), ("1001", 100.1)])
def test_sysfs_temperature_uses_tenths_celsius_at_all_magnitudes(battery_env, raw, expected):
    worker, battery = battery_env
    battery(temp=raw)
    assert worker._sysfs_battery_snapshot() == {"available": True, "source": "sysfs", "temperature_c": expected}


def test_sysfs_preserves_fields_and_primary_priority(battery_env):
    worker, battery = battery_env
    battery(capacity="102.9", status=" Full ", type="Battery")
    battery("BAT0", capacity="12", status="Discharging")
    assert worker._battery_snapshot() == {"available": True, "source": "sysfs", "level": 100,
                                          "status": "full", "charging": True, "plugged": "battery"}


def test_sysfs_skips_empty_candidate_and_handles_partial_read_denial(battery_env, monkeypatch):
    worker, battery = battery_env
    battery(type="Battery", capacity="invalid")
    good = battery("BAT0", capacity="12", status="Discharging")
    real_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == good / "capacity":
            raise PermissionError("capacity unreadable")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert worker._sysfs_battery_snapshot() == {"available": True, "source": "sysfs",
                                                "status": "discharging", "charging": False}


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("offline"), RuntimeError("probe broken")])
@pytest.mark.parametrize("primary", [True, False])
def test_sysfs_enumeration_failure_keeps_already_discovered_battery(battery_env, monkeypatch, error, primary):
    worker, battery = battery_env
    if primary:
        battery(capacity=64)
    real_glob = Path.glob

    def glob(path, pattern):
        if path == SYSFS:
            raise error
        return real_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", glob)
    result = worker._sysfs_battery_snapshot()
    if primary:
        assert result == {"available": True, "source": "sysfs", "level": 64}
    else:
        source = "sysfs_permission_denied" if isinstance(error, OSError) else "sysfs_error"
        assert result == worker._empty_battery_snapshot(source, "" if isinstance(error, OSError) else error)


def test_no_battery_returns_existing_unavailable_schema(battery_env):
    worker, _ = battery_env
    assert worker._battery_snapshot() == {"available": False, "source": "sysfs_unavailable",
                                          "level": None, "charging": None, "temperature_c": None}


@pytest.mark.parametrize("failure", ["missing", "exit", "json", "list", "timeout", "permission", "error_object"])
def test_termux_command_failures_fall_back_to_sysfs(battery_env, monkeypatch, failure):
    worker, battery = battery_env
    battery(capacity=52)
    # Exercise the production JSON command boundary, with command execution
    # controlled in-process. The exact existing timeout/argv remain unchanged.
    monkeypatch.setattr(worker, "_run_json_command", load_worker()._run_json_command)
    monkeypatch.setattr(worker.shutil, "which", lambda command: None if failure == "missing" else "/fixture/termux-battery-status")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "permission":
            raise PermissionError("API inaccessible")
        output = {"json": b"malformed", "list": b"[1]", "error_object": b'{"error":"permission denied"}'}.get(failure, b"{}")
        return subprocess.CompletedProcess(command, 1 if failure == "exit" else 0, stdout=output)

    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker._battery_snapshot() == {"available": True, "source": "sysfs", "level": 52}
    if failure == "missing":
        assert calls == []
    else:
        assert len(calls) == 1 and calls[0][0] == ["termux-battery-status"]
        assert calls[0][1] == {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "timeout": 2.0}


@pytest.mark.parametrize(("percentage", "expected"), [(0, 0), ("82.9", 82), (-5, 0), (140, 100)])
def test_termux_level_aliases_status_precedence_and_degrees(battery_env, monkeypatch, percentage, expected):
    worker, _ = battery_env
    raw = {"percentage": percentage, "level": 99, "status": " Discharging ", "plugged": "PLUGGED_USB", "temperature": 36.5}
    before = dict(raw)
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: raw)
    monkeypatch.setattr(worker, "_sysfs_battery_snapshot", lambda: pytest.fail("valid Termux result must avoid sysfs probes"))
    assert worker._battery_snapshot() == {"available": True, "source": "termux-api", "level": expected,
        "percentage": expected, "percent": expected, "charging": False,
        "status": "discharging", "plugged": "plugged_usb", "temperature_c": 36.5}
    assert raw == before


@pytest.mark.parametrize(("raw", "fields"), [({"level": 4}, {"level": 4, "percentage": 4, "percent": 4}),
    ({"plugged": "UNPLUGGED"}, {"plugged": "unplugged", "charging": False}),
    ({"status": "FULL"}, {"status": "full", "charging": True}),
    ({"temperature": 0}, {"temperature_c": 0.0})])
def test_valid_partial_termux_result_does_not_trigger_another_probe(battery_env, monkeypatch, raw, fields):
    worker, _ = battery_env
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: raw)
    monkeypatch.setattr(worker, "_sysfs_battery_snapshot", lambda: pytest.fail("partial data is still usable"))
    assert worker._battery_snapshot() == {"available": True, "source": "termux-api", **fields}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "invalid"])
@pytest.mark.parametrize("source", ["sysfs", "termux"])
def test_invalid_temperature_does_not_poison_partial_snapshot_json(battery_env, monkeypatch, value, source):
    worker, battery = battery_env
    if source == "sysfs":
        battery(capacity=42, temp=value)
    else:
        monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: {"percentage": 42, "temperature": value})
    result = worker._battery_snapshot()
    assert result["available"] and result["level"] == 42
    assert "temperature_c" not in result
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("raw", [{"health": "good"}, {"percentage": "bad", "temperature": "NaN"}])
def test_unusable_termux_payload_uses_existing_fallback(battery_env, monkeypatch, raw):
    worker, battery = battery_env
    battery(capacity=71)
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: raw)
    assert worker._battery_snapshot() == {"available": True, "source": "sysfs", "level": 71}


@pytest.mark.parametrize(("error", "source"), [(PermissionError("blocked"), "battery_permission_denied"),
                                             (ValueError("broken"), "battery_error")])
def test_unexpected_probe_exception_keeps_bounded_error_schema(battery_env, monkeypatch, error, source):
    worker, _ = battery_env

    def fail():
        raise error

    monkeypatch.setattr(worker, "_sysfs_battery_snapshot", fail)
    assert worker._battery_snapshot() == worker._empty_battery_snapshot(source, error)


@pytest.mark.parametrize(("temperature", "allowed"), [(350, True), (450, False)])
def test_teto_resource_guard_consumes_corrected_sysfs_temperature(battery_env, monkeypatch, temperature, allowed):
    worker, battery = battery_env
    battery(capacity=80, status="Discharging", temp=temperature)
    monkeypatch.setattr(worker, "_available_memory_mb", lambda: 2048)
    monkeypatch.setattr(worker, "_core_job_runtime_snapshot", lambda: {})
    monkeypatch.setenv("PHONE_WORKER_TETO_MIN_FREE_MEMORY_MB", "1200")
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_BATTERY_TEMP_C", "43")
    monkeypatch.setenv("PHONE_WORKER_TETO_MIN_BATTERY_PERCENT", "25")
    result = worker._teto_resource_snapshot()
    assert result["battery_temperature_c"] == temperature / 10
    assert result["ok"] is allowed
    assert ("bateria aquecida" in result["reason"]) is (not allowed)
    assert worker._TETO_RENDERER is None


def test_telemetry_loader_has_one_owner_and_uses_live_facade_probes(battery_env, monkeypatch):
    worker, _ = battery_env
    assert worker._PHONE_WORKER_TELEMETRY_MODULE is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        modules = list(pool.map(lambda _: worker._phone_worker_telemetry_module(), range(32)))
    assert all(item is modules[0] for item in modules)
    assert Path(modules[0].__file__) == PHONE.parent / "phone_worker_runtime/telemetry.py"
    for level in [11, 82]:
        monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: {"level": level})
        assert worker._battery_snapshot()["level"] == level
    fallback = {"available": False, "source": "late-sysfs"}
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: {})
    monkeypatch.setattr(worker, "_sysfs_battery_snapshot", lambda: fallback)
    assert worker._battery_snapshot() is fallback
    monkeypatch.setattr(modules[0], "battery_snapshot", lambda **kw: {"source": "late-module"})
    assert worker._battery_snapshot() == {"source": "late-module"}


def test_sysfs_reader_and_empty_factory_are_resolved_after_module_load(battery_env, monkeypatch):
    worker, battery = battery_env
    battery(capacity=80)
    assert worker._sysfs_battery_snapshot()["level"] == 80
    monkeypatch.setattr(worker, "_read_text_file", lambda path, **kw: "24" if path.name == "capacity" else "")
    assert worker._sysfs_battery_snapshot()["level"] == 24
    empty = {"available": False, "source": "late-empty"}
    monkeypatch.setattr(worker, "_read_text_file", lambda *a, **kw: "")
    monkeypatch.setattr(worker, "_empty_battery_snapshot", lambda *a, **kw: empty)
    assert worker._sysfs_battery_snapshot() is empty


@pytest.mark.parametrize("initial", ["missing", "broken"])
def test_lone_entrypoint_survives_and_retries_when_telemetry_arrives(tmp_path, monkeypatch, initial):
    bare = tmp_path / "phone_worker.py"
    bare.write_bytes(PHONE.read_bytes())
    target = tmp_path / "phone_worker_runtime/telemetry.py"
    if initial == "broken":
        target.parent.mkdir()
        target.write_text("raise RuntimeError('incomplete module')\n")
    worker = load_worker(bare)
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: pytest.fail("no probe before telemetry is available"))
    assert not worker._battery_snapshot()["available"]
    assert not worker._sysfs_battery_snapshot()["available"]
    assert worker._network_snapshot()["type"] == "unknown"
    assert not worker._vps_tcp_ping_snapshot()["available"]
    assert not worker._tailscale_snapshot(probe_vps=True)["connected"]
    assert worker._PHONE_WORKER_TELEMETRY_MODULE is None
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(PHONE.parent / "phone_worker_runtime/telemetry.py", target)
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: {"level": 66})
    assert worker._battery_snapshot()["level"] == 66
    assert Path(worker._phone_worker_telemetry_module().__file__) == target
    assert worker._PHONE_WORKER_CONFIG_MODULE is None and worker._TETO_RENDERER is None


def test_telemetry_module_import_and_parsing_have_no_runtime_side_effects(tmp_path):
    code = r'''
import importlib.util, os, socket, subprocess, sys, threading
from unittest.mock import patch
sys.dont_write_bytecode = True
before = dict(os.environ)
effects = []
def audit(event, args):
    if event == "open" and ((isinstance(args[1], str) and any(c in args[1] for c in "wa+"))
                            or (isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR))):
        effects.append(event)
        raise AssertionError("write during telemetry import/parse")
sys.addaudithook(audit)
with patch.object(threading.Thread, "start", side_effect=AssertionError("thread")), \
     patch.object(subprocess, "Popen", side_effect=AssertionError("process")), \
     patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
     patch.object(socket.socket, "bind", side_effect=AssertionError("bind")):
    spec = importlib.util.spec_from_file_location("isolated_telemetry", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def unexpected(*a, **kw):
        raise AssertionError("unneeded probe")
    result = module.battery_snapshot(run_json_command=lambda *a, **kw: {"level": 12},
                                     sysfs_snapshot=unexpected, empty_snapshot=unexpected)
    assert result["level"] == 12 and result["source"] == "termux-api"
assert not effects and dict(os.environ) == before
assert "phone_worker" not in sys.modules and "music_agent" not in sys.modules
assert "tts_transport" not in sys.modules
'''
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-S", "-c", code, str(PHONE.parent / "phone_worker_runtime/telemetry.py")],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("snapshot", "heavy_ok"), [
    ({"level": 0, "percent": 99, "charging": False}, False),
    ({"level": 0, "charging": False}, False),
    ({"level": 11, "charging": False, "status": "discharging", "plugged": "unplugged"}, False),
    ({"level": 11, "plugged": "battery"}, False),
    ({"level": 11, "charging": True}, True),
    ({"percent": 11, "status": "full"}, True),
    ({"level": 11, "plugged": "plugged_usb"}, True),
    ({"level": 80, "charging": False}, True),
    ({"available": False, "level": None, "charging": None}, True),
])
def test_assist_readiness_respects_zero_level_and_actual_charging(battery_env, monkeypatch, snapshot, heavy_ok):
    worker, _ = battery_env
    monkeypatch.setattr(worker, "_battery_snapshot", lambda: snapshot)
    monkeypatch.setattr(worker, "_network_snapshot", lambda: {"vps_reachable": True})
    monkeypatch.setattr(worker, "_system_status", lambda: {})
    monkeypatch.setattr(worker, "_current_core_worker_roles_and_capabilities", lambda: (["auxiliar"], ["apk-builder"]))
    result = worker._assist_readiness_snapshot({"min_battery_for_heavy": 25})
    assert result["heavy_ok"] is heavy_ok
    assert ("apk_build_debug" in result["recommended_tasks"]) is heavy_ok
    assert ("bateria baixa para tarefa pesada" in result["reasons"]) is (not heavy_ok)
    assert result["battery"] is snapshot and result["ok"]
    assert "hash_batch" in result["recommended_tasks"]
