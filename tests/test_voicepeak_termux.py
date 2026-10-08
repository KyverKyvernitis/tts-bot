"""Contract tests for the Termux toolkit; no proprietary engine is bundled."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest


TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"


def executable(path, code):
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o700)
    return str(path)


@pytest.fixture
def modules(monkeypatch):
    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, TOOLKIT / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    launcher = load("voicepeak_termux_launcher", "launcher.py")
    monkeypatch.setitem(sys.modules, "launcher", launcher)
    return launcher, load("voicepeak_termux_diagnostic", "diagnostic.py")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    configuration = {"container": "voicepeak-x64", "guest_executable": "/opt/Voicepeak path/voicepeak", "display": ""}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(configuration))
    temporary = tmp_path / "temporary audio"
    temporary.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ("proot-distro", "qemu-x86_64"):
        executable(binary / name, "pass\n")
    monkeypatch.setenv("PATH", str(binary))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(config_path))
    return configuration, temporary, binary, config_path


def test_launcher_preserves_text_and_output_arguments(modules, configured, monkeypatch):
    launcher, _ = modules
    config, temporary, binary, _ = configured
    text = 'Olá "$(touch segredo)" `comando`; && $TOKEN'
    output = str(temporary / "fala 1.wav")
    captured = []
    monkeypatch.setattr(sys, "argv", ["voicepeak-termux", "-s", text, "-o", output])
    monkeypatch.setattr(launcher.os, "execv", lambda program, arguments: captured.append((program, arguments)))
    assert launcher.main() == 0
    assert captured == [(str(binary / "proot-distro"), [str(binary / "proot-distro"), "login", "voicepeak-x64", "--bind", f"{temporary}:{temporary}", "--", "/usr/bin/env", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", config["guest_executable"], "-s", text, "-o", output])]


def test_display_enables_shared_x11_temporary_directory(modules, configured):
    launcher, _ = modules
    config, _, _, _ = configured
    config["display"] = ":1"
    command = launcher.login_command(config, [config["guest_executable"]])
    assert "--shared-tmp" in command
    assert "DISPLAY=:1" in command
    assert "DISPLAY=:1" not in launcher.login_command(config, ["--help"], gui=False)


def test_display_environment_and_persistent_engine_binding(modules, configured, tmp_path, monkeypatch):
    launcher, _ = modules
    config, _, _, config_path = configured
    engine = tmp_path / "persistent engine"
    config["engine_directory"] = str(engine)
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("DISPLAY", ":2.0")
    loaded = launcher.load_config()
    assert loaded["display"] == ":2.0"
    assert f"{engine}:/opt/Voicepeak" not in launcher.login_command(loaded, ["--help"])
    engine.mkdir()
    command = launcher.login_command(loaded, ["--help"])
    assert f"{engine}:/opt/Voicepeak" in command and "DISPLAY=:2.0" in command


def test_missing_configuration_fails_clearly(modules, tmp_path, monkeypatch, capsys):
    launcher, _ = modules
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(tmp_path / "missing"))
    assert launcher.main() == 2
    assert "execute setup.sh" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [("container", "--isolated"), ("guest_executable", "/opt/../bin/sh"), ("display", ":1\nTOKEN=secret"), ("token", "secret")])
def test_configuration_rejects_unsafe_or_licence_fields(modules, configured, field, value):
    launcher, _ = modules
    config, _, _, config_path = configured
    config[field] = value
    config_path.write_text(json.dumps(config))
    with pytest.raises(launcher.ConfigurationError):
        launcher.load_config()


def test_missing_emulator_fails_before_execution(modules, configured):
    launcher, _ = modules
    config, _, binary, _ = configured
    (binary / "qemu-x86_64").unlink()
    with pytest.raises(launcher.ConfigurationError, match="qemu-user-x86-64"):
        launcher.login_command(config, [config["guest_executable"]])


def test_diagnostic_default_never_claims_engine_ready(modules, configured, monkeypatch):
    _, diagnostic = modules
    calls = []
    monkeypatch.setattr(diagnostic, "bounded", lambda command, timeout, **kwargs: calls.append(command) or {"ok": True, "output": "QEMU version test\n"})
    result = diagnostic.report()
    assert result["engine_verified"] is False
    assert result["qemu_installed"] is True
    assert all("proot-distro" not in Path(command[0]).name for command in calls)


def test_diagnostic_missing_engine_stops_before_vendor_commands(modules, configured, monkeypatch):
    _, diagnostic = modules
    calls = []
    def run(command, timeout, **kwargs):
        calls.append(command)
        if "--print-architecture" in command:
            return {"ok": True, "output": "amd64\n"}
        return {"ok": False, "output": ""}
    monkeypatch.setattr(diagnostic, "bounded", run)
    result = diagnostic.report(probe_runtime=True)
    assert result["engine_verified"] is False
    assert "ausente" in result["runtime_error"]
    assert not any("--list-narrator" in command for command in calls)


def test_diagnostic_help_does_not_prove_teto_activation(modules, configured, monkeypatch):
    _, diagnostic = modules
    def run(command, timeout, **kwargs):
        if "--print-architecture" in command:
            return {"ok": True, "output": "amd64\n"}
        if "/bin/sh" in command:
            return {"ok": True, "output": "7f 45 4c 46 02 01 01 00 00 00 00 00 00 00 00 00 03 00 3e 00\n"}
        if "--list-narrator" in command:
            return {"ok": False, "error": "tempo esgotado"}
        return {"ok": True, "output": "VOICEPEAK help: -s text --speed value --list-narrator\n"}
    monkeypatch.setattr(diagnostic, "bounded", run)
    result = diagnostic.report(probe_runtime=True)
    assert result["cli_help_ok"] is True
    assert result["engine_verified"] is False
    assert "GUI" in result["note"]


def test_diagnostic_deadline_and_hidden_vendor_output(modules, configured, monkeypatch):
    _, diagnostic = modules
    monkeypatch.setattr(diagnostic.shutil, "which", lambda name: None if name == "qemu-x86_64" else "/fake/proot-distro")
    clock = iter([100.0, 101.0, 103.0])
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(diagnostic, "login_command", lambda config, command, **kwargs: command)
    calls = []
    monkeypatch.setattr(diagnostic, "bounded", lambda command, timeout, **kwargs: calls.append(timeout) or {"ok": True, "output": "amd64\n"})
    result = diagnostic.report(probe_runtime=True, timeout=2)
    assert calls == [1.0]
    assert result["engine_verified"] is False


def test_bounded_diagnostic_reaps_timed_out_process(modules, tmp_path):
    _, diagnostic = modules
    slow = executable(tmp_path / "slow-command", "import time\ntime.sleep(10)\n")
    assert diagnostic.bounded([slow], 0.05) == {"ok": False, "error": "tempo esgotado"}


def test_bounded_diagnostic_caps_output_and_discards_stderr(modules, tmp_path):
    _, diagnostic = modules
    noisy = executable(tmp_path / "noisy-command", "import sys\nprint('secret activation data',file=sys.stderr)\nprint('x'*65537)\n")
    result = diagnostic.bounded([noisy], 2)
    assert result == {"ok": False, "error": "saída excede limite"}


def test_stderr_inventory_is_recognized_without_publishing_licence_text(modules, configured, tmp_path, monkeypatch):
    _, diagnostic = modules
    vendor = executable(tmp_path / "stderr-vendor", "import sys\na=sys.argv\nif '--print-architecture' in a: print('amd64')\nelif '/bin/sh' in a: print('7f 45 4c 46 02 01 01 00 00 00 00 00 00 00 00 00 03 00 3e 00')\nelif '--help' in a: print('VOICEPEAK help: -s text --speed value --list-narrator; private activation token',file=sys.stderr)\nelif '--list-narrator' in a: print('private activation token\\n重音テト',file=sys.stderr)\n")
    monkeypatch.setattr(diagnostic, "login_command", lambda config, command, **kwargs: [vendor, *command])
    result = diagnostic.report(probe_runtime=True)
    assert result["cli_help_ok"] is True and result["teto_inventory_ok"] is True
    assert result["engine_verified"] is True
    assert "private activation token" not in json.dumps(result)
    assert "teste WAV real" in result["note"]


def test_failed_stderr_cli_never_returns_vendor_errors(modules, tmp_path):
    _, diagnostic = modules
    vendor = executable(tmp_path / "failed-vendor", "import sys\nprint('private activation token',file=sys.stderr)\nsys.exit(7)\n")
    assert diagnostic.bounded([vendor], 2, include_stderr=True) == {"ok": False, "code": 7}


def test_combined_streams_share_output_limit(modules, tmp_path):
    _, diagnostic = modules
    vendor = executable(tmp_path / "combined-vendor", "import sys\nprint('x'*33000)\nprint('y'*33000,file=sys.stderr)\n")
    assert diagnostic.bounded([vendor], 2, include_stderr=True) == {"ok": False, "error": "saída excede limite"}


def test_bounded_diagnostic_timeout_stops_child_heartbeat(modules, tmp_path):
    _, diagnostic = modules
    heartbeat, child_pid = tmp_path / "heartbeat", tmp_path / "child.pid"
    child = "import time\nf=open(" + repr(str(heartbeat)) + ",'a',buffering=1)\nwhile True:\n f.write('beat\\n')\n time.sleep(0.01)\n"
    parent = executable(tmp_path / "fake-proot", "import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c'," + repr(child) + "])\nPath(" + repr(str(child_pid)) + ").write_text(str(p.pid))\ntime.sleep(10)\n")
    try:
        assert diagnostic.bounded([parent], 0.5) == {"ok": False, "error": "tempo esgotado"}
        assert heartbeat.exists()
        size = heartbeat.stat().st_size
        time.sleep(0.1)
        assert heartbeat.stat().st_size == size
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture
def setup_environment(tmp_path):
    home = tmp_path / "home"
    prefix = tmp_path / "usr"
    fakebin = tmp_path / "fakebin"
    for path in (home, prefix, fakebin):
        path.mkdir()
    log = tmp_path / "commands.jsonl"
    executable(fakebin / "uname", "print('aarch64')\n")
    executable(fakebin / "pkg", "import json,os,sys\nwith open(os.environ['TERMUX_TEST_LOG'],'a') as f: f.write(json.dumps(['pkg']+sys.argv[1:])+'\\n')\n")
    executable(fakebin / "proot-distro", """import json, os, sys
