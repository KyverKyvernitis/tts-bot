from __future__ import annotations

import importlib.util
import io
import json
import math
import os
from pathlib import Path
import struct
from unittest.mock import Mock
import wave

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/scripts/validate-teto-assets.py"
spec = importlib.util.spec_from_file_location("_worldline_validator_test", SCRIPT)
assert spec and spec.loader
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def waveform(*, silent=False, rate=44100, width=2, channels=1):
    memory = io.BytesIO()
    with wave.open(memory, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        if width == 2:
            values = [0 if silent else round(5000 * math.sin(2 * math.pi * 440 * index / rate)) for index in range(1000)]
            pcm = b"".join(struct.pack("<h", value) for value in values) * channels
        else:
            pcm = bytes([128]) * 1000 * channels * width
        wav.writeframes(pcm)
    return memory.getvalue()


@pytest.fixture
def renderer(monkeypatch):
    for name in ("PHONE_WORKER_TETO_ENABLED", "PHONE_WORKER_TETO_BACKEND", "PHONE_WORKER_TETO_VOICEBANK_MODE",
                 "PHONE_WORKER_TETO_VOICEBANK_DIR", "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR",
                 "PHONE_WORKER_TETO_RESAMPLER_COMMAND", "PHONE_WORKER_WORLDLINE_CONTAINER", "PHONE_WORKER_WORLDLINE_LIBRARY"):
        monkeypatch.delenv(name, raising=False)
    instance = Mock()
    instance.status.return_value = {"ready": True, "backend": "worldline-r", "voicebank_profile": "standard"}
    instance.synthesize.return_value = {
        "audio": waveform(), "audio_format": "wav", "backend": "worldline-r",
        "native_phrase_render_verified": True, "voicebank": "Kasane Teto",
        "portuguese_speech_verified": True,  # Validator must override unproven quality claims.
    }
    import teto_renderer.worldline as module
    monkeypatch.setattr(module, "WorldlineRenderer", lambda **kwargs: instance)
    return instance


def run(capsys, *extra):
    code = validator.main(["--voicebank", "/bank/teto", "--backend", "worldline-r", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_worldline_requires_no_utau_resampler_and_sets_canonical_runtime_environment(renderer, capsys, tmp_path):
    library = tmp_path / "libworldline.so"
    code, report = run(capsys, "--container", "native-arm64", "--library", str(library))
    assert code == 0
    assert report["backend"] == "worldline-r"
    assert report["teto_synthesis_verified"] is False
    assert report["portuguese_speech_verified"] is False
    assert os.environ["PHONE_WORKER_TETO_BACKEND"] == "worldline-r"
    assert os.environ["PHONE_WORKER_WORLDLINE_CONTAINER"] == "native-arm64"
    assert os.environ["PHONE_WORKER_WORLDLINE_LIBRARY"] == str(library)
    assert "PHONE_WORKER_TETO_RESAMPLER_COMMAND" not in os.environ
    assert os.environ["PHONE_WORKER_TETO_VOICEBANK_DIR"] == "/bank/teto"
    renderer.synthesize.assert_not_called()


def test_actual_native_wav_is_saved_and_proves_synthesis_but_not_portuguese_quality(renderer, capsys, tmp_path):
    output, report_path = tmp_path / "teto.wav", tmp_path / "proof.json"
    code, report = run(capsys, "--render-test", "--text", "Olá, Brasil!", "--output", str(output), "--report", str(report_path), "--timeout", "45")
    assert code == 0
    assert output.read_bytes() == renderer.synthesize.return_value["audio"]
    assert json.loads(report_path.read_text()) == report
    assert report["teto_synthesis_verified"] is True
    assert report["portuguese_speech_verified"] is False
    assert report["portuguese_quality_verified"] is False
    render = report["render_test"]
    assert render["wav_verified"] is True and render["sample_frames"] == 1000
    assert render["backend"] == "worldline-r"
    assert render["teto_synthesis_verified"] is True
    assert render["portuguese_speech_verified"] is False
    assert "audio" not in render
    assert render["sha256"] and render["bytes"] == len(output.read_bytes())
    renderer.synthesize.assert_called_once_with("Olá, Brasil!", timeout_seconds=45.0, max_audio_bytes=8 * 1024 * 1024)
    assert not list(tmp_path.glob(".*"))


def test_report_and_wav_save_work_without_os_link(renderer, capsys, tmp_path, monkeypatch):
    monkeypatch.delattr(validator.os, "link", raising=False)
    code, _ = run(capsys, "--render-test", "--output", str(tmp_path / "teto.wav"), "--report", str(tmp_path / "proof.json"))
    assert code == 0


@pytest.mark.parametrize("audio", [
    b"RIFFinvalid-wave-bytes", waveform(silent=True), waveform(rate=22050), waveform(width=1),
    waveform(channels=2), waveform()[:-10], b"x" * (8 * 1024 * 1024 + 1), b"", "invalid-audio-type",
])
def test_invalid_silent_truncated_or_oversized_audio_never_proves_synthesis_or_overwrites_output(renderer, capsys, tmp_path, audio):
    renderer.synthesize.return_value["audio"] = audio
    output = tmp_path / "teto.wav"
    output.write_bytes(b"preserve-existing-output")
    code, report = run(capsys, "--render-test", "--output", str(output))
    assert code == 3
    assert report["teto_synthesis_verified"] is False
    assert report["portuguese_speech_verified"] is False
    assert output.read_bytes() == b"preserve-existing-output"
    assert "error" in report
    assert not list(tmp_path.glob(".*"))


@pytest.mark.parametrize("bad", [{"native_phrase_render_verified": False}, {"native_phrase_render_verified": 1}, {"backend": "utau"}])
def test_unconfirmed_native_phrase_or_other_backend_is_not_worldline_proof(renderer, capsys, bad):
    renderer.synthesize.return_value.update(bad)
    code, report = run(capsys, "--render-test")
    assert code == 3 and not report["teto_synthesis_verified"]


def test_unavailable_worldline_saves_failed_status_report_without_rendering(renderer, capsys, tmp_path):
    renderer.status.return_value = {"ready": False, "last_error": "library missing"}
    path = tmp_path / "failed-proof.json"
    code, report = run(capsys, "--render-test", "--report", str(path))
    assert code == 2
    assert json.loads(path.read_text())["status"]["last_error"] == "library missing"
    assert not report["teto_synthesis_verified"]
    renderer.synthesize.assert_not_called()


def test_renderer_error_saves_failure_json_without_staged_wav(renderer, capsys, tmp_path):
    renderer.synthesize.side_effect = TimeoutError("native deadline")
    code, report = run(capsys, "--render-test", "--output", str(tmp_path / "teto.wav"), "--report", str(tmp_path / "failure.json"))
    assert code == 3
    assert "TimeoutError" in report["error"]
    assert json.loads((tmp_path / "failure.json").read_text())["teto_synthesis_verified"] is False
    assert not (tmp_path / "teto.wav").exists()
    assert not list(tmp_path.glob(".*"))


def test_audit_uses_requested_timeout_and_reports_only_actual_audio_proof(renderer, capsys, tmp_path):
    directory = tmp_path / "audit"
    code, report = run(capsys, "--audit-dir", str(directory), "--timeout", "12.5")
    assert code == 0
    assert report["audit"]["rendered"] == 10
    assert report["audit"]["backend"] == "worldline-r"
    assert report["audit"]["limits"]["timeout_seconds_per_phrase"] == 12.5
    assert report["teto_synthesis_verified"] is True
    assert report["audit"]["portuguese_quality_verified"] is False
    assert len(list(directory.glob("*.wav"))) == 10
    assert len(list(directory.glob("*.json"))) == 11
    for call in renderer.synthesize.call_args_list:
        assert call.kwargs == {"timeout_seconds": 12.5, "max_audio_bytes": 8 * 1024 * 1024}
    assert not list(directory.glob(".*"))


def test_failed_audit_rerun_removes_stale_wav_and_saves_error(renderer, capsys, tmp_path):
    directory = tmp_path / "audit"
    directory.mkdir()
    stale = directory / "01-nasais-palatais.wav"
    stale.write_bytes(b"old-success")
    rendered = renderer.synthesize.return_value
    renderer.synthesize.side_effect = [TimeoutError("timed out"), *([rendered] * 9)]
    code, report = run(capsys, "--audit-dir", str(directory))
    assert code == 3 and report["audit"]["failed"] == 1
    assert not stale.exists()
    assert json.loads((directory / "01-nasais-palatais.json").read_text())["ok"] is False
    assert report["teto_synthesis_verified"] is True  # Other nine phrases actually rendered.


@pytest.mark.parametrize("timeout", ["0", "-1", "121", "nan", "inf"])
def test_invalid_timeout_is_rejected_before_renderer(renderer, capsys, timeout):
    with pytest.raises(SystemExit) as raised:
        run(capsys, "--timeout", timeout)
    assert raised.value.code == 2
    renderer.status.assert_not_called()


def test_utau_still_requires_explicit_resampler_and_worldline_does_not(renderer, capsys):
    with pytest.raises(SystemExit) as raised:
        validator.main(["--voicebank", "/bank"])
    assert raised.value.code == 2
    renderer.status.assert_not_called()


def test_worldline_english_voicebank_profile_keeps_standard_fallback_environment(renderer, capsys, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_VOICEBANK_DIR", "/existing-standard")
    code, _ = run(capsys, "--mode", "auto")
    assert code == 0
    assert os.environ["PHONE_WORKER_TETO_VOICEBANK_DIR"] == "/existing-standard"
    assert os.environ["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] == "/bank/teto"
    assert os.environ["PHONE_WORKER_TETO_VOICEBANK_MODE"] == "auto"


def test_invalid_guest_container_rejected_before_renderer(renderer, capsys):
    with pytest.raises(SystemExit) as raised:
        run(capsys, "--container", "bad:container")
    assert raised.value.code == 2
    renderer.status.assert_not_called()


def test_identical_report_and_wav_path_cannot_destroy_audio(renderer, capsys, tmp_path):
    path = str(tmp_path / "same-file")
    with pytest.raises(SystemExit) as raised:
        run(capsys, "--render-test", "--output", path, "--report", path)
    assert raised.value.code == 2
    renderer.synthesize.assert_not_called()


def test_report_parent_must_exist_without_creating_it(renderer, capsys, tmp_path):
    path = tmp_path / "missing-parent" / "proof.json"
    code, report = run(capsys, "--report", str(path))
    assert code == 3
    assert "FileNotFoundError" in report["report_error"]
    assert not path.parent.exists()


@pytest.mark.parametrize("target", ["output", "report"])
def test_symlink_destination_preserves_target(renderer, capsys, tmp_path, target):
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep-this-file")
    link = tmp_path / "symlink"
    link.symlink_to(protected)
    arguments = ["--report", str(link)] if target == "report" else ["--render-test", "--output", str(link)]
    code, report = run(capsys, *arguments)
    assert code == 3
    assert protected.read_bytes() == b"keep-this-file"
    assert not list(tmp_path.glob(".*"))


def test_atomic_output_failure_keeps_previous_audio_and_cleans_staging(renderer, capsys, tmp_path, monkeypatch):
    output = tmp_path / "teto.wav"
    output.write_bytes(b"old-file")
    monkeypatch.setattr(validator.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("storage failed")))
    code, report = run(capsys, "--render-test", "--output", str(output))
    assert code == 3
    assert "storage failed" in report["error"]
    assert output.read_bytes() == b"old-file"
    assert not list(tmp_path.glob(".*"))


def test_validator_does_not_modify_phone_worker_configuration_files(renderer, capsys, tmp_path):
    worker_env = tmp_path / ".phone-worker.env"
    worker_env.write_bytes(b"PHONE_WORKER_TETO_BACKEND=utau\nTOKEN=not-for-report\n")
    before = worker_env.read_bytes()
    code, report = run(capsys, "--render-test", "--report", str(tmp_path / "proof.json"))
    assert code == 0
    assert worker_env.read_bytes() == before
    assert "not-for-report" not in json.dumps(report)


def test_private_render_signature_preserves_old_arguments_and_accepts_optional_timeout(renderer):
    report = validator._render(renderer, "teto", timeout_seconds=18)
    assert report["backend"] == "worldline-r"
    assert report["teto_synthesis_verified"] is True
    renderer.synthesize.assert_called_once_with("teto", timeout_seconds=18, max_audio_bytes=8 * 1024 * 1024)
