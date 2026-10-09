"""Run the focused correction's guest Bash with fake build tools."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_voicepeak_termux_box64_setup import calls, executable, prepared, run_setup


TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"


@pytest.fixture
def existing(prepared, tmp_path):
    home, prefix, log, guest, env = prepared
    result = run_setup(env)
    assert result.returncode == 0, result.stderr
    log.write_text("")
    source = tmp_path / "toolkit with spaces"
    source.mkdir()
    for name in ("patch-box64-x11.sh", "launcher.py", "diagnostic.py"):
        shutil.copyfile(TOOLKIT / name, source / name)
    (source / "x11-probe-x86_64").write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\0")
    (source / "box64-xputpixel.py").write_text("""import os, shutil, sys
from pathlib import Path
if os.environ.get('FAKE_HELPER_FAILURE') == '1':
    raise SystemExit('pristine helper check failed')
a = sys.argv
source, target = Path(a[a.index('--source')+1]), Path(a[a.index('--target')+1])
shutil.copytree(source, target, dirs_exist_ok=True)
print('isolated patch prepared')
""")
    common = """import json, os, sys
from pathlib import Path
def record(name, args):
    with open(os.environ['BOX64_TEST_LOG'], 'a') as stream:
        stream.write(json.dumps([name] + args) + '\\n')
a = sys.argv[1:]
"""
    fakebin = Path(env["BOX64_TEST_BIN"])
    executable(fakebin / "proot-distro", common + """import subprocess, shlex
record('proot-distro', a)
if '--print-architecture' in a:
    print(os.environ.get('FAKE_GUEST_ARCH', 'arm64'))
elif '/bin/bash' in a:
    if os.environ.get('FAKE_OUTER') in ('empty', 'signal'):
        if os.environ['FAKE_OUTER'] == 'signal':
            print('proot info: terminated with signal 11', file=sys.stderr)
        sys.exit(0)
    script = sys.stdin.read()
    record('guest-script', [script])
    script = script.replace('/opt/voicepeak-box64', os.environ['BOX64_TEST_GUEST'] + '/opt/voicepeak-box64')
    script = script.replace('box64_toolkit=/opt/voicepeak-termux-kit', 'box64_toolkit=' + shlex.quote(os.environ['X11FIX_TEST_SOURCE']))
    script = script.replace('/opt/voicepeak-termux-kit', os.environ['X11FIX_TEST_SOURCE'])
    script = script.replace('/usr/bin/python3', sys.executable)
    environment = dict(os.environ)
    environment['PATH'] = os.environ['BOX64_TEST_BIN'] + os.pathsep + os.environ['PATH']
    sys.exit(subprocess.run(['bash', '-s', '--', a[-1]], input=script, text=True, env=environment).returncode)
else:
    sys.exit(99)
""")
    executable(fakebin / "cmake", common + """record('cmake', a)
if os.environ.get('FAKE_CMAKE_FAILURE') == ('build' if '--build' in a else 'configure'):
    sys.exit(1)
if '--build' in a:
    target = Path(a[a.index('--build')+1])
    target.mkdir(parents=True, exist_ok=True)
    binary = target / 'box64'
    binary.write_text('#!' + sys.executable + '\\n' + os.environ['X11FIX_TEST_BINARY_CODE'])
    binary.chmod(0o700)
else:
    Path(a[a.index('-B')+1]).mkdir(parents=True, exist_ok=True)
""")
    env["X11FIX_TEST_SOURCE"] = str(source)
    env["X11FIX_TEST_BINARY_CODE"] = """import os, sys
if '--version' in sys.argv:
    mode = os.environ.get('FAKE_VERSION', '')
    if mode == 'signal':
        print('Box64 with Dynarec v0.4.0\\nproot info: terminated with signal 11', file=sys.stderr)
    elif mode != 'empty':
        print('Box64 with Dynarec v0.3.8' if mode == 'wrong' else 'Box64 with Dynarec v0.4.0')
    sys.exit(1 if mode == 'failure' else 0)
