from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import time
import types
import wave

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/esper-utau/termux/compare-esper.py"


@pytest.fixture
def comparison():
    module = types.ModuleType("esper_comparison_test")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), "exec"), module.__dict__)
    return module


def pcm(*, silence=False, channels=1, rate=44100):
    result = io.BytesIO()
    with wave.open(result, "wb") as writer:
        writer.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        values = [0 if silence else round(6000 * math.sin(2 * math.pi * 260 * n / rate)) for n in range(4410)]
        writer.writeframes(b"".join(struct.pack("<h", v) for v in values) * channels)
    return result.getvalue()


def job(tmp_path, *, engine="esper-utau"):
    root = tmp_path / "esper"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "resampler.py").write_bytes(b"# test fixture\n")
    return {"engine": engine, "mode": "english", "voicebank": str(tmp_path / "bank"),
            "esper_root": str(root), "worldline_library": str(tmp_path / "libworldline.so"),
            "container": "voicepeak-arm64", "timeout": 10, "deadline": time.monotonic() + 10,
            "text": "Eu sou Teto.", "output": str(tmp_path / "audio.wav")}


def test_controls_pin_defaults_isolate_cache_and_force_english(tmp_path, comparison):
    request = job(tmp_path)
    controls = comparison.controls(request)
    assert controls["PHONE_WORKER_TETO_BACKEND"] == "utau"
    assert controls["PHONE_WORKER_TETO_VOICEBANK_MODE"] == "english"
    assert controls["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] == request["voicebank"]
    assert controls["PHONE_WORKER_TETO_BASE_PITCH"] == "C4"
    assert controls["PHONE_WORKER_TETO_SPEECH_RATE"] == "1.0"
    assert controls["PHONE_WORKER_TETO_FLAGS"] == ""
    assert float(controls["PHONE_WORKER_ESPER_DEADLINE"]) == request["deadline"]
    assert "bin/resampler.py" in controls["PHONE_WORKER_TETO_RESAMPLER_COMMAND"]
    before = controls["PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR"]
    (Path(request["esper_root"]) / "bin" / "resampler.py").write_bytes(b"# changed wrapper\n")
    assert comparison.controls(request)["PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR"] != before


def renderer_factory(*, audio=None, profile="english-cvvc", ready=True, native=True):
    class Renderer:
        def __init__(self, resource_guard):
            assert resource_guard()["ok"]

        @staticmethod
        def _run(command, *, deadline):
            return "proof", ""

        def status(self, force):
            assert force
            return {"ready": ready, "voicebank_profile": profile, "last_error": "missing runtime"}

        def synthesize(self, text, **kwargs):
            assert kwargs["pitch_offset_semitones"] == 0.0
            assert 0 < kwargs["timeout_seconds"] <= 10
            return {"audio": pcm() if audio is None else audio, "voicebank_profile": profile,
                    "backend": "worldline-r", "native_phrase_render_verified": native}
    return Renderer


def test_verified_render_preserves_environment_and_saves_actual_pcm(tmp_path, comparison, monkeypatch):
    request = job(tmp_path)
    monkeypatch.setattr(comparison, "load_renderers", lambda: {"esper-utau": renderer_factory()})
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    monkeypatch.setenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.3")
    before = dict(os.environ)
    result = comparison.render_job(request)
    assert result["ok"] and result["teto_synthesis_verified"]
    assert not result["portuguese_quality_verified"] and not result["worker_configuration_changed"]
    assert Path(request["output"]).read_bytes() == pcm()
    assert dict(os.environ) == before


@pytest.mark.parametrize("options", [{"audio": b"bad"}, {"audio": pcm(silence=True)},
                                    {"audio": pcm(channels=2)}, {"audio": pcm(rate=22050)},
                                    {"profile": "standard"}, {"ready": False},
                                    {"native": False}])
def test_bad_audio_profile_or_native_proof_preserves_existing_output(tmp_path, comparison, monkeypatch, options):
    request = job(tmp_path, engine="worldline-r" if "native" in options else "esper-utau")
    destination = Path(request["output"])
    destination.write_bytes(b"preserved previous output")
    monkeypatch.setattr(comparison, "load_renderers", lambda: {request["engine"]: renderer_factory(**options)})
    with pytest.raises(ValueError):
        comparison.render_job(request)
    assert destination.read_bytes() == b"preserved previous output"


def test_cli_independent_failure_does_not_change_env_or_reuse_old_audio(tmp_path, comparison, monkeypatch, capsys):
    monkeypatch.setattr(comparison.Path, "home", lambda: tmp_path)
    envfile = tmp_path / ".phone-worker.env"
    envfile.write_bytes(b"PHONE_WORKER_TETO_BACKEND=worldline-r\nTOKEN=private\n")
    output_dir = tmp_path / "results"
    output_dir.mkdir()
    (output_dir / "04-worldline-english.wav").write_bytes(b"stale")
    (output_dir / "05-esper-english.mp3").write_bytes(b"stale")
    jobs = []

    def run(request):
        jobs.append(request)
        if request["engine"] == "worldline-r":
            raise TimeoutError("fixture timeout")
        Path(request["output"]).write_bytes(pcm())
        return {"ok": True, "comparison_engine": "esper-utau", "teto_synthesis_verified": True}

    monkeypatch.setattr(comparison, "run_job", run)
    before = dict(os.environ)
    assert comparison.main(["--output-dir", str(output_dir)]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["rendered"] == 1
    assert "fixture timeout" in summary["samples"][0]["error"]
    assert summary["samples"][1]["ok"]
    assert len(jobs) == 2 and jobs[0]["text"] == jobs[1]["text"]
    assert not (output_dir / "04-worldline-english.wav").exists()
    assert not (output_dir / "05-esper-english.mp3").exists()
    assert envfile.read_bytes() == b"PHONE_WORKER_TETO_BACKEND=worldline-r\nTOKEN=private\n"
    assert dict(os.environ) == before


@pytest.mark.parametrize("worldline_fails", [False, True])
def test_default_comparison_budget_respects_native_timeout_without_shortening_esper(
        tmp_path, comparison, monkeypatch, capsys, worldline_fails):
    clock = [100.0]
    jobs, rendered_timeouts, native_deadlines, esper_deadlines = [], {}, [], []
    monkeypatch.setattr(comparison.time, "monotonic", lambda: clock[0])
    root = tmp_path / "esper"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "resampler.py").write_bytes(b"# comparison fixture\n")

    def factory(engine):
        class Renderer:
            def __init__(self, resource_guard):
                assert resource_guard()["ok"]

            @staticmethod
            def _run(command, *, deadline):
                native_deadlines.append(deadline)
                return "proof", ""

            def status(self, force):
                assert force
                clock[0] += 2  # Runtime diagnostics consume part of each budget.
                if engine == "worldline-r" and worldline_fails:
                    return {"ready": False, "last_error": "fixture runtime failure"}
                return {"ready": True, "voicebank_profile": "english-cvvc"}

            def synthesize(self, text, *, timeout_seconds, **kwargs):
                rendered_timeouts[engine] = timeout_seconds
                if engine == "worldline-r":
                    if not 0 < timeout_seconds <= 120:
                        raise ValueError("timeout Teto deve estar entre 0 e 120 segundos")
                    self._run([], deadline=clock[0] + 300)
                else:
                    esper_deadlines.append(float(os.environ["PHONE_WORKER_ESPER_DEADLINE"]))
                return {"audio": pcm(), "voicebank_profile": "english-cvvc",
                        "backend": "worldline-r" if engine == "worldline-r" else "utau",
                        "native_phrase_render_verified": engine == "worldline-r"}
        return Renderer

    def run(request):
        jobs.append(request)
        return comparison.render_job(request)

    monkeypatch.setattr(comparison, "load_renderers", lambda: {
        engine: factory(engine) for engine in ("worldline-r", "esper-utau")})
    monkeypatch.setattr(comparison, "run_job", run)
    environment_before = dict(os.environ)
    assert comparison.main(["--esper-root", str(root), "--output-dir", str(tmp_path / "results")]) == (
        2 if worldline_fails else 0)
    summary = json.loads(capsys.readouterr().out)
    assert all(request["timeout"] == 180 for request in jobs)
    assert rendered_timeouts["esper-utau"] == 178.0
    assert esper_deadlines == [jobs[1]["deadline"]]
    if worldline_fails:
        assert "fixture runtime failure" in summary["samples"][0]["error"]
        assert summary["rendered"] == 1
    else:
        assert rendered_timeouts["worldline-r"] == 120.0
        assert native_deadlines == [jobs[0]["deadline"]]
        assert summary["rendered"] == 2
    assert summary["samples"][1]["ok"]
    assert dict(os.environ) == environment_before


def test_existing_symlink_is_preserved_and_never_rendered(tmp_path, comparison, monkeypatch, capsys):
    saved = tmp_path / "saved"
    saved.write_bytes(b"keep")
    (tmp_path / "05-esper-english.wav").symlink_to(saved)
    monkeypatch.setattr(comparison, "run_job", lambda *_: pytest.fail("rendering a symlink destination"))
    assert comparison.main(["--esper-only", "--output-dir", str(tmp_path)]) == 2
    assert saved.read_bytes() == b"keep"
    summary = json.loads(capsys.readouterr().out)
    assert "preservado" in summary["samples"][0]["error"]


def test_proot_false_zero_signal_and_timeout_are_failures(comparison):
    with pytest.raises(ValueError, match="processo de áudio falhou"):
        comparison.bounded([sys.executable, "-c", "print('proot info: terminated with signal 11')"], 5)
    with pytest.raises(Exception) as exc:
        comparison.bounded([sys.executable, "-c", "import time; time.sleep(10)"], 0.05)
    assert type(exc.value).__name__ == "TimeoutExpired"


def test_real_mp3_encoding_is_valid_and_keeps_wav(tmp_path, comparison):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires local FFmpeg")
    source, output = tmp_path / "audio.wav", tmp_path / "audio.mp3"
    original = pcm()
    source.write_bytes(original)
    result = comparison.encode_mp3(source, output)
    assert source.read_bytes() == original and result["bytes"] == output.stat().st_size
    stdout, _ = comparison.bounded(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels",
                                    "-of", "json", str(output)], 5)
    stream = json.loads(stdout)["streams"][0]
    assert stream == {"codec_name": "mp3", "sample_rate": "44100", "channels": 1}


@pytest.mark.parametrize("arguments", [["--timeout", "nan"], ["--timeout", "0"], ["--timeout", "301"],
                                      ["--container", "bad;command"], ["--text", " "], ["--text", "x" * 181]])
def test_invalid_cli_rejected_before_any_work(comparison, arguments, monkeypatch):
    monkeypatch.setattr(comparison, "load_validator", lambda: pytest.fail("validation happened too late"))
    with pytest.raises(SystemExit) as exc:
        comparison.main(arguments)
    assert exc.value.code == 2
