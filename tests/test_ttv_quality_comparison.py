from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/ttv-quality/termux/compare-quality.py"


def load_script():
    spec = importlib.util.spec_from_file_location("ttv_quality_comparison_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    module = load_script()
    helper = module.installer()
    audio = module.audio_helper()
    runtime = tmp_path / "runtime"
    bridge = runtime / "bin/resampler.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("import argparse\np = argparse.ArgumentParser()\n" + "\n".join(
        "p.add_argument(" + repr(option) + ")" for option in ("--root", "--cache-dir", "--container", "--timeout")))
    banks = {voice: tmp_path / (voice + "-bank") for voice in ("teto", "miku")}
    for bank in banks.values():
        bank.mkdir()
        (bank / "source.wav").write_bytes(b"original bank")
    production = tmp_path / "production-cache"
    production.mkdir()
    (production / "keep.esp").write_bytes(b"production analysis")
    command = shlex.join([sys.executable, str(bridge), "--cache-dir=" + str(production), "--implementation-id", "stale"])
    env = tmp_path / "worker.env"
    env.write_text("TOKEN=never-print-this\n" + "\n".join([
        'PHONE_WORKER_TETO_FLAGS="Gb-8B0T0"', 'PHONE_WORKER_MIKU_FLAGS="B25T10"',
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND=" + helper.shell_value(command),
        "PHONE_WORKER_MIKU_RESAMPLER_COMMAND=" + helper.shell_value(command),
        "PHONE_WORKER_ESPER_CACHE_DIR=" + helper.shell_value(str(production)),
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR=" + helper.shell_value(str(banks["teto"])),
        "PHONE_WORKER_MIKU_ENGLISH_VOICEBANK_DIR=" + helper.shell_value(str(banks["miku"])),
    ]), encoding="utf-8")
    before, after = tmp_path / "before-worker", tmp_path / "after-worker"
    for directory, value in ((before, "before"), (after, "after")):
        source = directory / "teto_renderer"
        source.mkdir(parents=True)
        (source / "renderer.py").write_text(repr(value))
    monkeypatch.setattr(module, "BASELINE_WORKER", before)
    monkeypatch.setattr(module, "CURRENT_WORKER", after)
    monkeypatch.setattr(module, "installer", lambda: helper)
    monkeypatch.setattr(module, "audio_helper", lambda: audio)
    monkeypatch.setattr(module.shutil, "which", lambda binary: None if binary == "termux-media-scan" else binary)
    calls = []

    def run_sample(job):
        effective = module.controls(helper, job["settings"], voice=job["voice"], directory=Path(job["voicebank"]),
            container=job["container"], analysis=Path(job["analysis"]), fragments=Path(job["fragments"]),
            timeout=job["timeout"], esper_root=Path(job["esper_root"]) if job["esper_root"] else None)
        analysis, fragments = Path(job["analysis"]), Path(job["fragments"])
        analysis.mkdir(parents=True, exist_ok=True)
        fragments.mkdir(parents=True, exist_ok=True)
        (analysis / (job["voice"] + ".esp")).write_bytes(b"temporary analysis")
        (fragments / "fragment.wav").write_bytes(b"temporary fragment")
        Path(job["output"]).write_bytes(b"sample PCM " + job["revision"].encode())
        calls.append((job, effective))
        return {"ok": True, "id": job["id"], "voice_id": module.VOICES[job["voice"]][0],
                "revision": job["revision"], "synthesis_verified": True, "portuguese_quality_verified": False,
                "render": {"sha256": hashlib.sha256(Path(job["output"]).read_bytes()).hexdigest()}}

    def mp3(wav, output):
        output.write_bytes(b"verified MP3")
        return {"mp3_verified": True, "output": str(output)}

    monkeypatch.setattr(module, "run_sample", run_sample)
    monkeypatch.setattr(audio, "encode_mp3", mp3)
    return module, helper, audio, banks, runtime, env, production, calls


def perform(prepared, **changes):
    module, _, _, _, _, env, _, _ = prepared
    arguments = {"env": env, "output": env.parent / "samples", "container": "voicepeak-arm64", "timeout": 120}
    arguments.update(changes)
    return module.compare(**arguments)