mode = os.environ.get('FAKE_PROBE', '')
if mode != 'empty':
    print('VOICEPEAK_X11_SYMBOLS_OK' if mode != 'wrong' else 'VOICEPEAK_X11_PROBE_OK')
if mode == 'signal':
    print('proot info: terminated with signal 11', file=sys.stderr)
sys.exit(1 if mode == 'failure' else 0)
"""
    env.pop("FAKE_CMAKE_FAILURE", None)
    return home, prefix, log, guest, env, source


def run_patch(existing, *arguments):
    *_, env, source = existing
    return subprocess.run(["bash", str(source / "patch-box64-x11.sh"), *arguments],
                          env=env, capture_output=True, text=True, timeout=20)


def test_correction_builds_separate_binary_and_preserves_original(existing):
    home, _, log, guest, _, source = existing
    config = home / ".voicepeak-termux/config-box64.json"
    original_config = config.read_bytes()
    original_binary = guest / "opt/voicepeak-box64/bin/box64"
    original_binary_bytes = original_binary.read_bytes()
    engine = home / ".voicepeak-termux/engine/Voicepeak"
    engine.mkdir(parents=True)
    (engine / "licensed-settings").write_text("keep")
    result = run_patch(existing)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == original_config
    assert original_binary.read_bytes() == original_binary_bytes
    assert (engine / "licensed-settings").read_text() == "keep"
    commands = calls(log)
    assert not any("install" in command or "remove" in command or "reset" in command or "apt-get" in str(command) for command in commands if command[0] != "guest-script")
    login = next(command for command in commands if "/bin/bash" in command)
    assert f"{source}:/opt/voicepeak-termux-kit" in login
    configure = next(command for command in commands if command[0] == "cmake" and "-S" in command)
    assert configure[configure.index("-S")+1].endswith("/src-x11fix-1")
    assert configure[configure.index("-B")+1].endswith("/build-x11fix-1")
    assert {"-DARM64=1", "-DARM_DYNAREC=ON", "-DBAD_SIGNAL=ON", "-DNOGIT=ON"} <= set(configure)
    assert next(command for command in commands if command[:2] == ["cmake", "--build"])[-2:] == ["--parallel", "2"]
    fixed = json.loads((config.parent / "config-box64-x11fix.json").read_text())
    expected = json.loads(original_config)
    expected["guest_box64"] = "/opt/voicepeak-box64/bin/box64-x11fix-1"
    assert fixed == expected
    assert (guest / "opt/voicepeak-box64/bin/box64-x11fix-1").is_file()
    assert "não confirma abertura da GUI" in result.stdout


@pytest.mark.parametrize("variable,value", [("FAKE_WRONG_COMMIT", "1"), ("FAKE_DIRTY_SOURCE", "1"),
    ("FAKE_HELPER_FAILURE", "1"), ("FAKE_CMAKE_FAILURE", "configure"), ("FAKE_CMAKE_FAILURE", "build"),
    ("FAKE_VERSION", "empty"), ("FAKE_VERSION", "wrong"), ("FAKE_VERSION", "failure"), ("FAKE_VERSION", "signal"),
    ("FAKE_PROBE", "empty"), ("FAKE_PROBE", "wrong"), ("FAKE_PROBE", "failure"), ("FAKE_PROBE", "signal"),
    ("FAKE_OUTER", "empty"), ("FAKE_OUTER", "signal")])
def test_failure_or_false_zero_never_publishes_candidate(existing, variable, value):
    home, _, _, guest, env, _ = existing
    original_binary = (guest / "opt/voicepeak-box64/bin/box64").read_bytes()
    original_config = (home / ".voicepeak-termux/config-box64.json").read_bytes()
    env[variable] = value
    assert run_patch(existing).returncode != 0
    assert not (home / ".voicepeak-termux/config-box64-x11fix.json").exists()
    assert not (home / ".voicepeak-termux/bin/voicepeak-termux-box64-x11fix").exists()
    assert (guest / "opt/voicepeak-box64/bin/box64").read_bytes() == original_binary
    assert (home / ".voicepeak-termux/config-box64.json").read_bytes() == original_config


@pytest.mark.parametrize("field", ["alias", "config"])
def test_foreign_native_files_fail_before_build(existing, field):
    home, _, log, _, _, _ = existing
    target = home / ".voicepeak-termux"
    path = target / ("bin/voicepeak-termux-box64-x11fix" if field == "alias" else "config-box64-x11fix.json")
    original = "foreign command" if field == "alias" else '{"custom": "keep"}'
    path.write_text(original)
    result = run_patch(existing)
    assert result.returncode != 0
    assert path.read_text() == original
    assert not calls(log)


def test_repeat_preserves_matching_config_and_managed_alias(existing):
    home, _, _, _, _, _ = existing
    first = run_patch(existing)
    assert first.returncode == 0, first.stderr
    config = home / ".voicepeak-termux/config-box64-x11fix.json"
    original = config.read_bytes()
    second = run_patch(existing)
    assert second.returncode == 0, second.stderr
    assert config.read_bytes() == original


@pytest.mark.parametrize("variable,value", [("FAKE_VERSION", "wrong"), ("FAKE_PROBE", "failure")])
def test_failed_retry_preserves_previously_verified_binary_and_aliases(existing, variable, value):
    home, _, _, guest, env, _ = existing
    result = run_patch(existing)
    assert result.returncode == 0, result.stderr
    candidate = guest / "opt/voicepeak-box64/bin/box64-x11fix-1"
    selected = home / ".voicepeak-termux"
    published = [candidate, selected / "config-box64-x11fix.json",
                 selected / "bin/voicepeak-termux-box64-x11fix",
                 selected / "bin/voicepeak-termux-box64-x11fix-diagnostic"]
    previous = {path: path.read_bytes() for path in published}
    # Make a rebuilt candidate observably different from the active binary.
    env["X11FIX_TEST_BINARY_CODE"] += "\n# failed retry candidate\n"
    env[variable] = value
    assert run_patch(existing).returncode != 0
    assert {path: path.read_bytes() for path in published} == previous
    assert not list(candidate.parent.glob(".box64-x11fix-1.*"))


def test_native_wrapper_selects_fixed_config_with_no_global_env_change(existing):
    home, _, _, _, env, _ = existing
    result = run_patch(existing)
    assert result.returncode == 0, result.stderr
    binary = home / ".voicepeak-termux/bin"
    (binary / "launcher.py").write_text("import os\nprint(os.environ['VOICEPEAK_TERMUX_CONFIG'])\n")
    env["VOICEPEAK_TERMUX_CONFIG"] = "unrelated-config"
    result = subprocess.run([str(binary / "voicepeak-termux-box64-x11fix")], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(home / ".voicepeak-termux/config-box64-x11fix.json")
    assert env["VOICEPEAK_TERMUX_CONFIG"] == "unrelated-config"


@pytest.mark.parametrize("case", ["missing-config", "wrong-backend", "wrong-guest", "wrong-arch", "outside-termux", "bad-probe"])
def test_prerequisites_preserve_runtime(existing, case):
    home, _, _, _, env, source = existing
    config = home / ".voicepeak-termux/config-box64.json"
    if case == "missing-config":
        config.unlink()
    elif case in ("wrong-backend", "wrong-guest"):
        value = json.loads(config.read_text())
        value["backend" if case == "wrong-backend" else "guest_box64"] = "qemu" if case == "wrong-backend" else "/custom/box64"
        config.write_text(json.dumps(value))
    elif case == "wrong-arch":
        env["FAKE_GUEST_ARCH"] = "amd64"
    elif case == "outside-termux":
        env["TERMUX_VERSION"] = ""
    elif case == "bad-probe":
        (source / "x11-probe-x86_64").write_text("invalid executable")
    assert run_patch(existing).returncode != 0
    assert not (home / ".voicepeak-termux/config-box64-x11fix.json").exists()


def test_help_unknown_argument_and_shell_syntax(existing):
    assert run_patch(existing, "--help").returncode == 0
    assert run_patch(existing, "--reset").returncode == 2
    assert subprocess.run(["bash", "-n", str(TOOLKIT / "patch-box64-x11.sh")], capture_output=True).returncode == 0
