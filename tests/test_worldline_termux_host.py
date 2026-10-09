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
    monkeypatch.setattr(doctor, "read_teto_environment", lambda: (
        {"PHONE_WORKER_TETO_ENABLED": "1", "PHONE_WORKER_TETO_BACKEND": "worldline-r"}, {"present": True}))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "library_inventory", lambda path: {
        "path": str(path), "present": True, "elf_arm64": True,
        "sha256": doctor.load_runtime_module().RELEASE_SHA256, "pinned_release": True,
    })
    calls = []
    def run(command, timeout, *, env=None):
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
        elif "--_adapter-status" in command:
            assert "-B" in command and env["PHONE_WORKER_TETO_BACKEND"] == "worldline-r"
            assert "DISCORD_TOKEN" not in env
            output = json.dumps({"importable": True, "status_ready": True, "tts_ready": True,
                                 "enabled": True, "native_c_api": True, "runtime_ready": True,
                                 "phrase_adapter_available": True, "adapter_sha256": "1" * 64,
                                 "library_sha256": doctor.load_runtime_module().RELEASE_SHA256})
        else:
            assert "--_voicebank-audit" in command and "-B" in command
            output = json.dumps({"ok": True, "aliases": 700, "fingerprint": "0" * 64, "wav_decode_checked": False})
        return {"ok": True, "code": 0, "output": output}
    monkeypatch.setattr(doctor, "bounded", run)
    return calls


def test_complete_kit_reports_tts_ready_without_claiming_worker_installed(tmp_path, monkeypatch, doctor):
    calls = ready_report_dependencies(monkeypatch, doctor, tmp_path)
    result = doctor.build_report(library=tmp_path / "libworldline.so", voicebank=tmp_path / "Teto")
    assert result["runtime_ready"]
    assert result["requirements"]["valid_voicebank"]
    assert result["requirements"]["ffmpeg"]
    assert result["blockers"] == []
    assert result["tts_ready"] and result["phrase_adapter_available"] and result["kit_tts_ready"]
    assert not result["worker_tts_ready"] and result["phrase_adapter"]["source"] == "bundled"
    assert "worker instalado ainda não confirmado" in result["note"]
    assert not result["teto_synthesis_verified"] and not result["portuguese_speech_verified"]
    assert not result["box64_required"] and not result["voicepeak_required"]
    for command in calls:
        assert not any(word in command for word in ("apt", "apt-get", "install", "--configure", "--add-architecture"))


def test_amd64_guest_is_reported_without_executing_library(tmp_path, monkeypatch, doctor):
    calls = ready_report_dependencies(monkeypatch, doctor, tmp_path)
    original = doctor.bounded
    def run(command, timeout, *, env=None):
        if "--print-architecture" in command:
            calls.append(command)
            return {"ok": True, "code": 0, "output": "amd64\n"}
        return original(command, timeout, env=env)
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


def test_new_and_legacy_worldline_environment_keys_are_literal_only(tmp_path, doctor):
    environment = tmp_path / ".phone-worker.env"
    environment.write_text(
        "PHONE_WORKER_WORLDLINE_CONTAINER=worldline-arm64\n"
        "PHONE_WORKER_WORLDLINE_LIBRARY='/home/user/renderer/libworldline.so'\n"
        "PHONE_WORKER_TETO_WORLDLINE_CONTAINER=legacy-arm64\n"
        "PHONE_WORKER_TETO_WORLDLINE_LIBRARY='/home/user/old/libworldline.so'\n"
        "PHONE_WORKER_AUTH_TOKEN=secret\n"
    )
    values, metadata = doctor.read_teto_environment(environment)
    assert metadata["accepted_keys"] == 4
    assert values["PHONE_WORKER_WORLDLINE_CONTAINER"] == "worldline-arm64"
    assert "secret" not in json.dumps((values, metadata))


def test_adapter_environment_drops_authentication_values(monkeypatch, doctor):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-secret")
    monkeypatch.setenv("PHONE_WORKER_AUTH_TOKEN", "phone-secret")
    monkeypatch.setenv("HOME", "/home/test")
    environment = doctor.adapter_environment({"PHONE_WORKER_TETO_ENABLED": "1", "PHONE_WORKER_AUTH_TOKEN": "another-secret"})
    assert environment["HOME"] == "/home/test"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PHONE_WORKER_TETO_ENABLED"] == "1"
    assert "secret" not in json.dumps(environment)