def test_comparison_keeps_profiles_configuration_banks_and_production_caches_independent(prepared, monkeypatch):
    module, _, _, banks, _, env, production, calls = prepared
    monkeypatch.setenv("PHONE_WORKER_TETO_FLAGS", "unrelated parent flags")
    monkeypatch.setenv("PHONE_WORKER_MIKU_FLAGS", "unrelated other voice flags")
    original_env, process_env = env.read_bytes(), dict(os.environ)
    report = perform(prepared)
    assert report["ok"] and report["rendered"] == 4 and not report["skipped"]
    assert report["temporary_cache_only"] and report["temporary_files_removed"]
    assert report["worker_configuration_changed"] is report["production_caches_changed"] is False
    assert report["portuguese_quality_verified"] is report["english_quality_verified"] is False
    assert report["renderer_sources"]["anterior"] != report["renderer_sources"]["atual"]
    assert len({job["analysis"] for job, _ in calls}) == 1
    assert len({job["fragments"] for job, _ in calls}) == 4
    for job, effective in calls:
        argv = shlex.split(effective["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])
        assert "--implementation-id" not in argv and "stale" not in argv
        assert argv[argv.index("--cache-dir") + 1] == job["analysis"]
        assert str(production) not in argv
        assert effective["PHONE_WORKER_TETO_FLAGS"] == ("Gb-8B0T0" if job["voice"] == "teto" else "B25T10")
        assert effective["PHONE_WORKER_TETO_RENDER_THREADS"] == ("2" if job["voice"] == "teto" else "4")
        assert effective["PHONE_WORKER_TETO_ALIAS_SUFFIX"] == ("" if job["voice"] == "teto" else "_D4")
        assert effective["PHONE_WORKER_TETO_BASE_PITCH"] == "C4" and effective["PHONE_WORKER_TETO_SPEECH_RATE"] == "1.0"
        assert not any(key.startswith("PHONE_WORKER_MIKU_") for key in effective)
        assert Path(job["output"]).is_file() and not Path(job["analysis"]).exists()
        assert not Path(job["fragments"]).exists()
    assert env.read_bytes() == original_env and dict(os.environ) == process_env
    assert all((bank / "source.wav").read_bytes() == b"original bank" for bank in banks.values())
    assert (production / "keep.esp").read_bytes() == b"production analysis"
    assert sorted(path.name for path in production.iterdir()) == ["keep.esp"]
    assert "never-print-this" not in json.dumps(report)
    persisted = json.loads(Path(report["report"]).read_text())
    assert persisted["ok"] and persisted["report"] == report["report"]


def test_legacy_bridge_parser_is_respected_even_with_explicit_runtime(prepared):
    module, helper, _, banks, runtime, env, production, _ = prepared
    values = module.read_environment(helper, env)
    command = module.temporary_command(helper, values, voice="miku", directory=banks["miku"],
        container="voicepeak-arm64", analysis=env.parent / "analysis", esper_root=runtime)
    argv = shlex.split(command)
    assert "--implementation-id" not in argv
    assert argv[argv.index("--root") + 1] == str(runtime)
    assert argv[argv.index("--cache-dir") + 1] == str(env.parent / "analysis")
    assert str(production) not in argv


def test_environment_values_are_literals_and_unknown_keys_are_never_executed(prepared):
    module, helper, _, _, _, env, _, _ = prepared
    marker = env.parent / "must-not-exist"
    env.write_text("SECRET=never-print-this\n" +
        "PHONE_WORKER_TETO_FLAGS=" + helper.shell_value("B0T0") + " # keep explicit zeros\n" +
        "PHONE_WORKER_MIKU_FLAGS=''\n" +
        "RUN_ME=$(touch " + str(marker) + ")\n", encoding="utf-8")
    values = module.read_environment(helper, env)
    assert values == {"PHONE_WORKER_TETO_FLAGS": "B0T0", "PHONE_WORKER_MIKU_FLAGS": ""}
    assert not marker.exists()


def test_current_bridge_keeps_identity_and_temporary_cache(prepared):
    module, helper, _, banks, runtime, env, _, _ = prepared
    bridge = runtime / "bin/resampler.py"
    bridge.write_text(bridge.read_text() + '\np.add_argument("--implementation-id")\n')
    values = module.read_environment(helper, env)
    argv = shlex.split(module.temporary_command(helper, values, voice="teto", directory=banks["teto"],
        container="voicepeak-arm64", analysis=env.parent / "analysis", esper_root=None))
    assert argv[argv.index("--implementation-id") + 1] == hashlib.sha256(bridge.read_bytes()).hexdigest()
    assert argv[argv.index("--cache-dir") + 1] == str(env.parent / "analysis")


def test_failures_preserve_other_comparison_results_and_clean_temporary_cache(prepared, monkeypatch):
    module, _, _, _, _, _, _, calls = prepared
    original = module.run_sample
    def failed(job):
        if job["voice"] == "miku" and job["revision"] == "atual":
            raise TimeoutError("prazo de síntese esgotado")
        return original(job)
    monkeypatch.setattr(module, "run_sample", failed)
    report = perform(prepared)
    assert not report["ok"] and report["rendered"] == 3 and len(report["samples"]) == 4
    assert "prazo de síntese esgotado" in report["samples"][-1]["error"]
    assert report["temporary_files_removed"]
    assert all(not Path(job["analysis"]).exists() for job, _ in calls)
    assert Path(report["report"]).is_file()


def test_absent_bank_is_actionable_and_does_not_hide_other_voice_results(prepared):
    report = perform(prepared, miku_voicebank=prepared[5].parent / "absent-bank")
    assert not report["ok"] and report["rendered"] == 2
    assert report["skipped"][0]["voice"] == "miku"
    assert "--miku-voicebank" in report["skipped"][0]["action"]
    assert all(sample["voice_id"] == "kasane-teto" for sample in report["samples"])


def test_extended_languages_and_new_run_directory_preserve_previous_audio(prepared):
    first = perform(prepared, english=True, extended=True)
    assert first["ok"] and first["rendered"] == 16
    assert {job["language"] for job, _ in prepared[-1]} == {"pt-BR", "en-US"}
    before_files = {path: path.read_bytes() for path in Path(first["directory"]).glob("*.mp3")}
    second = perform(prepared, voice="miku")
    assert second["ok"] and second["rendered"] == 2 and first["directory"] != second["directory"]
    assert all(path.read_bytes() == content for path, content in before_files.items())


@pytest.mark.parametrize("changes", [{"timeout": 0}, {"timeout": 121}, {"timeout": float("nan")},
    {"timeout": float("inf")}, {"timeout": True}, {"voice": "unknown"}, {"container": "bad\ncommand"}])
def test_invalid_limits_fail_before_rendering(prepared, changes):
    report = perform(prepared, **changes)
    assert not report["ok"] and "error" in report and not prepared[-1]


@pytest.mark.parametrize("protected", ["bank", "cache", "runtime", "configuration", "symlink"])
def test_output_cannot_write_to_bank_runtime_production_cache_or_config(prepared, protected):
    _, _, _, banks, runtime, env, production, calls = prepared
    outputs = {"bank": banks["miku"], "cache": production, "runtime": runtime, "configuration": env}
    if protected == "symlink":
        link = env.parent / "output-link"
        link.symlink_to(env.parent / "samples")
        outputs[protected] = link
    report = perform(prepared, output=outputs[protected], esper_root=runtime)
    assert not report["ok"] and "error" in report and not calls


def test_before_after_are_imported_in_fresh_subprocesses_with_exact_pcm(tmp_path):
    module = load_script()
    fake = '''import io, wave
class TetoRenderer:
    def __init__(self, environment, voice_id, resource_guard):
        self.voice_id = voice_id
    def status(self, force=False):
        return {"ready": True}
    def synthesize(self, text, **kwargs):
        target = io.BytesIO()
        with wave.open(target, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(44100)
            wav.writeframes(PCM * 200)
        return {"audio": target.getvalue(), "voice_id": self.voice_id,
                "reading_language": kwargs["reading_language"],
                "pitch_offset_semitones": kwargs["pitch_offset_semitones"], "speech_rate": kwargs["speech_rate"]}
'''
    runtime = tmp_path / "runtime"
    bridge = runtime / "bin/resampler.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("import argparse\np = argparse.ArgumentParser()\n" + "\n".join(
        "p.add_argument(" + repr(option) + ")" for option in ("--root", "--cache-dir", "--container", "--timeout")))
    hashes = []
    for revision, pcm in (("anterior", b"\x01\x00"), ("atual", b"\x02\x00"), ("anterior", b"\x01\x00")):
        worker = tmp_path / revision
        renderer = worker / "teto_renderer"
        renderer.mkdir(parents=True, exist_ok=True)
        (renderer / "renderer.py").write_text("PCM = " + repr(pcm) + "\n" + fake)
        (renderer / "__init__.py").write_text("from .renderer import TetoRenderer\n")
        job = {"id": revision, "revision": revision, "voice": "miku", "voicebank": str(tmp_path),
               "worker_dir": str(worker), "renderer_digest": module.renderer_digest(worker), "settings": {},
               "language": "pt-BR", "text": "Olá", "container": "voicepeak-arm64", "timeout": 20,
               "analysis": str(tmp_path / "analysis"), "fragments": str(tmp_path / revision / "fragments"),
               "esper_root": str(runtime), "output": str(tmp_path / (revision + ".wav"))}
        result = module.run_sample(job)
        assert result["render"]["wav_verified"] and result["render"]["sample_frames"] == 200
        hashes.append(result["render"]["sha256"])
    assert hashes[0] == hashes[2] and hashes[0] != hashes[1]


def test_help_describes_portuguese_comparison_without_exposing_internal_render(capsys):
    module = load_script()
    with pytest.raises(SystemExit) as result:
        module.main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "pronúncia anterior e atual" in output and "somente leitura" in output and "--_render" not in output
