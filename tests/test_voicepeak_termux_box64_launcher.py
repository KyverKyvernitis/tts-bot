"""Box64 launch contracts without requiring Android or a proprietary voice."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


LAUNCHER = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux/launcher.py"


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("voicepeak_box64_launcher", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def environment(tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    temporary = tmp_path / "temporary audio"
    engine = tmp_path / "persistent engine"
    for directory in (binary, temporary, engine):
        directory.mkdir()
    proot = binary / "proot-distro"
    proot.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    proot.chmod(0o700)
    config = {
        "container": "voicepeak-arm64",
        "backend": "box64",
        "guest_executable": "/opt/Voicepeak/voicepeak",
        "engine_directory": str(engine),
        "display": "",
    }
    path = tmp_path / "config-box64.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("PATH", str(binary))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(path))
    monkeypatch.delenv("DISPLAY", raising=False)
    return config, path, binary, temporary, engine


def test_box64_main_preserves_literal_text_output_and_engine_binding(environment, tmp_path):
    _, _, _, temporary, engine = environment
    marker = tmp_path / "must-not-exist"
    text = f'Olá "$(touch {marker})" `comando`; && $TOKEN'
    output = str(temporary / "fala 1.wav")
    result = subprocess.run(
        [sys.executable, "-I", str(LAUNCHER), "-s", text, "-o", output],
        env=dict(os.environ), text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    arguments = json.loads(result.stdout)
    assert arguments[:2] == ["login", "voicepeak-arm64"]
    assert f"{temporary}:{temporary}" in arguments
    assert f"{engine}:/opt/Voicepeak" in arguments
    assert arguments[-6:] == ["/opt/voicepeak-box64/bin/box64", "/opt/Voicepeak/voicepeak", "-s", text, "-o", output]
    assert "BOX64_LOG=0" in arguments and "BOX64_NOBANNER=1" in arguments
    assert not marker.exists()


def test_box64_does_not_require_host_qemu(launcher, environment):
    _, _, binary, _, _ = environment
    assert not (binary / "qemu-x86_64").exists()
    config = launcher.load_config()
    command = launcher.login_command(config, launcher.voicepeak_command(config, ["--help"]))
    assert command[0] == str(binary / "proot-distro")
    assert command[-3:] == [config["guest_box64"], config["guest_executable"], "--help"]


def test_generic_arm_utilities_are_never_wrapped(launcher, environment):
    config = launcher.load_config()
    utility = ["/usr/bin/dpkg", "--print-architecture"]
    command = launcher.login_command(config, utility, gui=False)
    assert command[-2:] == utility
    assert config["guest_box64"] not in command
    assert config["guest_executable"] not in command
    assert launcher.voicepeak_command(config, ["--list-narrator"]).count(config["guest_box64"]) == 1


def test_box64_library_search_path_is_explicit_guest_environment(launcher, environment, monkeypatch):
    config, path, _, _, _ = environment
    config["box64_library_path"] = "/opt/voicepeak-box64/lib/x86_64-linux-gnu:/opt/Voicepeak/lib"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("BOX64_LD_LIBRARY_PATH", "/host-only")
    monkeypatch.setenv("BOX64_LOG", "3")
    loaded = launcher.load_config()
    command = launcher.login_command(loaded, launcher.voicepeak_command(loaded, ["--help"]))
    assert f"BOX64_LD_LIBRARY_PATH={config['box64_library_path']}" in command
    assert all("/host-only" not in item for item in command)
    assert os.environ["BOX64_LD_LIBRARY_PATH"] == "/host-only"
    assert os.environ["BOX64_LOG"] == "3"


def test_default_box64_does_not_invent_x64_search_directory(launcher, environment):
    config = launcher.load_config()
    assert not any(item.startswith("BOX64_LD_LIBRARY_PATH=") for item in launcher.login_command(config, ["/usr/bin/true"]))


def test_box64_shared_x11_tmp_and_environment_display(launcher, environment, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":2.0")
    config = launcher.load_config()
    command = launcher.login_command(config, launcher.voicepeak_command(config, []))
    assert "--shared-tmp" in command and "DISPLAY=:2.0" in command
    headless = launcher.login_command(config, ["/usr/bin/dpkg", "--print-architecture"], gui=False)
    assert "--shared-tmp" not in headless and "DISPLAY=:2.0" not in headless


def test_box64_missing_proot_fails_before_exec(launcher, environment, monkeypatch):
    config = launcher.load_config()
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    with pytest.raises(launcher.ConfigurationError, match="instale proot-distro"):
        launcher.login_command(config, ["/usr/bin/true"])


def test_custom_guest_box64_path_is_literal(launcher, environment):
    config, path, _, _, _ = environment
    config["guest_box64"] = "/opt/Box64 custom/box64"
    path.write_text(json.dumps(config))
    loaded = launcher.load_config()
    assert launcher.voicepeak_command(loaded, ["--help"]) == [config["guest_box64"], config["guest_executable"], "--help"]


@pytest.mark.parametrize("field,value", [
    ("backend", "shell"), ("backend", None), ("backend", ["box64"]),
    ("guest_box64", "box64"), ("guest_box64", "/opt/../bin/box64"),
    ("guest_box64", "/opt/box64\nTOKEN=secret"), ("guest_box64", "/opt/box64\x7f"),
    ("guest_box64", "/opt:box64/bin/box64"), ("guest_box64", None),
    ("box64_library_path", "relative"), ("box64_library_path", "/opt/../lib"),
    ("box64_library_path", "/opt/lib:"), ("box64_library_path", ":/opt/lib"),
    ("box64_library_path", "/opt/lib::/opt/other"),
    ("box64_library_path", "/opt/lib\nLD_PRELOAD=secret"),
    ("box64_library_path", None), ("box64_library_path", "/" + "x" * 4096),
    ("licence", "private token"),
])
def test_invalid_box64_configuration_is_rejected(launcher, environment, field, value):
    config, path, _, _, _ = environment
    config[field] = value
    path.write_text(json.dumps(config))
    with pytest.raises(launcher.ConfigurationError):
        launcher.load_config()


def test_qemu_default_retains_original_config_shape_and_commands(launcher, environment):
    config, path, binary, _, _ = environment
    config.pop("backend")
    config["container"] = "voicepeak-x64"
    path.write_text(json.dumps(config))
    loaded = launcher.load_config()
    assert loaded == config
    assert launcher.voicepeak_command(loaded, ["--help"]) == [config["guest_executable"], "--help"]
    with pytest.raises(launcher.ConfigurationError, match="qemu-user-x86-64"):
        launcher.login_command(loaded, [config["guest_executable"], "--help"])
    qemu = binary / "qemu-x86_64"
    qemu.write_text("#!" + sys.executable + "\n")
    qemu.chmod(0o700)
    command = launcher.login_command(loaded, launcher.voicepeak_command(loaded, ["--help"]))
    assert not any(item.startswith("BOX64_") for item in command)
    assert command[-2:] == [config["guest_executable"], "--help"]


def test_explicit_qemu_backend_does_not_use_box64(launcher, environment):
    config, path, _, _, _ = environment
    config["backend"] = "qemu"
    path.write_text(json.dumps(config))
    loaded = launcher.load_config()
    assert launcher.voicepeak_command(loaded, ["--help"]) == [config["guest_executable"], "--help"]
