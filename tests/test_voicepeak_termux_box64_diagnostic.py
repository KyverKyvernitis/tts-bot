"""Native ARM diagnostics and crash detection for the alternative runtime."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"


@pytest.fixture
def diagnostic(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("voicepeak_box64_diagnostic", TOOLKIT / "diagnostic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "login_command", lambda config, command, **options: ["guest", *command])
    path = tmp_path / "config-box64.json"
    path.write_text(json.dumps({"container": "voicepeak-arm64", "backend": "box64", "display": "",
                                "engine_directory": "", "guest_executable": "/opt/Voicepeak/voicepeak"}))
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(path))
    return module


def fake_guest(diagnostic, monkeypatch, *, architecture="arm64", box_version="Box64 with Dynarec v0.4.0\n",
               help_text="VOICEPEAK usage: -s text --speed value --list-narrator\n", narrator="重音テト\n",
               real_cache_code=0):
    calls = []
    def run(command, timeout, **options):
        calls.append((command, options))
        output = ""
        if "--print-architecture" in command:
            output = architecture + "\n"
        elif "/usr/bin/dpkg-query" in command:
            output = "install ok installed\n"
        elif command[-2:] == ["/sbin/ldconfig.real", "-p"]:
            if real_cache_code:
                return {"ok": False, "code": real_cache_code}
            output = "12 libs found in cache `/etc/ld.so.cache'\n"
        elif command[-2:] == ["/opt/voicepeak-box64/bin/box64", "--version"]:
            assert options["include_stderr"] is True
            output = box_version
        elif "/bin/sh" in command:
            output = "7f 45 4c 46 02 01 01 00 00 00 00 00 00 00 00 00 03 00 3e 00\n"
        elif command[-1] == "--help":
            output = help_text
        elif command[-1] == "--list-narrator":
            output = narrator
        return {"ok": True, "code": 0, "output": output}
    monkeypatch.setattr(diagnostic, "bounded", run)
    return calls


@pytest.mark.parametrize("message", [
    "qemu: uncaught target signal 11 (Segmentation fault) - core dumped",
    "proot info: vpid 1: terminated with signal 11",
])
@pytest.mark.parametrize("include_stderr", [False, True])
def test_signalled_guest_is_not_success_even_when_proot_returns_zero(diagnostic, message, include_stderr):
    command = [sys.executable, "-c", "import sys; print('12 libs found in cache /etc/ld.so.cache'); print(" + repr(message) + ",file=sys.stderr)"]
    result = diagnostic.bounded(command, 3, detect_guest_signals=True, include_stderr=include_stderr)
    assert result["ok"] is False and result["code"] == 0 and result["signal"] == 11
    assert "código de retorno" in result["error"]
    assert "output" not in result


def test_box64_uses_native_arm_utilities_and_wraps_only_x64_engine(diagnostic, monkeypatch):
    calls = fake_guest(diagnostic, monkeypatch)
    result = diagnostic.report(probe_system=True, probe_runtime=True)
    assert result["system_probe"]["system_healthy"] is True
    assert result["system_probe"]["expected_guest_architecture"] == "arm64"
    assert result["system_probe"]["box64_verified"] is True
    assert result["system_probe"]["box64_probe"]["version"] == "0.4.0"
    assert result["guest_architecture"] == "arm64"
    assert result["engine_elf_x86_64"] is True and result["cli_help_ok"] is True
    assert result["engine_verified"] is True
    for command, _ in calls:
        if command[-1] in {"--help", "--list-narrator"}:
            assert command[-3:] == ["/opt/voicepeak-box64/bin/box64", "/opt/Voicepeak/voicepeak", command[-1]]
        elif "/usr/bin/dpkg" in command or "/sbin/ldconfig.real" in command:
            assert "/opt/voicepeak-box64/bin/box64" not in command
    assert not any("/sbin/ldconfig" in command for command, _ in calls)


def test_real_cache_failure_cannot_be_hidden_by_successful_wrapper(diagnostic, monkeypatch):
    calls = fake_guest(diagnostic, monkeypatch, real_cache_code=139)
    result = diagnostic.report(probe_system=True, probe_runtime=True)
    assert result["system_probe"]["ldconfig_cache"] == {"ok": False, "code": 139}
    assert result["system_probe"]["system_healthy"] is False
    assert result["engine_verified"] is False
    assert not any(command[-1] in {"--help", "--list-narrator"} for command, _ in calls)


def test_box64_refuses_x64_guest_for_native_utilities(diagnostic, monkeypatch):
    calls = fake_guest(diagnostic, monkeypatch, architecture="amd64")
    result = diagnostic.report(probe_system=True, probe_runtime=True)
    assert result["system_probe"]["system_healthy"] is False
    assert result["engine_verified"] is False
    assert not any(command[-1] == "--help" for command, _ in calls)


@pytest.mark.parametrize("box_version", ["", "unrecognised wrapper output\n"])
def test_return_zero_without_box64_version_never_launches_engine(diagnostic, monkeypatch, box_version):
    calls = fake_guest(diagnostic, monkeypatch, box_version=box_version)
    result = diagnostic.report(probe_system=True, probe_runtime=True)
    assert result["system_probe"]["system_healthy"] is True
    assert result["system_probe"]["box64_verified"] is False
    assert "Box64" in result["runtime_error"] and result["engine_verified"] is False
    assert not any(command[-1] == "--help" for command, _ in calls)


@pytest.mark.parametrize("help_text", ["", "wrapper finished\n"])
def test_return_zero_without_engine_help_does_not_confirm_teto(diagnostic, monkeypatch, help_text):
    fake_guest(diagnostic, monkeypatch, help_text=help_text)
    result = diagnostic.report(probe_system=True, probe_runtime=True)
    assert result["teto_inventory_ok"] is True
    assert result["cli_help_ok"] is False and result["engine_verified"] is False


def test_native_arm_runtime_requires_real_teto_inventory(diagnostic, monkeypatch):
    fake_guest(diagnostic, monkeypatch, narrator="Other narrator\n")
    result = diagnostic.report(probe_system=True, probe_runtime=True)
    assert result["cli_help_ok"] is True
    assert result["teto_inventory_ok"] is False and result["engine_verified"] is False
