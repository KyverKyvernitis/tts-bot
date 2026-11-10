"""Artifact ABI and local-build tooling contracts; no compiler/network required."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(filename):
    spec = importlib.util.spec_from_file_location("native_tool_test_" + filename.replace("-", "_"), ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_glibc_gate_ignores_optional_weak_probes_and_rejects_new_mandatory_symbols(monkeypatch, tmp_path):
    tool = load("build-tts-native.py")
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/readelf")
    output = ["""Version needs section '.gnu.version_r':
  File: libc.so.6
  Name: GLIBC_2.34  Flags: none Version: 2
  Name: GLIBC_2.39  Flags: WEAK Version: 3
  File: libm.so.6
  Name: GLIBC_2.29  Flags: none Version: 4
"""]

    def readelf(command, **kwargs):
        assert command[1] == "--version-info"
        assert kwargs["env"]["LC_ALL"] == "C"
        return SimpleNamespace(stdout=output[0])

    monkeypatch.setattr(tool.subprocess, "run", readelf)
    assert tool.glibc_requirement(tmp_path / "native.so", "2.35") == "2.34"
    output[0] += "Name: GLIBC_2.36  Flags: none Version: 5\n"
    with pytest.raises(RuntimeError, match="requires GLIBC 2.36"):
        tool.glibc_requirement(tmp_path / "native.so", "2.35")
    output[0] += "Name: GLIBC_2.39  Flags: none Version: 6\n"
    with pytest.raises(RuntimeError, match="requires GLIBC 2.39"):
        tool.glibc_requirement(tmp_path / "native.so", "2.35")


def test_wheel_extraction_requires_one_abi3_extension_before_replacing_current(monkeypatch, tmp_path):
    tool = load("build-tts-native.py")
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    destination = tmp_path / "vinculo_python/python/tts_fluxo_experimental/_nucleo_fluxo_tts.abi3.so"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous-native")
    wheel = tmp_path / "extension.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("tts_fluxo_experimental/_nucleo_fluxo_tts.cpython-312-x86_64-linux-gnu.so", b"wrong-abi")
    with pytest.raises(RuntimeError, match="expected one extension"):
        tool.install_extension(wheel, env={}, install_wheel=False)
    assert destination.read_bytes() == b"previous-native"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("a/_nucleo_fluxo_tts.abi3.so", b"ambiguous-a")
        archive.writestr("b/_nucleo_fluxo_tts.abi3.so", b"ambiguous-b")
    with pytest.raises(RuntimeError, match="expected one extension"):
        tool.install_extension(wheel, env={}, install_wheel=False)
    assert destination.read_bytes() == b"previous-native"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("tts_fluxo_experimental/_nucleo_fluxo_tts.abi3.so", b"complete-new-native")
    assert tool.install_extension(wheel, env={}, install_wheel=False) == destination
    assert destination.read_bytes() == b"complete-new-native"
    assert not list(destination.parent.glob("._nucleo*"))


@pytest.mark.parametrize("target,zig,expected", [
    ("x86_64-unknown-linux-gnu", False, ["--compatibility", "linux"]),
    ("aarch64-unknown-linux-gnu", True, ["--zig", "--compatibility", "manylinux_2_35"]),
    ("aarch64-apple-darwin", False, []),
])
def test_private_wheel_tag_keeps_weak_libc_probes_out_of_pip_minimum(target, zig, expected):
    tool = load("build-tts-native.py")
    assert tool.wheel_compatibility_args(target, zig=zig, maximum_glibc="2.35") == expected


def test_native_gate_rejects_missing_worker_instead_of_skipping(tmp_path):
    tool = load("check-tts-native.py")
    with pytest.raises(RuntimeError, match="missing or not executable"):
        tool.worker_binary({"TTS_NATIVE_WORKER_TEST_BINARY": str(tmp_path / "missing-worker")})


def test_mandatory_gate_fails_when_pytest_returns_zero_but_native_test_skips(monkeypatch, tmp_path):
    tool = load("check-tts-native.py")
    monkeypatch.setattr(tool, "preflight", lambda: None)
    worker = tmp_path / "worker"
    worker.write_bytes(b"native-worker-fixture")
    monkeypatch.setattr(tool, "worker_binary", lambda _env: worker)
    builder = load("build-tts-native.py")
    monkeypatch.setattr(tool, "load_builder", lambda: SimpleNamespace(
        environment=lambda: {"PATH": "/usr/bin"}, atomic_write=builder.atomic_write))
    monkeypatch.setattr(tool.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/ffmpeg")

    def run(command, **_kwargs):
        skipped = any(str(part).endswith("test_tts_replay_rust.py") for part in command)
        return SimpleNamespace(returncode=0, stdout="1 skipped\n" if skipped else "1 passed\n")

    monkeypatch.setattr(tool.subprocess, "run", run)
    report = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["check-tts-native.py", "--python-only", "--report", str(report)])
    assert tool.main() == 1
    result = json.loads(report.read_text())
    assert result["passed"] is False
    replay = next(row for row in result["commands"] if row["name"] == "native-replay")
    assert replay["returncode"] == 0
    assert replay["unexpected_native_skip"] is True
    assert result["completed"] is True
    assert result["worker"]["bytes"] == len(b"native-worker-fixture")
    names = {row["name"] for row in result["commands"]}
    assert {"cd-native-pcm", "music-native-tts", "tests/test_tts_native_source_pipeline.py",
            "tests/test_tts_policy_rust_native.py"} <= names
    assert next(row for row in result["commands"] if row["name"] == "cd-native-pcm")["mandatory_native"]


@pytest.mark.parametrize("missing", ["NativePendingQueue", "provider_save", "ttv_midi_json", "cd_stereo_mix"])
def test_native_preflight_rejects_missing_callable_export(monkeypatch, missing):
    tool = load("check-tts-native.py")
    exports = dict.fromkeys(tool.REQUIRED_EXPORTS, lambda: None)
    exports.update(dict.fromkeys(tool.REQUIRED_PROTOCOLS, 1))
    exports[missing] = None
    native = SimpleNamespace(**exports)
    monkeypatch.setitem(sys.modules, "cogs.tts.fluxo_rust", SimpleNamespace(_carregar_nucleo=lambda: native))
    with pytest.raises(RuntimeError, match=missing):
        tool.preflight()


def test_preflight_failure_invalidates_previous_passing_report(monkeypatch, tmp_path):
    tool = load("check-tts-native.py")
    builder = load("build-tts-native.py")
    monkeypatch.setattr(tool, "load_builder", lambda: SimpleNamespace(
        environment=lambda: {"PATH": "/usr/bin"}, atomic_write=builder.atomic_write))
    monkeypatch.setattr(tool.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/ffmpeg")

    def missing_extension():
        raise ImportError("required native extension absent")

    monkeypatch.setattr(tool, "preflight", missing_extension)
    report = tmp_path / "report.json"
    report.write_text('{"passed": true, "commands": [{"old": true}]}')
    monkeypatch.setattr(sys, "argv", ["check-tts-native.py", "--python-only", "--report", str(report)])
    with pytest.raises(ImportError, match="native extension absent"):
        tool.main()
    result = json.loads(report.read_text())
    assert result["passed"] is False
    assert result["completed"] is False
    assert result["commands"] == []
    assert result["started_utc"]


def test_gate_rejects_worker_replaced_during_passing_tests(monkeypatch, tmp_path):
    tool = load("check-tts-native.py")
    builder = load("build-tts-native.py")
    worker = tmp_path / "worker"
    worker.write_bytes(b"original")
    monkeypatch.setattr(tool, "preflight", lambda: None)
    monkeypatch.setattr(tool, "worker_binary", lambda _env: worker)
    monkeypatch.setattr(tool, "load_builder", lambda: SimpleNamespace(
        environment=lambda: {"PATH": "/usr/bin"}, atomic_write=builder.atomic_write))
    monkeypatch.setattr(tool.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/ffmpeg")

    def run(_command, **_kwargs):
        worker.write_bytes(b"replacement")
        return SimpleNamespace(returncode=0, stdout="1 passed\n")

    monkeypatch.setattr(tool.subprocess, "run", run)
    report = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["check-tts-native.py", "--python-only", "--report", str(report)])
    assert tool.main() == 1
    result = json.loads(report.read_text())
    assert result["completed"] is True
    assert result["artifacts_unchanged"] is False
    assert result["changed_artifacts"] == ["worker"]
    assert all(command["returncode"] == 0 for command in result["commands"])


@pytest.mark.parametrize("script", ["build-tts-native.py", "check-tts-native.py", "benchmark-tts-native.py"])
def test_cli_help_never_builds_loads_native_or_contacts_network(script):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"],
                            text=True, capture_output=True, check=True, timeout=5)
    assert "usage:" in result.stdout
    assert "Traceback" not in result.stderr


def test_benchmark_provenance_hashes_incrementally_without_polluting_rss(tmp_path):
    import hashlib
    tool = load("benchmark-tts-native.py")
    artifact = tmp_path / "extension.so"
    artifact.write_bytes(bytes(range(256)) * 2048)
    assert tool.file_sha256(artifact) == hashlib.sha256(artifact.read_bytes()).hexdigest()
    summary = tool.summarize([{"first_pcm_frame_ms": 10, "output_bytes": 3840, "output_sha256": "same"},
                              {"first_pcm_frame_ms": 30, "output_bytes": 3840, "output_sha256": "same"}])
    assert summary["first_pcm_frame_ms"]["p50"] == 20
    assert summary["first_pcm_frame_ms"]["p95"] == 29
    assert summary["output_sha256"] == ["same"]


def test_benchmark_cli_rejects_zero_samples_before_loading_native():
    # The parser validates numerical constraints before FFmpeg/native import.
    result = subprocess.run([sys.executable, str(ROOT / "scripts/benchmark-tts-native.py"), "--runs", "0"],
                            text=True, capture_output=True, timeout=5)
    assert result.returncode != 0
    assert "must be positive" in result.stderr