def test_default_worker_uses_installed_new_modules_and_falls_back_for_legacy(tmp_path, monkeypatch, doctor):
    monkeypatch.setattr(doctor.Path, "home", lambda: tmp_path)
    installed = tmp_path / "phone-worker" / "teto_renderer"
    installed.mkdir(parents=True)
    (installed / "voicebank.py").write_text("# existing legacy index\n")
    assert doctor.default_worker_directory() == doctor.bundled_worker_directory()
    for name in ("__init__.py", "worldline.py", "worldline_native.py"):
        (installed / name).write_text("# presence only; later status proves importability\n")
    assert doctor.default_worker_directory() == tmp_path / "phone-worker"
    assert doctor.worker_source(tmp_path / "phone-worker") == "installed"
    assert doctor.worker_source(doctor.bundled_worker_directory()) == "bundled"
    assert doctor.worker_source(tmp_path / "other") == "explicit"


def test_worker_router_is_inspected_without_importing_worker(tmp_path, doctor):
    worker = tmp_path / "phone_worker.py"
    worker.write_text("raise RuntimeError('never import worker')\n# worldline-r WorldlineRenderer\n")
    assert not doctor.worker_backend_registered(tmp_path)
    worker.write_text(
        "raise RuntimeError('never import worker')\n"
        "def _teto_backend():\n    return 'worldline-r'\n"
        "def _get_teto_renderer():\n    if _teto_backend() == 'worldline-r':\n        from teto_renderer import WorldlineRenderer\n        return WorldlineRenderer()\n"
    )
    assert doctor.worker_backend_registered(tmp_path)


def test_real_adapter_is_importable_but_disabled_status_is_not_ready(tmp_path, monkeypatch, doctor):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "0")
    monkeypatch.setenv("PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR", str(tmp_path / "must-not-create-cache"))
    result = doctor.adapter_status(ROOT / "deploy/termux/phone-worker")
    assert result["importable"] and not result["status_ready"]
    assert not result["teto_synthesis_verified"] and not result["portuguese_speech_verified"]
    assert not (tmp_path / "must-not-create-cache").exists()
    assert not any(name.startswith("_worldline_adapter_audit") for name in sys.modules)


def test_real_adapter_status_requires_native_pin_and_never_renders_speech(tmp_path, monkeypatch, doctor):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "1")
    monkeypatch.setenv("PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR", str(tmp_path / "must-not-create-cache"))
    original_loader = doctor.load_bundled_source
    observed = []
    pin = doctor.load_runtime_module().RELEASE_SHA256
    def loader(path, name):
        module = original_loader(path, name)
        if name.endswith(".worldline"):
            renderer = module.WorldlineRenderer
            monkeypatch.setattr(renderer, "_library_metadata", lambda self: {
                "library_sha256": pin, "native_architecture": "arm64", "library": str(tmp_path / "libworldline.so")})
            monkeypatch.setattr(renderer, "_load_index", lambda self: types.SimpleNamespace(
                fingerprint="0" * 64, snapshot=lambda: {"aliases": 700, "root": str(tmp_path / "Teto")}))
            monkeypatch.setattr(module.shutil, "which", lambda executable: f"/usr/bin/{executable}")
            def run(command, *, deadline):
                observed.append(command)
                assert "--probe" in command and "--job" not in command and "--output" not in command
                return json.dumps({"ok": True, "api_verified": True, "abi_verified": True,
                                   "library_hash_verified": True, "library_sha256": pin,
                                   "native_architecture": "arm64", "source_commit": module.SOURCE_COMMIT,
                                   "source_version": module.SOURCE_VERSION}), ""
            monkeypatch.setattr(renderer, "_run", staticmethod(run))
            monkeypatch.setattr(renderer, "synthesize", lambda *args, **kwargs: pytest.fail("status must never synthesize"), raising=False)
        return module
    monkeypatch.setattr(doctor, "load_bundled_source", loader)
    result = doctor.adapter_status(ROOT / "deploy/termux/phone-worker")
    assert result["importable"] and result["status_ready"] and result["tts_ready"]
    assert result["library_sha256"] == pin and len(result["adapter_sha256"]) == 64
    assert observed and not (tmp_path / "must-not-create-cache").exists()
    assert not result["teto_synthesis_verified"] and not result["portuguese_speech_verified"]


