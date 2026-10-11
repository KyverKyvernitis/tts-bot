"""Parallel fragments preserve PCM, source serialization and reusable caches."""
from __future__ import annotations

import array
import importlib.util
import os
from pathlib import Path
import sys
import threading
import wave

import pytest

from test_miku_renderer import TetoRenderer, settings

ROOT = Path(__file__).resolve().parents[1]
TEXT = "Miku Teto echo Miku"


def assets(root, *, miku):
    path = root / "bank"
    path.mkdir()
    aliases = ["m i:", "k u:", "t e", "t u:", "Sil e", "S u:"] if miku else ["み", "く", "て", "と", "え", "しゅ"]
    lines = []
    for number, alias in enumerate(aliases):
        source = path / f"source-{number}.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(array.array("h", [(-1) ** number * (900 + number * 100)] * 13230).tobytes())
        lines.append(f"{source.name}={alias}{'_D4' if miku else ''},0,20,-300,20,10\n")
    (path / "oto.ini").write_text("".join(lines))
    return settings(root, path, miku=miku)


@pytest.mark.parametrize("voice,requested,expected", [
    ("hatsune-miku", None, 4), ("hatsune-miku", "2", 2),
    ("hatsune-miku", "100", 4), ("hatsune-miku", "0", 1),
    ("hatsune-miku", "invalid", 4), ("kasane-teto", "100", 2),
])
def test_pool_limits_and_same_source_serialization(tmp_path, monkeypatch, voice, requested, expected):
    env = assets(tmp_path, miku=voice == "hatsune-miku")
    if requested is not None:
        env["PHONE_WORKER_TETO_RENDER_THREADS"] = requested
    renderer = TetoRenderer(environment=env, voice_id=voice)
    original = renderer._resample_note
    condition = threading.Condition()
    gate = threading.Event()
    active = set()
    peak = 0

    def tracked(**kwargs):
        nonlocal peak
        source = kwargs["entry"].wav_path
        with condition:
            assert source not in active, "notes sharing a WAV raced its analysis cache"
            active.add(source)
            peak = max(peak, len(active))
            if len(active) == expected:
                gate.set()
        try:
            assert gate.wait(5), "independent groups were not admitted concurrently"
            return original(**kwargs)
        finally:
            with condition:
                active.remove(source)

    monkeypatch.setattr(renderer, "_resample_note", tracked)
    result = renderer.synthesize(TEXT, timeout_seconds=15)
    assert peak == result["render_workers"] == expected
    assert not result["missing_phonemes"]
    assert result["source_groups"] >= expected
    assert result["fragment_cache_misses"] == result["rendered_phonemes"]
    assert result["resampler_calls"] == result["fragment_cache_misses"]
    assert result["fragment_cache_hits"] == 0
    assert not hasattr(renderer._reading_context, "render_metrics")
    assert not active


def test_four_workers_reuse_two_worker_fragments_and_preserve_exact_audio(tmp_path, monkeypatch):
    env = assets(tmp_path, miku=True)
    two = TetoRenderer(environment={**env, "PHONE_WORKER_TETO_RENDER_THREADS": "2"}, voice_id="hatsune-miku")
    initial = two.synthesize(TEXT, timeout_seconds=15)
    four = TetoRenderer(environment={**env, "PHONE_WORKER_TETO_RENDER_THREADS": "4"}, voice_id="hatsune-miku")
    # A cache hit must never start Python/PRoot/CoreCLR or change its voice identity.
    import teto_renderer.renderer as module
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: pytest.fail("cached fragments invoked a subprocess"))
    repeated = four.synthesize(TEXT, timeout_seconds=15)
    assert initial["audio"] == repeated["audio"]
    assert initial["renderer_fingerprint"] == repeated["renderer_fingerprint"]
    assert repeated["fragment_cache_hits"] == initial["rendered_phonemes"]
    assert repeated["fragment_cache_misses"] == repeated["resampler_calls"] == 0
    assert repeated["render_workers"] == 4
    assert not hasattr(four._reading_context, "render_metrics")


@pytest.mark.parametrize("configured", [False, True])
def test_validation_uses_worker_fragment_cache_without_overwriting_preferences(tmp_path, monkeypatch, configured):
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    installer = load("miku_perf_installer", ROOT / "deploy/miku-utau/termux/install-voicebank.py")
    validator = load("miku_perf_validator", ROOT / "deploy/miku-utau/termux/validate-miku.py")
    worker = load("miku_perf_worker", ROOT / "deploy/termux/phone-worker/phone_worker.py")
    bridge = tmp_path / "esper/bin/resampler.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_bytes((ROOT / "deploy/esper-utau/termux/resampler.py").read_bytes())
    values = installer.configuration(tmp_path / "bank", {}, container="voicepeak-arm64", esper_root=bridge.parent.parent)
    if configured:
        values.update(PHONE_WORKER_MIKU_RENDER_THREADS="2", PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR=str(tmp_path / "custom fragments"))
    original = ("# user settings\nTOKEN=not-for-output\n" + "".join(f"{key}={installer.shell_value(value)}\n" for key, value in values.items())).encode()
    env = tmp_path / "worker.env"
    env.write_bytes(original)
    _, saved = installer.read_environment(env)
    for key in list(os.environ):
        if key.startswith("PHONE_WORKER_MIKU_"):
            monkeypatch.delenv(key)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    profile = validator.controls(saved, directory=tmp_path / "bank", container="voicepeak-arm64", timeout=120)
    bot = worker._miku_renderer_environment()
    for key in ("PHONE_WORKER_TETO_RENDER_THREADS", "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR", "PHONE_WORKER_TETO_RESAMPLER_COMMAND"):
        assert profile[key] == bot[key]
    assert profile["PHONE_WORKER_TETO_RENDER_THREADS"] == ("2" if configured else "4")
    assert env.read_bytes() == original and "TOKEN" not in saved
