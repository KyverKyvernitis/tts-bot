from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import types
import wave
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/worldline-r/termux/diagnostic.py"


@pytest.fixture
def doctor():
    module = types.ModuleType("worldline_doctor_test")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), "exec"), module.__dict__)
    return module


def verified_runtime(doctor):
    module = doctor.load_runtime_module()
    return {
        "runtime_ok": True, "api_verified": True, "abi_verified": True,
        "library_hash_verified": True, "synthetic_render_verified": True,
        "source_commit": module.SOURCE_COMMIT, "source_version": module.SOURCE_VERSION,
        "native_architecture": "arm64", "library_sha256": module.RELEASE_SHA256,
        "teto_synthesis_verified": False, "portuguese_speech_verified": False,
    }


def arm64_elf():
    raw = bytearray(64)
    raw[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", raw, 16, 3, 183)
    return bytes(raw)


def test_environment_reads_only_literal_teto_values_without_execution(tmp_path, doctor):
    marker = tmp_path / "executed"
    environment = tmp_path / ".phone-worker.env"
    secret = "private-discord-token"
    environment.write_text(
        f'DISCORD_TOKEN="{secret}"\n'
        "PHONE_WORKER_TETO_ENABLED=1\n"
        "export PHONE_WORKER_TETO_VOICEBANK_DIR='/home/user/Teto bank' # comment\n"
        f'PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR="$(touch {marker})"\n'
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND=`echo forbidden`\n"
    )
    before = environment.read_bytes()
    values, metadata = doctor.read_teto_environment(environment)
    assert values == {"PHONE_WORKER_TETO_ENABLED": "1", "PHONE_WORKER_TETO_VOICEBANK_DIR": "/home/user/Teto bank"}
    assert metadata["accepted_keys"] == 2 and metadata["rejected_teto_values"] == 2
    assert secret not in json.dumps((values, metadata))
    assert environment.read_bytes() == before and not marker.exists()


@pytest.mark.parametrize("kind", ["symlink", "directory", "oversized", "fifo"])
def test_environment_rejects_nonregular_or_unbounded_inputs(tmp_path, doctor, kind):
    environment = tmp_path / ".phone-worker.env"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("PHONE_WORKER_TETO_ENABLED=1")
        environment.symlink_to(target)
    elif kind == "directory":
        environment.mkdir()
    elif kind == "fifo":
        os.mkfifo(environment)
    else:
        environment.write_bytes(b"x" * (doctor.MAX_ENV + 1))
    values, metadata = doctor.read_teto_environment(environment)
    assert values == {} and not metadata["present"] and metadata["error"]


def test_binary_package_inventory_recognizes_arm64_multiarch_only(doctor):
    result = doctor.package_inventory({"ok": True, "output": (
        "python3\tinstall ok installed\n"
        "libc6:arm64\tinstall ok installed\n"
        "libstdc++6:arm64\thold ok installed\n"
        "libgcc-s1:arm64\tinstall ok half-configured\n"
        "libgcc-s1:amd64\tinstall ok installed\n"
    )})
    assert result == {"python3": True, "libc6": True, "libstdc++6": True, "libgcc-s1": False}


@pytest.mark.parametrize("command", [
    [sys.executable, "-c", "import os; os.write(2, b'qemu: uncaught target signal 11 (Segmentation fault)\\n')"],
    [sys.executable, "-c", "import os; os.write(2, b'proot info: vpid 1: terminated with signal 11\\n')"],
])
def test_proot_false_zero_cannot_hide_signal_failure(doctor, command):
    result = doctor.bounded(command, 2)
    assert result["ok"] is False and result["code"] == 0 and result["signal"] == 11
    assert "output" not in result


def test_probe_output_limit_stops_noisy_process(doctor):
    result = doctor.bounded([sys.executable, "-c", "import os; os.write(1, b'x' * 200000); import time; time.sleep(10)"], 2)
    assert not result["ok"] and result["error"] == "saída excede limite"
    assert "output" not in result


def test_probe_timeout_stops_process_group_without_waiting_for_descendant(doctor):
    result = doctor.bounded([sys.executable, "-c", "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); import time; time.sleep(10)"], .1)
    assert not result["ok"] and result["error"] == "tempo esgotado"


def test_probe_accepts_bounded_stdout_and_omits_stderr(doctor):
    result = doctor.bounded([sys.executable, "-c", "import sys; print('arm64'); print('private-token', file=sys.stderr)"], 2)
    assert result == {"ok": True, "code": 0, "output": "arm64\n"}


def test_library_inventory_confirms_elf_and_pin_without_loading_library(tmp_path, monkeypatch, doctor):
    library = tmp_path / "libworldline.so"
    library.write_bytes(arm64_elf())
    pin = hashlib.sha256(library.read_bytes()).hexdigest()
    monkeypatch.setattr(doctor, "load_runtime_module", lambda: types.SimpleNamespace(RELEASE_SHA256=pin, MAX_LIBRARY_BYTES=1024))
    result = doctor.library_inventory(library)
    assert result["present"] and result["elf_arm64"] and result["pinned_release"]
    assert result["sha256"] == pin


@pytest.mark.parametrize("kind", ["x86_64", "symlink", "oversized"])
def test_library_inventory_does_not_mark_incompatible_inputs_ready(tmp_path, monkeypatch, doctor, kind):
    library = tmp_path / "libworldline.so"
    if kind == "symlink":
        target = tmp_path / "target.so"
        target.write_bytes(arm64_elf())
        library.symlink_to(target)
    elif kind == "oversized":
        library.write_bytes(arm64_elf() + b"x" * 100)
    else:
        raw = bytearray(arm64_elf())
        struct.pack_into("<H", raw, 18, 62)
        library.write_bytes(raw)
    monkeypatch.setattr(doctor, "load_runtime_module", lambda: types.SimpleNamespace(RELEASE_SHA256="0" * 64, MAX_LIBRARY_BYTES=100))
    result = doctor.library_inventory(library)
    assert not (result["elf_arm64"] and result["pinned_release"])


def test_load_helpers_does_not_create_bytecode_files(tmp_path, monkeypatch, doctor):
    source = tmp_path / "helper.py"
    source.write_text("answer = 42\n")
    module = doctor.load_bundled_source(source, "worldline_readonly_helper_test")
    assert module.answer == 42 and not (tmp_path / "__pycache__").exists()
    sys.modules.pop(module.__name__, None)


def test_real_voicebank_index_is_loaded_without_renderer_or_writes(tmp_path, doctor):
    bank = tmp_path / "Teto"
    bank.mkdir()
    wav = bank / "sample.wav"
    with wave.open(str(wav), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b"\x00\x00" * 441)
    (bank / "oto.ini").write_text("sample.wav=a,0,10,-20,5,2\nsample.wav=i,0,10,-20,5,2\n")
    before = {path: path.read_bytes() for path in bank.iterdir()}
    worker = ROOT / "deploy/termux/phone-worker"
    modules_before = set(sys.modules)
    result = doctor.audit_voicebank(bank, worker, 2)
    assert result["ok"] and result["aliases"] == 2 and not result["wav_decode_checked"]
    assert len(result["fingerprint"]) == 64
    assert {path: path.read_bytes() for path in bank.iterdir()} == before
    assert not any(name.startswith("_worldline_voicebank_audit") for name in sys.modules)
    assert "teto_renderer.renderer" not in set(sys.modules) - modules_before


def test_insufficient_voicebank_returns_safe_category(tmp_path, doctor):
    result = doctor.audit_voicebank(tmp_path, ROOT / "deploy/termux/phone-worker", 10)
    assert not result["ok"] and "TetoVoicebankError" == result["error_type"]
    assert str(tmp_path) not in json.dumps(result)


def test_runtime_evidence_uses_actual_bundled_schema(doctor):
    result = doctor.decode_probe({"ok": True, "output": json.dumps(verified_runtime(doctor))})
    assert result["runtime_verified"] and result["probe"]["synthetic_render_verified"]
    assert not result["probe"]["teto_synthesis_verified"]


@pytest.mark.parametrize("field,value", [
    ("runtime_ok", False), ("api_verified", False), ("abi_verified", False),
    ("library_hash_verified", False), ("synthetic_render_verified", False),
    ("source_commit", "unknown"), ("source_version", "newer"),
    ("native_architecture", "x64"), ("library_sha256", "0" * 64),
])
def test_partial_or_inconsistent_runtime_evidence_is_not_ready(doctor, field, value):
    data = verified_runtime(doctor)
    data[field] = value
    data["checks"] = {"stdout": "private-discord-token"}
    result = doctor.decode_probe({"ok": True, "output": json.dumps(data)})
    assert not result["runtime_verified"]
    assert "private-discord-token" not in json.dumps(result)


def test_runtime_rejects_exit_zero_without_json_evidence(doctor):
    assert not doctor.decode_probe({"ok": True, "output": "WORLDLINE_OK"})["runtime_verified"]
    assert not doctor.decode_probe({"ok": True, "output": "[]"})["runtime_verified"]


def ready_report_dependencies(monkeypatch, doctor, tmp_path):
    monkeypatch.setattr(doctor.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(doctor, "read_teto_environment", lambda: ({}, {"present": False}))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "library_inventory", lambda path: {
        "path": str(path), "present": True, "elf_arm64": True,
        "sha256": doctor.load_runtime_module().RELEASE_SHA256, "pinned_release": True,
    })
    calls = []
    def run(command, timeout):
        calls.append(command)
        if command[0].endswith("ffmpeg"):
            output = "ffmpeg version 8.0\n"
        elif "--print-architecture" in command:
            output = "arm64\n"
        elif "/usr/bin/dpkg-query" in command:
            output = "python3\tinstall ok installed\n" + "".join(
                f"{name}:arm64\tinstall ok installed\n" for name in doctor.REQUIRED_PACKAGES if name != "python3"
            )
        elif "/opt/worldline-r-kit/runtime-probe.py" in command:
            assert "--render-probe" in command
            output = json.dumps(verified_runtime(doctor))
        else:
            assert "--_voicebank-audit" in command and "-B" in command
            output = json.dumps({"ok": True, "aliases": 700, "fingerprint": "0" * 64, "wav_decode_checked": False})
        return {"ok": True, "code": 0, "output": output}
    monkeypatch.setattr(doctor, "bounded", run)
    return calls


def test_complete_runtime_and_voicebank_still_do_not_enable_tts(tmp_path, monkeypatch, doctor):
    calls = ready_report_dependencies(monkeypatch, doctor, tmp_path)
    result = doctor.build_report(library=tmp_path / "libworldline.so", voicebank=tmp_path / "Teto")
    assert result["runtime_ready"]
    assert result["requirements"]["valid_voicebank"]
    assert result["requirements"]["ffmpeg"]
    assert result["blockers"] == ["phrase_adapter"]
    assert not result["tts_ready"] and not result["phrase_adapter_available"]
    assert not result["box64_required"] and not result["voicepeak_required"]
    for command in calls:
        assert not any(word in command for word in ("apt", "apt-get", "install", "--configure", "--add-architecture"))


def test_amd64_guest_is_reported_without_executing_library(tmp_path, monkeypatch, doctor):
    calls = ready_report_dependencies(monkeypatch, doctor, tmp_path)
    original = doctor.bounded
    def run(command, timeout):
        if "--print-architecture" in command:
            calls.append(command)
            return {"ok": True, "code": 0, "output": "amd64\n"}
        return original(command, timeout)
    monkeypatch.setattr(doctor, "bounded", run)
    result = doctor.build_report(library=tmp_path / "libworldline.so")
    assert not result["runtime_ready"] and not result["requirements"]["guest_arm64"]
    assert not any("/opt/worldline-r-kit/runtime-probe.py" in command for command in calls)


def test_unpinned_library_never_reaches_native_probe(tmp_path, monkeypatch, doctor):
    calls = ready_report_dependencies(monkeypatch, doctor, tmp_path)
    monkeypatch.setattr(doctor, "library_inventory", lambda path: {
        "path": str(path), "present": True, "elf_arm64": True, "sha256": "0" * 64, "pinned_release": False,
    })
    result = doctor.build_report(library=tmp_path / "libworldline.so")
    assert not result["requirements"]["native_c_api"]
    assert not any("/opt/worldline-r-kit/runtime-probe.py" in command for command in calls)


def test_report_file_creation_works_without_android_os_link(tmp_path, monkeypatch, doctor):
    monkeypatch.delattr(doctor.os, "link")
    report = tmp_path / "diagnostic.json"
    doctor.write_report(report, {"tts_ready": False})
    assert json.loads(report.read_text()) == {"tts_ready": False}
    assert not list(tmp_path.glob(".diagnostic.json.*"))


def test_report_rejects_symlink(tmp_path, doctor):
    target = tmp_path / "target"
    target.write_text("preserve")
    report = tmp_path / "report.json"
    report.symlink_to(target)
    with pytest.raises(ValueError, match="regular"):
        doctor.write_report(report, {})
    assert target.read_text() == "preserve"


def test_cli_audit_runs_without_guest_and_keeps_worker_disabled(tmp_path, monkeypatch, capsys, doctor):
    monkeypatch.setattr(doctor, "bounded", lambda *args, **kwargs: pytest.fail("must not start guest"))
    assert doctor.main(["--_voicebank-audit", str(tmp_path), "--worker-dir", str(ROOT / "deploy/termux/phone-worker")]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize("timeout", ["0", "121", "nan", "inf"])
def test_cli_rejects_invalid_timeout(doctor, timeout):
    with pytest.raises(SystemExit) as error:
        doctor.main(["--timeout", timeout])
    assert error.value.code == 2
