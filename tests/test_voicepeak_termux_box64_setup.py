"""Exercise the native ARM64 setup with a fake guest, without commercial assets."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"
COMMIT = "dae0917c47b4edd8956f314210417a20fd225c4b"


def executable(path, body):
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o700)


@pytest.fixture
def prepared(tmp_path):
    home, prefix, fakebin, guest = [tmp_path / name for name in ("home", "usr", "fakebin", "guest")]
    for path in (home, prefix, fakebin, guest):
        path.mkdir()
    log = tmp_path / "calls.jsonl"
    common = """import json, os, sys
from pathlib import Path
def record(name, args):
    with open(os.environ['BOX64_TEST_LOG'], 'a') as stream:
        stream.write(json.dumps([name] + args) + '\\n')
a = sys.argv[1:]
"""
    executable(fakebin / "uname", "import os\nprint(os.environ.get('FAKE_NATIVE_ARCH', 'aarch64'))\n")
    executable(fakebin / "pkg", common + "record('pkg', a)\n")
    executable(fakebin / "git", common + """record('git', a)
if a[0] == 'clone':
    destination = Path(a[-1])
    (destination / '.git').mkdir(parents=True)
    (destination / 'x64lib').mkdir()
    header = b'\\x7fELF\\x02\\x01' + b'\\0' * 12 + b'\\x3e\\0'
    if os.environ.get('FAKE_INVALID_LIBRARY') == '1':
        header = b'not an ELF library'
    for name in ('libstdc++.so.6', 'libgcc_s.so.1'):
        (destination / 'x64lib' / name).write_bytes(header)
elif 'rev-parse' in a:
    print('wrong' if os.environ.get('FAKE_WRONG_COMMIT') == '1' else 'dae0917c47b4edd8956f314210417a20fd225c4b')
elif 'status' in a:
    if os.environ.get('FAKE_DIRTY_SOURCE') == '1':
        print(' M src/user-change.c')
else:
    sys.exit(90)
""")
    executable(fakebin / "cmake", common + """record('cmake', a)
if os.environ.get('FAKE_CMAKE_FAILURE') == ('build' if '--build' in a else 'configure'):
    print('compiler failed', file=sys.stderr)
    sys.exit(1)
if '--build' in a:
    build = Path(a[a.index('--build') + 1])
    build.mkdir(parents=True, exist_ok=True)
    binary = build / 'box64'
    binary.write_text('#!' + sys.executable + '\\nimport os,sys\\nprint("" if os.environ.get("FAKE_EMPTY_VERSION")=="1" else "Box64 with Dynarec v0.4.0")\\nif os.environ.get("FAKE_SIGNALLED_VERSION")=="1": print("proot info: vpid 1: terminated with signal 11",file=sys.stderr)\\nsys.exit(1 if os.environ.get("FAKE_BOX64_FAILURE")=="1" else 0)\\n')
    binary.chmod(0o700)
else:
    Path(a[a.index('-B') + 1]).mkdir(parents=True, exist_ok=True)
""")
    executable(fakebin / "proot-distro", common + """import subprocess
record('proot-distro', a)
if a == ['install', '--help']:
    print('old' if os.environ.get('FAKE_OLD_PROOT') == '1' else '--architecture')
elif a[0] == 'install':
    (Path(os.environ['PREFIX']) / 'var/lib/proot-distro/containers' / a[a.index('--name') + 1] / 'rootfs').mkdir(parents=True)
elif '--print-architecture' in a:
    print(os.environ.get('FAKE_GUEST_ARCH', 'arm64'))
elif 'GNU_LIBC_VERSION' in a:
    print(os.environ.get('FAKE_GLIBC', 'glibc 2.39'))
elif '/usr/bin/apt-get' in a:
    if os.environ.get('FAKE_APT_FAILURE') in a:
        sys.exit(1)
elif '/bin/bash' in a:
    script = sys.stdin.read()
    record('guest-script', [script])
    script = script.replace('/opt/voicepeak-box64', os.environ['BOX64_TEST_GUEST'] + '/opt/voicepeak-box64')
    environment = dict(os.environ)
    environment['PATH'] = os.environ['BOX64_TEST_BIN'] + os.pathsep + os.environ['PATH']
    p = subprocess.run(['bash', '-s', '--', a[-1]], input=script, text=True, env=environment)
    sys.exit(p.returncode)
