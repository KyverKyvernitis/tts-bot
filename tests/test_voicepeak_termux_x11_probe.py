"""Regression checks for Xlib probe truthfulness without requiring Android."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("voicepeak_x11_probe", TOOLKIT / "probe-x11.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def script(tmp_path, code):
    path = tmp_path / "guest.py"
    path.write_text(code)
    return [sys.executable, str(path)]


@pytest.mark.parametrize("output,exit_code,expected", [
    ("stage: done\\nVOICEPEAK_X11_PROBE_OK", 0, True),
    ("stage: XOpenDisplay", 0, False),
    ("VOICEPEAK_X11_PROBE_OK", 1, False),
    ("a quoted VOICEPEAK_X11_PROBE_OK is not success", 0, False),
    ("proot info: vpid 1: terminated with signal 11\\nVOICEPEAK_X11_PROBE_OK", 0, False),
])
def test_exit_and_marker_and_signal_must_agree(probe, tmp_path, output, exit_code, expected):
    rendered = output.replace("\\n", "\n")
    command = script(tmp_path, f"import sys\nprint({rendered!r})\nsys.exit({exit_code})\n")
    result = probe.run_probe(command, 2)
    assert result["ok"] is expected
    assert result["output"]
    if "signal 11" in output:
        assert result["signal"] == 11 and result["code"] == 0


def test_timeout_retains_last_stage(probe, tmp_path):
    command = script(tmp_path, "import time\nprint('stage: XGetWindowProperty',flush=True)\ntime.sleep(10)\n")
    result = probe.run_probe(command, 0.1)
    assert not result["ok"] and result["error"] == "tempo esgotado"
    assert "XGetWindowProperty" in result["output"]


def test_bounded_log_cannot_report_success_after_truncation(probe, tmp_path):
    command = script(tmp_path, "print('VOICEPEAK_X11_PROBE_OK')\nprint('x'*65536)\n")
    result = probe.run_probe(command, 2)
    assert not result["ok"] and len(result["output"]) == 65536


def test_probe_profile_uses_shared_x11_and_never_launches_engine(probe, tmp_path, monkeypatch):
    config = {"container": "voicepeak-arm64", "guest_box64": "/opt/voicepeak-box64/bin/box64",
              "guest_executable": "/opt/Voicepeak/voicepeak", "engine_directory": "/commercial",
              "backend": "box64", "display": ":1"}
    seen = []
    def login(selected, command):
        seen.append(selected)
        return ["proot-distro", "login", selected["container"], "--shared-tmp", "--", *command]
    monkeypatch.setattr(probe, "login_command", login)
    binary = tmp_path / "probe with spaces" / "x11-probe-x86_64"
    for early in (False, True):
        command = probe.probe_command(config, binary, early_threads=early)
        assert "--shared-tmp" in command
        assert f"{binary.parent}:/opt/voicepeak-x11-probe" in command
        assert f"BOX64_X11THREADS={int(early)}" in command
        assert "BOX64_LOG=1" in command and "BOX64_ROLLING_LOG=64" in command
        assert "BOX64_DYNAREC=0" in command and "BOX64_SHOWBT=0" in command
        assert config["guest_executable"] not in command
    assert all(selected["engine_directory"] == "" for selected in seen)
    assert config["engine_directory"] == "/commercial"


def test_report_preserves_each_outcome_and_uses_downloads(probe, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "storage/downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(probe, "load_config", lambda: {"backend": "box64", "container": "voicepeak-arm64", "display": ""})
    calls = []
    monkeypatch.setattr(probe, "probe_command", lambda config, binary, **kw: calls.append((dict(config), kw)) or [])
    outcomes = iter([{"ok": False, "code": 0, "signal": 11, "output": "stage before crash"},
                     {"ok": True, "code": 0, "output": "VOICEPEAK_X11_PROBE_OK"}])
    monkeypatch.setattr(probe, "run_probe", lambda command, timeout: next(outcomes))
    monkeypatch.setattr(sys, "argv", ["probe-x11.py"])
    assert probe.main() == 1
    report = json.loads((downloads / "voicepeak-box64-x11-probe.json").read_text())
    assert not report["probe_ok"] and not report["voicepeak_synthesis_verified"]
    assert report["checks"][0]["signal"] == 11 and report["checks"][1]["ok"]
    assert [options["early_threads"] for _, options in calls] == [False, True]
    assert all(config["display"] == ":1" for config, _ in calls)


def test_wrong_architecture_never_starts_guest(probe, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    binary = tmp_path / "wrong-elf"
    binary.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 100)
    monkeypatch.setattr(probe, "BINARY", binary)
    monkeypatch.setattr(probe, "load_config", lambda: {"backend": "box64", "container": "voicepeak-arm64", "display": ""})
    monkeypatch.setattr(probe, "run_probe", lambda *a: pytest.fail("invalid ELF started"))
    monkeypatch.setattr(sys, "argv", ["probe-x11.py"])
    assert probe.main() == 1
    report = json.loads((tmp_path / "voicepeak-box64-x11-probe.json").read_text())
    assert "ELF x86_64" in report["error"] and report["checks"] == []
