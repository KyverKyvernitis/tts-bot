from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/worldline-r/termux/runtime-probe.py"
spec = importlib.util.spec_from_file_location("worldline_runtime_probe", SCRIPT)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def elf(machine=62, elf_class=2, endian=1, kind=3):
    value = bytearray(64)
    value[:4] = b"\x7fELF"
    value[4:6] = bytes((elf_class, endian))
    struct.pack_into("<HH", value, 16, kind, machine)
    return bytes(value)


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe, "native_architecture", lambda: "x64")


@pytest.mark.parametrize("data, message", [
    (b"text", "not an ELF"),
    (elf(183), "does not match"),
    (elf(elf_class=1), "ELF64"),
    (elf(endian=2), "little-endian"),
    (elf(kind=2), "shared object"),
    (elf(), "SHA-256"),
])
def test_inspect_rejects_incompatible_library_before_native_calls(tmp_path, native, data, message):
    path = tmp_path / "libworldline.so"
    path.write_bytes(data)
    with pytest.raises(probe.ProbeError, match=message):
        probe.inspect_library(path)


def test_inspect_rejects_android_bionic(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "android")
    with pytest.raises(probe.ProbeError, match="glibc guest"):
        probe.inspect_library(tmp_path / "libworldline.so")


def test_relative_library_has_actionable_error():
    report = probe.probe(Path("libworldline.so"))
    assert report["runtime_ok"] is False
    assert "absolute path" in report["error"]


def test_missing_library_is_reported(tmp_path, native):
    report = probe.probe(tmp_path / "absent.so")
    assert not report["runtime_ok"]
    assert "not found" in report["error"]


@pytest.mark.parametrize("timeout", [0, -1, 121, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_without_subprocess(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("child should not run"))
    assert "timeout" in probe.probe(tmp_path / "lib.so", timeout=timeout)["error"]


@pytest.fixture
def trusted_library(tmp_path, monkeypatch):
    path = tmp_path / "libworldline.so"
    monkeypatch.setattr(probe, "inspect_library", lambda path: {"native_architecture": "x64", "library_hash_verified": True})
    return path


def test_child_signal_is_contained(trusted_library, monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=-11, stdout="", stderr="segfault"))
    report = probe.probe(trusted_library)
    assert not report["runtime_ok"]
    assert report["checks"]["native_child"]["signal"] == 11
    assert report["checks"]["native_child"]["stderr"] == "segfault"


def test_child_timeout_is_contained(trusted_library, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr=b"slow native call")
    monkeypatch.setattr(probe.subprocess, "run", run)
    report = probe.probe(trusted_library, timeout=0.01)
    assert not report["runtime_ok"]
    assert report["checks"]["native_child"]["timed_out"]
    assert report["checks"]["native_child"]["stderr"] == "slow native call"


def test_non_json_child_cannot_be_success(trusted_library, monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="native text", stderr=""))
    report = probe.probe(trusted_library)
    assert not report["runtime_ok"]
    assert "valid JSON" in report["error"]


def test_success_stays_distinct_from_teto_synthesis(trusted_library, monkeypatch):
    child = {"ok": True, "api_verified": True, "synthetic_render": {"ok": True, "sample_frames": 13231}}
    def run(command, **kwargs):
        assert command[0] == probe.sys.executable
        assert "--_child" in command and "--render-probe" in command
        assert kwargs["timeout"] == 20
        return SimpleNamespace(returncode=0, stdout=json.dumps(child), stderr="")
    monkeypatch.setattr(probe.subprocess, "run", run)
    report = probe.probe(trusted_library, render_probe=True)
    assert report["runtime_ok"] and report["api_verified"] and report["synthetic_render_verified"]
    assert report["teto_synthesis_verified"] is False
    assert report["portuguese_speech_verified"] is False


def test_create_delete_only_by_default(trusted_library, monkeypatch):
    def run(command, **kwargs):
        assert "--render-probe" not in command
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "api_verified": True}), stderr="")
    monkeypatch.setattr(probe.subprocess, "run", run)
    report = probe.probe(trusted_library)
    assert report["runtime_ok"] and not report["synthetic_render_verified"]


def test_missing_phrase_symbols_are_actionable():
    with pytest.raises(probe.ProbeError, match="PhraseSynthNew"):
        probe.bind_api(SimpleNamespace())


def test_64bit_request_layout_matches_upstream():
    if struct.calcsize("P") != 8:
        pytest.skip("64-bit native ABI required")
    assert probe.ctypes.sizeof(probe.SynthRequest) == 144
    assert probe.SynthRequest.con_vel.offset == 40
    assert probe.SynthRequest.pitch_bend.offset == 112
    assert probe.SynthRequest.flag_Mv.offset == 140