def test_adapter_files_alone_do_not_establish_importability(tmp_path, doctor):
    source = tmp_path / "teto_renderer"
    source.mkdir()
    for name in ("__init__.py", "worldline.py", "worldline_native.py", "voicebank.py"):
        (source / name).write_text("invalid Python !!!\n")
    result = doctor.adapter_status(tmp_path)
    assert not result["importable"] and not result["status_ready"]


def test_real_adapter_errors_do_not_publish_arbitrary_exception_content(tmp_path, monkeypatch, doctor):
    source = tmp_path / "teto_renderer"
    source.mkdir()
    (source / "errors.py").write_text("raise ValueError('private-discord-token')\n")
    result = doctor.adapter_status(tmp_path)
    assert "private-discord-token" not in json.dumps(result)
    assert result["error_type"] == "ValueError"


def test_missing_adapter_status_blocks_tts_even_after_runtime_probe(tmp_path, monkeypatch, doctor):
    ready_report_dependencies(monkeypatch, doctor, tmp_path)
    original = doctor.bounded
    def run(command, timeout, *, env=None):
        if "--_adapter-status" in command:
            return {"ok": True, "output": json.dumps({"importable": False, "status_ready": False})}
        return original(command, timeout, env=env)
    monkeypatch.setattr(doctor, "bounded", run)
    result = doctor.build_report(library=tmp_path / "libworldline.so", voicebank=tmp_path / "Teto")
    assert result["runtime_ready"] and not result["tts_ready"]
    assert not result["phrase_adapter_available"]
    assert "phrase_adapter" in result["blockers"] and "adapter_status" in result["blockers"]


def test_readiness_requires_worldline_backend_and_enabled_setting(tmp_path, monkeypatch, doctor):
    ready_report_dependencies(monkeypatch, doctor, tmp_path)
    monkeypatch.setattr(doctor, "read_teto_environment", lambda: (
        {"PHONE_WORKER_TETO_ENABLED": "0", "PHONE_WORKER_TETO_BACKEND": "utau"}, {"present": True}))
    original = doctor.bounded
    def run(command, timeout, *, env=None):
        if "--_adapter-status" in command:
            assert env["PHONE_WORKER_TETO_BACKEND"] == "utau"
            return {"ok": True, "output": json.dumps({"importable": True, "status_ready": True})}
        return original(command, timeout, env=env)
    monkeypatch.setattr(doctor, "bounded", run)
    result = doctor.build_report(library=tmp_path / "libworldline.so", voicebank=tmp_path / "Teto")
    assert not result["tts_ready"]
    assert "teto_enabled" in result["blockers"] and "worldline_backend_selected" in result["blockers"]


def test_installed_worker_readiness_requires_router_registration(tmp_path, monkeypatch, doctor):
    ready_report_dependencies(monkeypatch, doctor, tmp_path)
    monkeypatch.setattr(doctor, "worker_source", lambda worker: "installed")
    monkeypatch.setattr(doctor, "worker_backend_registered", lambda worker: False)
    result = doctor.build_report(library=tmp_path / "libworldline.so", voicebank=tmp_path / "Teto")
    assert result["tts_ready"] and not result["worker_tts_ready"] and not result["kit_tts_ready"]
    monkeypatch.setattr(doctor, "worker_backend_registered", lambda worker: True)
    result = doctor.build_report(library=tmp_path / "libworldline.so", voicebank=tmp_path / "Teto")
    assert result["worker_tts_ready"]


def test_cli_adapter_status_stays_read_only_and_speech_unverified(monkeypatch, capsys, doctor):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "0")
    assert doctor.main(["--_adapter-status", "--worker-dir", str(ROOT / "deploy/termux/phone-worker")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["importable"] and result["read_only"]
    assert not result["tts_ready"] and not result["portuguese_speech_verified"]