elif '--version' in a:
    binary = Path(os.environ['BOX64_TEST_GUEST']) / 'opt/voicepeak-box64/bin/box64'
    sys.exit(subprocess.run([str(binary), '--version']).returncode)
else:
    sys.exit(91)
""")
    env = dict(os.environ, HOME=str(home), PREFIX=str(prefix), TERMUX_VERSION="test",
               PATH=str(fakebin) + os.pathsep + os.environ["PATH"], BOX64_TEST_LOG=str(log),
               BOX64_TEST_GUEST=str(guest), BOX64_TEST_BIN=str(fakebin))
    env.pop("VOICEPEAK_TERMUX_CONFIG", None)
    return home, prefix, log, guest, env


def run_setup(env, *args):
    return subprocess.run(["bash", str(TOOLKIT / "setup-box64.sh"), *args], env=env,
                          capture_output=True, text=True, timeout=20)


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_new_container_builds_pinned_official_runtime_and_libraries(prepared):
    home, _, log, guest, env = prepared
    result = run_setup(env)
    assert result.returncode == 0, result.stderr
    commands = calls(log)
    assert ["proot-distro", "install", "ubuntu:24.04", "--architecture", "aarch64", "--name", "voicepeak-arm64"] in commands
    dependencies = next(command for command in commands if '/usr/bin/apt-get' in command and 'install' in command)
    assert 'libcurl4t64' in dependencies and 'libasound2t64' in dependencies
    clone = next(command for command in commands if command[:2] == ["git", "clone"])
    assert clone[1:6] == ["clone", "--depth", "1", "--branch", "v0.4.0"]
    assert "https://github.com/ptitSeb/box64.git" in clone
    configure = next(command for command in commands if command[0] == "cmake" and "-S" in command)
    assert set(("-DARM64=1", "-DARM_DYNAREC=ON", "-DBAD_SIGNAL=ON", "-DCMAKE_C_COMPILER=gcc", "-DCMAKE_BUILD_TYPE=RelWithDebInfo")) <= set(configure)
    build = next(command for command in commands if command[:2] == ["cmake", "--build"])
    assert build[-2:] == ["--parallel", "2"]
    script = next(command[1] for command in commands if command[0] == "guest-script")
    assert COMMIT in next(command[-1] for command in commands if "/bin/bash" in command)
    assert "make install" not in script and "binfmt" not in script
    assert (guest / "opt/voicepeak-box64/bin/box64").is_file()
    for filename in ("libstdc++.so.6", "libgcc_s.so.1"):
        assert (guest / "opt/voicepeak-box64/lib/x86_64-linux-gnu" / filename).read_bytes()[18:20] == b"\x3e\0"
    config = json.loads((home / ".voicepeak-termux/config-box64.json").read_text())
    assert config["backend"] == "box64"
    assert config["guest_box64"] == "/opt/voicepeak-box64/bin/box64"
    assert config["box64_library_path"] == "/opt/voicepeak-box64/lib/x86_64-linux-gnu"
    assert "Teto ainda não foram verificados" in result.stdout


def test_old_x64_container_config_and_alias_are_preserved(prepared):
    home, prefix, log, _, env = prepared
    target = home / ".voicepeak-termux"
    (target / "bin").mkdir(parents=True)
    legacy = target / "config.json"
    original = json.dumps({"container": "voicepeak-x64", "guest_executable": "/opt/Voicepeak/voicepeak", "engine_directory": str(home / "licensed engine"), "display": ":1"})
    legacy.write_text(original)
    alias = target / "bin/voicepeak-termux"
    alias.symlink_to("launcher.py")
    marker = prefix / "var/lib/proot-distro/containers/voicepeak-x64/rootfs/keep-activation"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep")
    result = run_setup(env)
    assert result.returncode == 0, result.stderr
    assert legacy.read_text() == original and alias.readlink() == Path("launcher.py")
    assert marker.read_text() == "keep"
    assert json.loads((target / "config-box64.json").read_text())["engine_directory"] == str(home / "licensed engine")
    assert not any("voicepeak-x64" in command for command in calls(log))


def test_repeat_setup_preserves_box64_configuration_and_checkout(prepared):
    home, _, log, _, env = prepared
    first = run_setup(env)
    assert first.returncode == 0, first.stderr
    config = home / ".voicepeak-termux/config-box64.json"
    value = json.loads(config.read_text())
    value["display"] = ":2"
    original = json.dumps(value)
    config.write_text(original)
    second = run_setup(env)
    assert second.returncode == 0, second.stderr
    assert config.read_text() == original
    assert len([command for command in calls(log) if command[:2] == ["git", "clone"]]) == 1
    assert not any("reset" in command or "checkout" in command or "upgrade" in command for command in calls(log))


@pytest.mark.parametrize("variable,value", [("FAKE_APT_FAILURE", "update"), ("FAKE_APT_FAILURE", "install"),
    ("FAKE_CMAKE_FAILURE", "configure"), ("FAKE_CMAKE_FAILURE", "build"), ("FAKE_BOX64_FAILURE", "1"),
    ("FAKE_WRONG_COMMIT", "1"), ("FAKE_DIRTY_SOURCE", "1"), ("FAKE_INVALID_LIBRARY", "1"), ("FAKE_EMPTY_VERSION", "1"),
    ("FAKE_SIGNALLED_VERSION", "1")])
def test_failed_packages_build_pin_or_binary_never_publish_ready(prepared, variable, value):
    home, prefix, _, _, env = prepared
    env[variable] = value
    result = run_setup(env)
    assert result.returncode != 0
    assert not (home / ".voicepeak-termux/config-box64.json").exists()
    assert not (home / ".voicepeak-termux/bin/voicepeak-termux-box64").exists()
    assert "Runtime ARM64 + Box64 preparado" not in result.stdout
    assert (prefix / "var/lib/proot-distro/containers/voicepeak-arm64/rootfs").is_dir()


@pytest.mark.parametrize("variable,value", [("FAKE_NATIVE_ARCH", "x86_64"), ("TERMUX_VERSION", ""), ("PREFIX", ""), ("FAKE_OLD_PROOT", "1"), ("FAKE_GUEST_ARCH", "amd64"), ("FAKE_GLIBC", "glibc 2.35"), ("FAKE_GLIBC", "invalid")])
def test_native_termux_and_arm64_guards(prepared, variable, value):
    home, _, log, _, env = prepared
    env[variable] = value
    result = run_setup(env)
    assert result.returncode == 2
    assert not (home / ".voicepeak-termux/config-box64.json").exists()
    assert not any("/usr/bin/apt-get" in command for command in calls(log))


def test_foreign_alias_is_preserved(prepared):
    home, _, _, _, env = prepared
    alias = home / ".voicepeak-termux/bin/voicepeak-termux-box64"
    alias.parent.mkdir(parents=True)
    alias.write_text("foreign command")
    result = run_setup(env)
    assert result.returncode != 0
    assert alias.read_text() == "foreign command"
    assert not (home / ".voicepeak-termux/config-box64.json").exists()


def test_box64_wrapper_selects_only_its_private_config(prepared):
    home, _, _, _, env = prepared
    result = run_setup(env)
    assert result.returncode == 0, result.stderr
    binary = home / ".voicepeak-termux/bin"
    (binary / "launcher.py").write_text("import os\nprint(os.environ['VOICEPEAK_TERMUX_CONFIG'])\n")
    env["VOICEPEAK_TERMUX_CONFIG"] = "unrelated-user-config"
    result = subprocess.run([str(binary / "voicepeak-termux-box64")], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(home / ".voicepeak-termux/config-box64.json")
    assert env["VOICEPEAK_TERMUX_CONFIG"] == "unrelated-user-config"


@pytest.mark.parametrize("arguments", [("--reset",), ("--prepare-only",), ("unexpected", "argument")])
def test_unknown_options_do_not_mutate(prepared, arguments):
    _, _, log, _, env = prepared
    assert run_setup(env, *arguments).returncode == 2
    assert not log.exists()


def test_help_and_shell_syntax(prepared):
    _, _, log, _, env = prepared
    assert run_setup(env, "--help").returncode == 0
    assert not log.exists()
    assert subprocess.run(["bash", "-n", str(TOOLKIT / "setup-box64.sh")], capture_output=True).returncode == 0
