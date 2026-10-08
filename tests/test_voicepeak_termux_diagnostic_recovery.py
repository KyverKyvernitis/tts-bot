"""Read-only diagnostics for an interrupted amd64 guest installation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"


@pytest.fixture
def diagnostic(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("voicepeak_recovery_diagnostic", TOOLKIT / "diagnostic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "login_command", lambda config, command, **kwargs: ["guest", config["container"], *command])
    return module


def fake_system(diagnostic, monkeypatch, *, state="install ok installed", audit="", cache_code=0, scan_code=0, no_aux_code=0,
                cache_output="1 libs found in cache `/etc/ld.so.cache'\nlibc.so.6 => /lib/libc.so.6\n"):
    calls = []
    def execute(command, timeout, **options):
        calls.append((command, timeout, options))
        if "--print-architecture" in command:
            return {"ok": True, "code": 0, "output": "amd64\n"}
        if "dpkg-query" in command[-4]:
            return {"ok": True, "code": 0, "output": state + "\n"}
        if "--audit" in command:
            return {"ok": True, "code": 0, "output": audit}
        code = no_aux_code if "-i" in command else scan_code if "-N" in command else cache_code
        if "-p" in command:
            return {"ok": code == 0, "code": code, "output": cache_output if code == 0 else "private failure"}
        return {"ok": code == 0, "code": code, "output": "internal cache paths and activation token" if code == 0 else "private failure"}
    monkeypatch.setattr(diagnostic, "bounded", execute)
    return calls


def test_missing_config_can_inspect_guest_without_starting_voicepeak(diagnostic, monkeypatch):
    calls = fake_system(diagnostic, monkeypatch)
    result = diagnostic.report(probe_system=True)
    system = result["system_probe"]
    assert "configuration_error" in result
    assert system["container"] == "voicepeak-x64" and system["read_only"] is True
    assert system["guest_architecture"] == "amd64"
    assert system["libc_bin_state"] == "installed" and system["libc_bin_pending"] is False
    assert system["system_healthy"] is True
    assert result["engine_verified"] is False
    assert len(calls) == 8
    for command, _, _ in calls:
        assert "--configure" not in command and "--list-narrator" not in command
        if "/sbin/ldconfig.real" in command:
            assert "-p" in command or {"-N", "-X"}.issubset(command)
    assert "activation token" not in json.dumps(result)
    assert "private failure" not in json.dumps(result)


@pytest.mark.parametrize("state", ["half-installed", "unpacked", "half-configured", "triggers-awaited", "triggers-pending"])
def test_interrupted_libc_postinstall_is_reported_without_repair(diagnostic, monkeypatch, state):
    fake_system(diagnostic, monkeypatch, state="install ok " + state, audit="libc-bin requires configuration\n", scan_code=139)
    result = diagnostic.report(probe_system=True)
    system = result["system_probe"]
    assert system["libc_bin_pending"] is True and system["system_healthy"] is False
    assert system["ldconfig_scan"] == {"ok": False, "code": 139}
    assert system["audit_has_findings"] is True
    assert "requires configuration" not in json.dumps(result)


def test_reinstall_required_is_not_healthy_even_when_package_installed(diagnostic, monkeypatch):
    fake_system(diagnostic, monkeypatch, state="install reinstreq installed")
    system = diagnostic.report(probe_system=True)["system_probe"]
    assert system["libc_bin_reinstall_required"] is True
    assert system["libc_bin_status_ok"] is False
    assert system["system_healthy"] is False


@pytest.mark.parametrize("requested", ["unknown", "deinstall", "purge"])
def test_non_install_action_is_not_healthy(diagnostic, monkeypatch, requested):
    fake_system(diagnostic, monkeypatch, state=requested + " ok installed")
    system = diagnostic.report(probe_system=True)["system_probe"]
    assert system["libc_bin_status_ok"] is False and system["system_healthy"] is False


def test_combined_system_runtime_probe_blocks_app_with_pending_libc(diagnostic, monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"container": "custom-x64", "guest_executable": "/opt/Voicepeak/voicepeak", "display": ""}))
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(config))
    calls = fake_system(diagnostic, monkeypatch, state="install ok half-configured")
    result = diagnostic.report(probe_system=True, probe_runtime=True)
    assert result["system_probe"]["container"] == "custom-x64"
    assert "motor não executado" in result["runtime_error"]
    assert result["engine_verified"] is False
    assert not any("--help" in command or "--list-narrator" in command for command, _, _ in calls)


def test_no_seccomp_and_aux_cache_variants_are_comparisons_without_repair(diagnostic, monkeypatch):
    monkeypatch.delenv("PROOT_NO_SECCOMP", raising=False)
    calls = fake_system(diagnostic, monkeypatch, scan_code=139, no_aux_code=0)
    system = diagnostic.report(probe_system=True)["system_probe"]
    overrides = [(command, options["environment"]) for command, _, options in calls if "environment" in options]
    assert len(overrides) == 2 and overrides[0][0][-2:] == ["/sbin/ldconfig.real", "-p"]
    assert overrides[1][0][-4:] == ["/sbin/ldconfig.real", "-N", "-X", "-i"]
    assert all(environment["PROOT_NO_SECCOMP"] == "1" for _, environment in overrides)
    assert "PROOT_NO_SECCOMP" not in os.environ
    assert system["ldconfig_scan_without_aux_cache"] == {"ok": True, "code": 0}
    assert system["ldconfig_scan_without_aux_cache_no_seccomp"] == {"ok": True, "code": 0}
    assert system["system_healthy"] is False
    assert "não comprova" in system["scan_test_note"]


def test_unknown_status_cannot_approve_guest(diagnostic, monkeypatch):
    fake_system(diagnostic, monkeypatch, state="secret licence state")
    result = diagnostic.report(probe_system=True)
    assert result["system_probe"]["libc_bin_state"] is None
    assert result["system_probe"]["system_healthy"] is False
    assert "secret licence" not in json.dumps(result)


@pytest.mark.parametrize("cache_output", ["", "0 libs found in cache `/etc/ld.so.cache'\n", "unknown cache format\n"])
def test_empty_or_invalid_library_cache_cannot_approve_guest(diagnostic, monkeypatch, cache_output):
    fake_system(diagnostic, monkeypatch, cache_output=cache_output)
    system = diagnostic.report(probe_system=True)["system_probe"]
    assert system["ldconfig_cache"] == {"ok": True, "code": 0}
    assert system["ldconfig_cache_readable"] is False
    assert system["system_healthy"] is False


def test_probe_system_commands_share_total_deadline(diagnostic, monkeypatch):
    calls = fake_system(diagnostic, monkeypatch)
    clock = iter([100.0, 101.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0])
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: next(clock))
    result = diagnostic.report(probe_system=True, timeout=2)
    assert len(calls) == 1 and calls[0][1] == 1
    assert result["system_probe"]["libc_bin_query"] == {"ok": False, "error": "tempo total esgotado"}
    assert result["system_probe"]["system_healthy"] is False


def test_cpu_and_python_platform_are_bounded_metadata(diagnostic, monkeypatch):
    fake_system(diagnostic, monkeypatch)
    monkeypatch.setattr(diagnostic.sys, "platform", "android")
    monkeypatch.setenv("QEMU_CPU", "max,sse4.2=on")
    result = diagnostic.report(probe_system=True)
    assert result["python_platform"] == "android"
    assert result["python_version"]
    assert result["qemu_cpu"] == "max,sse4.2=on"
    monkeypatch.setenv("QEMU_CPU", "secret\nprivate licence")
    assert diagnostic.report(probe_system=True)["qemu_cpu"] == "valor omitido"


def test_missing_config_system_cli_returns_healthy_exit_without_claiming_engine(diagnostic, monkeypatch, capsys):
    fake_system(diagnostic, monkeypatch)
    monkeypatch.setattr(diagnostic.sys, "argv", ["diagnostic.py", "--probe-system", "--timeout", "60"])
    assert diagnostic.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["system_probe"]["system_healthy"] is True and result["engine_verified"] is False


def test_system_help_works_under_isolated_external_python(tmp_path):
    result = subprocess.run([sys.executable, "-I", str(TOOLKIT / "diagnostic.py"), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert "--probe-system" in result.stdout and "--probe-runtime" in result.stdout
