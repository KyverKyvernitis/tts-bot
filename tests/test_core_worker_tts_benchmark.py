"""Benchmark dispatch must preserve the requested native synthesis inputs."""
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"


@pytest.mark.parametrize("engine", ["android_native", "android-native"])
def test_android_benchmark_delegates_once_without_applying_fallback_inputs(monkeypatch, engine):
    spec = importlib.util.spec_from_file_location("phone_benchmark_test", PHONE)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    handler = object.__new__(worker.WorkerHandler)
    handler.server = SimpleNamespace(job_timeout=30, max_output_bytes=4096)
    monkeypatch.setattr(handler, "_ensure_tts_benchmark_turbo_allowed", lambda: (["turbo"], ["tts"]))
    body = {"engine": engine, "text": "Teste.", "voice": "pt-BR-native", "language": "pt-BR",
            "rate": 1.2, "pitch": 0.9, "cache_key": "requested-cache", "cache_mode": "only",
            "fallback_voice": "another", "fallback_language": "en-US", "fallback_rate": 2.0,
            "fallback_pitch": 2.0, "timeout_seconds": 4, "max_audio_bytes": 2048}
    calls = []
    audio = base64.b64encode(b"cached-native-audio").decode()

    def synthesize(payload, **kwargs):
        calls.append((payload, kwargs))
        return {"ok": True, "cache_hit": True, "cache_key": payload["cache_key"],
                "data_b64": audio, "logs": ["cache hit"]}

    monkeypatch.setattr(handler, "_synthesize_standard_tts_bytes", synthesize)
    result = handler._task_tts_synthesize_benchmark(body)

    assert len(calls) == 1
    payload, settings = calls[0]
    assert payload == body and payload is not body
    assert settings["engine"] == "android_native"
    assert settings["roles"] == ["turbo"] and settings["capabilities"] == ["tts"]
    assert settings["timeout"] == 4 and settings["max_audio_bytes"] == 2048
    assert result["ok"] and result["cache_hit"]
    assert result["engine"] == "android_native" and result["cache_key"] == "requested-cache"
    assert result["data_b64"] == audio