from pathlib import Path
a = sys.argv[1:]
with open(os.environ['TERMUX_TEST_LOG'], 'a') as f:
    f.write(json.dumps(['proot-distro'] + a) + '\\n')
if a == ['install', '--help']:
    print('--architecture' if os.environ.get('FAKE_OLD_PROOT') != '1' else 'old install usage')
elif '--print-architecture' in a:
    print(os.environ.get('FAKE_GUEST_ARCH', 'amd64'))
elif a and a[0] == 'install':
    (Path(os.environ['PREFIX']) / 'var/lib/proot-distro/containers' / a[a.index('--name') + 1] / 'rootfs').mkdir(parents=True)
elif '/usr/bin/apt-get' in a:
    if not (Path.home() / '.voicepeak-termux/config.json').is_file():
        sys.exit(88)
    if os.environ.get('FAKE_APT_FAILURE') in a:
        print('libc-bin trigger: qemu signal 11', file=sys.stderr)
        sys.exit(1)
""")
    env = dict(os.environ, HOME=str(home), PREFIX=str(prefix), TERMUX_VERSION="test", TERMUX_TEST_LOG=str(log), PATH=str(fakebin) + os.pathsep + os.environ["PATH"])
    env.pop("VOICEPEAK_TERMUX_CONFIG", None)
    return home, prefix, log, env


def run_setup(env, *arguments):
    return subprocess.run(["bash", str(TOOLKIT / "setup.sh"), *arguments], env=env, text=True, capture_output=True, timeout=20)


def test_setup_new_container_and_native_interpreter(setup_environment):
    home, _, log, env = setup_environment
    result = run_setup(env)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["proot-distro", "install", "ubuntu:22.04", "--architecture", "x86_64", "--name", "voicepeak-x64"] in calls
    assert not any("upgrade" in command or "remove" in command or "reset" in command for command in calls)
    config = json.loads((home / ".voicepeak-termux/config.json").read_text())
    assert config["guest_executable"] == "/opt/Voicepeak/voicepeak"
    assert (home / ".voicepeak-termux/bin/launcher.py").read_text().splitlines()[0] == "#!" + shutil.which("python", path=env["PATH"])


def test_setup_preserves_existing_container_and_configuration(setup_environment):
    home, prefix, log, env = setup_environment
    container = prefix / "var/lib/proot-distro/containers/custom-x64/rootfs"
    container.mkdir(parents=True)
    marker = container / "licensed-user-settings"
    marker.write_text("keep")
    config = home / ".voicepeak-termux/config.json"
    config.parent.mkdir()
    original = json.dumps({"container": "custom-x64", "guest_executable": "/opt/My Voicepeak/voicepeak", "display": ":1"})
    config.write_text(original)
    result = run_setup(env)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(command[:2] == ["proot-distro", "install"] and "--help" not in command for command in calls)
    assert config.read_text() == original and marker.read_text() == "keep"


def test_setup_preserves_foreign_alias(setup_environment):
    home, _, _, env = setup_environment
    alias = home / ".voicepeak-termux/bin/voicepeak-termux"
    alias.parent.mkdir(parents=True)
    alias.write_text("foreign command")
    result = run_setup(env)
    assert result.returncode != 0
    assert alias.read_text() == "foreign command"


@pytest.mark.parametrize("variable,value", [("FAKE_GUEST_ARCH", "arm64"), ("FAKE_OLD_PROOT", "1")])
def test_setup_refuses_wrong_architecture_or_legacy_plugins(setup_environment, variable, value):
    home, _, log, env = setup_environment
    env[variable] = value
    result = run_setup(env)
    assert result.returncode == 2
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("/usr/bin/apt-get" in command for command in calls)
    assert not (home / ".voicepeak-termux/config.json").exists()


@pytest.mark.parametrize("failed_step", ["update", "install"])
def test_setup_keeps_diagnostics_after_failed_guest_packages(setup_environment, failed_step):
    home, prefix, _, env = setup_environment
    env["FAKE_APT_FAILURE"] = failed_step
    marker = prefix / "var/lib/proot-distro/containers/voicepeak-x64/rootfs/licensed-settings"
    marker.parent.mkdir(parents=True)
    marker.write_text("preserve")
    result = run_setup(env)
    assert result.returncode == 1
    assert "--probe-system" in result.stderr
    assert "Runtime preparado" not in result.stdout
    assert (home / ".voicepeak-termux/config.json").is_file()
    assert (home / ".voicepeak-termux/bin/voicepeak-termux-diagnostic").is_file()
    assert marker.read_text() == "preserve"


def test_setup_prepare_only_resumes_without_running_guest_package_manager(setup_environment):
    home, prefix, log, env = setup_environment
    (prefix / "var/lib/proot-distro/containers/voicepeak-x64/rootfs").mkdir(parents=True)
    env["FAKE_APT_FAILURE"] = "install"
    result = run_setup(env, "--prepare-only")
    assert result.returncode == 0, result.stderr
    assert "ainda não verificadas" in result.stdout
    assert (home / ".voicepeak-termux/config.json").is_file()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("/usr/bin/apt-get" in command or "/usr/bin/dpkg" in command and "--configure" in command for command in calls)
    assert not any(command[:2] == ["proot-distro", "install"] and "--help" not in command for command in calls)


def test_setup_rejects_unknown_option_before_installing_any_package(setup_environment):
    _, _, log, env = setup_environment
    result = run_setup(env, "--reset")
    assert result.returncode == 2
    assert not log.exists()


def test_diagnostic_help_works_from_external_directory_with_isolated_python(tmp_path):
    result = subprocess.run([sys.executable, '-I', str(TOOLKIT / 'diagnostic.py'), '--help'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert '--probe-runtime' in result.stdout

