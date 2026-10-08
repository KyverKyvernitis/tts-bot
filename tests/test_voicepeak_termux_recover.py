"""Opt-in, bounded Termux Ubuntu recovery without launching the engine."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"


@pytest.fixture
def recovery(monkeypatch):
    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, TOOLKIT / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    launcher = load("voicepeak_recovery_launcher_test", "launcher.py")
    monkeypatch.setitem(sys.modules, "launcher", launcher)
    module = load("voicepeak_recovery_test", "recover-runtime.py")
    monkeypatch.setenv("TERMUX_VERSION", "test")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    return module


@pytest.fixture
def configured(tmp_path, monkeypatch):
    config = {"container": "custom-voicepeak", "guest_executable": "/opt/Voicepeak/voicepeak", "display": "", "engine_directory": ""}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(path))
    return config, path


def stub_guest(recovery, monkeypatch, *, architecture="amd64", version_ok=True, scan_ok=True, alternate_ok=True,
               auxiliary=True, backup_ok=True, rebuild_ok=True, recheck_ok=True, dpkg_ok=True,
               package="install ok installed", audit="", cache="42 libs found in cache `/etc/ld.so.cache'\n",
               fail_final=None):
    commands = []
    scans = 0
    def run(command, deadline):
        nonlocal scans
        commands.append(command)
        if command == ["/usr/bin/dpkg", "--print-architecture"]:
            return {"ok": True, "code": 0, "output": architecture + "\n"}
        if command == ["/sbin/ldconfig", "--version"]:
            return {"ok": True, "code": 0, "output": "glibc version; private token"} if version_ok else {"ok": False, "code": 139}
        if command == ["/sbin/ldconfig", "-N", "-X"]:
            scans += 1
            ok = scan_ok if scans == 1 else recheck_ok
            return {"ok": ok, "code": 0 if ok else 139, "output": "private scan token"}
        if command == ["/sbin/ldconfig", "-N", "-X", "-i"]:
            return {"ok": alternate_ok, "code": 0 if alternate_ok else 139, "output": "private alternate token"}
        if command == ["/usr/bin/test", "-f", recovery.AUX_CACHE]:
            return {"ok": auxiliary, "code": 0 if auxiliary else 1}
        if command[0] == "/bin/cp":
            return {"ok": backup_ok, "code": 0 if backup_ok else 1}
        if command in (["/sbin/ldconfig"], ["/sbin/ldconfig", "-i"]):
            return {"ok": rebuild_ok, "code": 0 if rebuild_ok else 139}
        if command == ["/usr/bin/dpkg", "--configure", "-a"]:
            return {"ok": dpkg_ok, "code": 0 if dpkg_ok else 1, "output": "private dpkg diagnostic"}
        if command == ["/usr/bin/dpkg-query", "-W", "-f=${Status}\\n", "libc-bin"]:
            return {"ok": fail_final != "package", "code": 1 if fail_final == "package" else 0, "output": package + "\n"}
        if command == ["/usr/bin/dpkg", "--audit"]:
            return {"ok": fail_final != "audit", "code": 1 if fail_final == "audit" else 0, "output": audit}
        if command == ["/sbin/ldconfig", "-p"]:
            return {"ok": fail_final != "cache", "code": 1 if fail_final == "cache" else 0, "output": cache}
        pytest.fail("Unexpected guest command: " + repr(command))
    monkeypatch.setattr(recovery, "login_command", lambda config, command, *, gui: command if gui is False else pytest.fail("GUI must remain off"))
    monkeypatch.setattr(recovery, "bounded", run)
    return commands


def test_normal_recovery_rebuilds_rechecks_and_configures_existing_guest(recovery, configured, monkeypatch):
    commands = stub_guest(recovery, monkeypatch)
    before = configured[1].read_bytes()
    result = recovery.recover()
    assert result["recovered"] is True and result["engine_verified"] is False
    assert result["strategy"] == "normal_rebuild"
    assert commands == [
        ["/usr/bin/dpkg", "--print-architecture"], ["/sbin/ldconfig", "--version"],
        ["/sbin/ldconfig", "-N", "-X"], ["/sbin/ldconfig"], ["/sbin/ldconfig", "-N", "-X"],
        ["/usr/bin/dpkg", "--configure", "-a"], ["/usr/bin/dpkg-query", "-W", "-f=${Status}\\n", "libc-bin"],
        ["/usr/bin/dpkg", "--audit"], ["/sbin/ldconfig", "-p"],
    ]
    assert configured[1].read_bytes() == before
    assert "private" not in json.dumps(result)
    assert all(not any(item in {"/bin/sh", "--list-narrator", "--help"} or item.endswith("/voicepeak") for item in command) for command in commands)


@pytest.mark.parametrize("auxiliary", [True, False])
def test_ignore_auxiliary_route_backs_up_without_replacing_then_rebuilds(recovery, configured, monkeypatch, auxiliary):
    commands = stub_guest(recovery, monkeypatch, scan_ok=False, auxiliary=auxiliary)
    result = recovery.recover()
    assert result["recovered"] is True
    assert result["strategy"] == "rebuild_ignoring_auxiliary_cache"
    assert commands[:4] == [["/usr/bin/dpkg", "--print-architecture"], ["/sbin/ldconfig", "--version"], ["/sbin/ldconfig", "-N", "-X"], ["/sbin/ldconfig", "-N", "-X", "-i"]]
    backup = ["/bin/cp", "-p", "--no-clobber", "--", recovery.AUX_CACHE, recovery.AUX_BACKUP]
    assert (backup in commands) is auxiliary
    rebuild_index = commands.index(["/sbin/ldconfig", "-i"])
    assert commands[rebuild_index + 1] == ["/sbin/ldconfig", "-N", "-X"]
    assert commands[rebuild_index + 2] == ["/usr/bin/dpkg", "--configure", "-a"]


def test_two_failed_read_only_scans_never_mutate(recovery, configured, monkeypatch):
    commands = stub_guest(recovery, monkeypatch, scan_ok=False, alternate_ok=False)
    result = recovery.recover()
    assert result["recovered"] is False
    assert result["repair_steps"] == {}
    assert len(commands) == 4
    assert "nenhum reparo" in result["error"]
    assert "QEMU/PRoot" in result["hint"]


@pytest.mark.parametrize("architecture", ["arm64", "i386", "amd64; touch /tmp/secret", ""])
def test_wrong_or_unrecognized_guest_architecture_stops_before_mutations(recovery, configured, monkeypatch, architecture):
    commands = stub_guest(recovery, monkeypatch, architecture=architecture)
    result = recovery.recover()
    assert result["recovered"] is False and result["repair_steps"] == {}
    assert commands == [["/usr/bin/dpkg", "--print-architecture"]]


def test_ldconfig_version_failure_stops_before_scans_and_mutations(recovery, configured, monkeypatch):
    commands = stub_guest(recovery, monkeypatch, version_ok=False)
    result = recovery.recover()
    assert result["recovered"] is False and result["repair_steps"] == {}
    assert len(commands) == 2


@pytest.mark.parametrize("stage", ["backup", "rebuild", "recheck", "dpkg"])
def test_failed_repair_step_is_never_masked(recovery, configured, monkeypatch, stage):
    options = {"scan_ok": False}
    options[{"backup": "backup_ok", "rebuild": "rebuild_ok", "recheck": "recheck_ok", "dpkg": "dpkg_ok"}[stage]] = False
    commands = stub_guest(recovery, monkeypatch, **options)
    result = recovery.recover()
    assert result["recovered"] is False
    if stage != "dpkg":
        assert ["/usr/bin/dpkg", "--configure", "-a"] not in commands
    else:
        assert result["repair_steps"]["dpkg_configure"]["code"] == 1
        assert not any(command[0] == "/usr/bin/dpkg-query" for command in commands)


@pytest.mark.parametrize("field,value", [
    ("package", "install ok half-configured"), ("package", "install reinstreq installed"),
    ("audit", "libc-bin requires configuration; private package data"),
    ("cache", ""), ("cache", "0 libs found in cache `/etc/ld.so.cache'\n"),
])
def test_final_verification_failure_never_claims_recovered(recovery, configured, monkeypatch, field, value):
    stub_guest(recovery, monkeypatch, **{field: value})
    result = recovery.recover()
    assert result["recovered"] is False
    assert "verificação final" in result["error"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("stage", ["package", "audit", "cache"])
def test_final_command_failure_prevents_recovered(recovery, configured, monkeypatch, stage):
    stub_guest(recovery, monkeypatch, fail_final=stage)
    assert recovery.recover()["recovered"] is False


def test_held_but_fully_installed_libc_is_accepted(recovery, configured, monkeypatch):
    stub_guest(recovery, monkeypatch, package="hold ok installed")
    assert recovery.recover()["recovered"] is True


@pytest.mark.parametrize("missing", ["TERMUX_VERSION", "PREFIX", "native_architecture"])
def test_native_termux_guard_never_opens_guest(recovery, configured, monkeypatch, missing):
    if missing == "native_architecture":
        monkeypatch.setattr(recovery.platform, "machine", lambda: "x86_64")
    else:
        monkeypatch.delenv(missing)
    monkeypatch.setattr(recovery, "load_config", lambda: pytest.fail("Native guard must run first"))
    assert recovery.recover()["recovered"] is False


def test_missing_configuration_returns_failure_before_guest_execution(recovery, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(tmp_path / "missing"))
    monkeypatch.setattr(recovery, "bounded", lambda *args: pytest.fail("Missing configuration must stop"))
    assert recovery.main(["--repair"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["recovered"] is False and "configuração" in result["error"]


def test_missing_runtime_tools_reports_metadata_only(recovery, configured, monkeypatch):
    monkeypatch.setattr(sys.modules["launcher"].shutil, "which", lambda name: None)
    result = recovery.recover()
    assert result["recovered"] is False
    assert "qemu-user-x86-64" in result["checks"]["architecture"]["error"]


def test_existing_configuration_metacharacters_remain_literal_argv(recovery, configured, tmp_path, monkeypatch):
    configuration, path = configured
    engine = tmp_path / "engine $(touch secret); `secret`"
    engine.mkdir()
    configuration["engine_directory"] = str(engine)
    configuration["guest_executable"] = "/opt/$(touch secret); `secret`/voicepeak"
    path.write_text(json.dumps(configuration))
    temporary = tmp_path / "tmp $(secret)"
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(sys.modules["launcher"].shutil, "which", lambda name: "/native/" + name)
    seen = []
    monkeypatch.setattr(recovery, "bounded", lambda command, deadline: seen.append(command) or {"ok": False, "code": 139})
    assert recovery.recover()["recovered"] is False
    assert seen == [["/native/proot-distro", "login", "custom-voicepeak", "--bind", f"{temporary}:{temporary}", "--bind", f"{engine}:/opt/Voicepeak", "--", "/usr/bin/env", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "/usr/bin/dpkg", "--print-architecture"]]
    assert "/bin/sh" not in seen[0] and configuration["guest_executable"] not in seen[0]


def test_no_repair_flag_never_loads_configuration(recovery, monkeypatch, capsys):
    monkeypatch.setattr(recovery, "recover", lambda **kwargs: pytest.fail("Repair must be explicit"))
    assert recovery.main([]) == 2
    assert json.loads(capsys.readouterr().out)["recovered"] is False


@pytest.mark.parametrize("timeout", ["0", "301", "nan", "inf"])
def test_cli_rejects_timeout_outside_shared_budget(recovery, monkeypatch, capsys, timeout):
    monkeypatch.setattr(recovery, "recover", lambda **kwargs: pytest.fail("Invalid timeout must stop before repair"))
    assert recovery.main(["--repair", "--timeout", timeout]) == 2
    assert "timeout" in json.loads(capsys.readouterr().out)["error"]


def test_one_deadline_is_shared_and_expired_commands_never_spawn(recovery, configured, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(recovery, "login_command", lambda config, command, *, gui: command)
    calls = []
    def run(command, deadline):
        calls.append((command, deadline))
        clock[0] += 0.25
        return {"ok": True, "output": "amd64\n" if "--print-architecture" in command else ""}
    monkeypatch.setattr(recovery, "bounded", run)
    result = recovery.recover(timeout=1)
    assert result["recovered"] is False
    assert len(calls) == 4 and {deadline for command, deadline in calls} == {101.0}
    assert result["checks"]["ldconfig_scan_after_rebuild"]["error"] == "tempo total esgotado"
    assert all("--configure" not in command for command, deadline in calls)


def test_bounded_deadline_expiry_never_spawns(recovery, monkeypatch):
    monkeypatch.setattr(recovery.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Expired budget must not spawn"))
    assert recovery.bounded(["unused"], recovery.time.monotonic() - 1) == {"ok": False, "error": "tempo total esgotado"}


def test_bounded_output_is_capped_without_leaking_stderr(recovery):
    command = [sys.executable, "-c", "import sys; print('private token',file=sys.stderr); print('x'*65537)"]
    assert recovery.bounded(command, time.monotonic() + 3) == {"ok": False, "error": "saída excede limite"}


def test_failed_command_hides_output_and_keeps_exit_code(recovery):
    command = [sys.executable, "-c", "import sys; print('private token'); print('private token',file=sys.stderr); sys.exit(139)"]
    assert recovery.bounded(command, time.monotonic() + 3) == {"ok": False, "code": 139}


def test_missing_command_is_controlled_json_metadata(recovery):
    assert recovery.bounded(["/missing-recovery-command"], time.monotonic() + 3) == {"ok": False, "error": "comando indisponível"}


def test_timeout_kills_and_reaps_process_group(recovery, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    child_code = "import time\nf=open(" + repr(str(heartbeat)) + ",'a',buffering=1)\nwhile True:\n f.write('beat\\n')\n time.sleep(0.01)\n"
    parent_code = "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c'," + repr(child_code) + "])\ntime.sleep(30)\n"
    result = recovery.bounded([sys.executable, "-c", parent_code], time.monotonic() + 0.4)
    assert result == {"ok": False, "error": "tempo total esgotado"}
    before = heartbeat.read_bytes()
    time.sleep(0.08)
    assert heartbeat.read_bytes() == before


def test_cli_exit_tracks_final_recovery(recovery, monkeypatch, capsys):
    monkeypatch.setattr(recovery, "recover", lambda **kwargs: {"recovered": True, "engine_verified": False})
    assert recovery.main(["--repair", "--timeout", "180"]) == 0
    assert json.loads(capsys.readouterr().out)["engine_verified"] is False
    monkeypatch.setattr(recovery, "recover", lambda **kwargs: {"recovered": False})
    assert recovery.main(["--repair"]) == 1


def test_isolated_python_import_from_external_directory(tmp_path):
    result = subprocess.run([sys.executable, "-I", str(TOOLKIT / "recover-runtime.py"), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert "--repair" in result.stdout and "--timeout" in result.stdout
