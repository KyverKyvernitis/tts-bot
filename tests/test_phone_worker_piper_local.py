"""Piper phase 1: compatible with Termux CLI, no resident process or APK."""
from __future__ import annotations

import array
import os
import sys
import wave
from pathlib import Path

import pytest

from test_phone_worker_tts_lifecycle import tts


def _fake_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "piper"
    executable.write_text("""#!/usr/bin/env python3
import array, pathlib, sys
args = sys.argv[1:]
assert '-m' in args and '-f' in args and '--' in args
out = pathlib.Path(args[args.index('-f') + 1])
samples = array.array('f', [0.25] * 2500)
out.write_bytes(samples.tobytes())
""")
    executable.chmod(0o755)
    return executable


def _model(tmp_path: Path) -> Path:
    model = tmp_path / "pt_BR-faber-medium.onnx"
    model.write_bytes(b"fake-model")
    (tmp_path / (model.name + ".json")).write_text('{"audio":{"sample_rate":22050}}')
    return model


def test_termux_piper_binary_generates_valid_wave(tmp_path, monkeypatch):
    import importlib.util
    path = Path(__file__).parents[1] / "deploy/termux/phone-worker/phone_worker_runtime/tts_piper.py"
    spec = importlib.util.spec_from_file_location("tts_piper_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    synthesize_to_wav = module.synthesize_to_wav

    model = _model(tmp_path)
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(_fake_cli(tmp_path)))
    destination = tmp_path / "speech.wav"
    synthesize_to_wav("Olá, pessoal", str(model), str(model) + ".json", destination, timeout_seconds=8)
    with wave.open(str(destination), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 22050
        assert wav.getnframes() == 2500


def test_piper_task_runs_in_python_with_native_rust_enabled(tts, tmp_path, monkeypatch):
    # The native Rust worker deliberately does not synthesize Piper.
    _fake_cli(tmp_path)
    model = _model(tmp_path)
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(tmp_path / "piper"))
    monkeypatch.setenv("PHONE_WORKER_PIPER_MODEL", str(model))
    monkeypatch.setenv("PHONE_WORKER_PIPER_CONFIG", str(model) + ".json")
    monkeypatch.setenv("PHONE_WORKER_PIPER_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_PIPER_OUTPUT_FORMAT", "wav")
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    monkeypatch.setattr(tts.worker, "_native_tts_task", lambda *_args: pytest.fail("Piper delegado ao Rust"))
    tts.handler.server.max_output_bytes = 1024 * 1024
    result = tts.handler._task_tts_synthesize_piper({
        "text": "Teste Piper", "engine": "piper", "cache_enabled": False,
        "timeout_seconds": 10, "max_audio_bytes": 1024 * 1024,
    })
    assert result["ok"] and result["engine"] == "piper"
    assert result["audio_format"] == "wav"
    import base64
    payload = base64.b64decode(result["data_b64"])
    assert payload.startswith(b"RIFF") and len(payload) > 44


def test_disabled_piper_is_not_synthesized(tts, tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_PIPER_ENABLED", "false")
    with pytest.raises(RuntimeError, match="desativado"):
        tts.handler._task_tts_synthesize_piper({"text": "Olá"})
