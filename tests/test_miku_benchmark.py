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
SCRIPT = ROOT / "deploy/miku-utau/termux/benchmark-miku.py"


def load_script():
    spec = importlib.util.spec_from_file_location("miku_benchmark_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    module = load_script()
    validator = module.validator()
    helper = validator.installer()
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "source.wav").write_bytes(b"original voicebank")
    bridge = tmp_path / "esper/bin/resampler.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_bytes((ROOT / "deploy/esper-utau/termux/resampler.py").read_bytes())
    production = tmp_path / "production-cache"
    production.mkdir()
    (production / "keep.esp").write_bytes(b"existing production analysis")
    command = shlex.join([sys.executable, str(bridge), "--cache-dir=" + str(production)])
    env = tmp_path / "worker.env"
    env.write_text("TOKEN=do-not-print\n" +
                   "PHONE_WORKER_MIKU_RESAMPLER_COMMAND=" + helper.shell_value(command) + "\n" +
                   "PHONE_WORKER_ESPER_CACHE_DIR=" + helper.shell_value(str(production)) + "\n", encoding="utf-8")
    monkeypatch.setattr(helper, "check_existing", lambda directory: {"aliases": 2222})
    monkeypatch.setattr(validator, "installer", lambda: helper)
    monkeypatch.setattr(module, "validator", lambda: validator)
    calls = []

    def run(job):
        settings = job["settings"]
        cache = Path(settings["PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR"])
        hit = cache.exists()
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "fragment.wav").write_bytes(b"cached temporary fragment")
        analysis = Path(settings["PHONE_WORKER_ESPER_CACHE_DIR"])
        analysis.mkdir(parents=True, exist_ok=True)
        (analysis / "source.esp").write_bytes(b"temporary analysis")
        Path(job["output"]).write_bytes(b"same waveform")
        calls.append({"job": job, "cache_hit": hit})
        threads = int(settings["PHONE_WORKER_MIKU_RENDER_THREADS"])
        return {"ok": True, "render": {"sha256": hashlib.sha256(b"same waveform").hexdigest(),
                 "worker_synth_ms": 0.01 if hit else 0.1 / threads,
                 "duration_seconds": 2.0, "render_workers": threads,
                 "fragment_cache_hits": int(hit), "resampler_calls": 0 if hit else 3}}

    monkeypatch.setattr(validator, "run_sample", run)
    return module, validator, helper, bank, env, production, calls


def perform(prepared, **changes):
    module, _, _, bank, env, _, _ = prepared
    arguments = {"env": env, "directory": bank, "container": "voicepeak-arm64", "timeout": 120,
                 "rounds": 2, "language": "pt-BR", "report": env.parent / "report.json"}
    arguments.update(changes)
    return module.benchmark(**arguments)


def test_benchmark_isolates_caches_alternates_runs_and_confirms_every_wav(prepared):
    module, _, _, bank, env, production, calls = prepared
    before_env, before_source = env.read_bytes(), (bank / "source.wav").read_bytes()
    before_process_environment = dict(os.environ)
    report = perform(prepared)
    assert report["ok"] and report["wav_identical"]
    assert report["temporary_files_removed"] and report["temporary_cache_only"]
    assert report["phrase_cache_measured"] is False
    assert report["worker_configuration_changed"] is False
    assert report["production_caches_changed"] is False
    assert len(calls) == 9 and len(report["samples"]) == 8
    assert [row["threads_requested"] for row in report["samples"]] == [2, 2, 4, 4, 4, 4, 2, 2]
    assert [row["phase"] for row in report["samples"]] == ["fragmentos_novos", "fragmentos_prontos"] * 4
    assert [call["cache_hit"] for call in calls] == [False] + [False, True] * 4
    assert len({call["job"]["settings"]["PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR"] for call in calls}) == 5
    assert len({call["job"]["settings"]["PHONE_WORKER_ESPER_CACHE_DIR"] for call in calls}) == 1
    for call in calls:
        job = call["job"]
        analysis = job["settings"]["PHONE_WORKER_ESPER_CACHE_DIR"]
        assert analysis != str(production)
        command = shlex.split(job["settings"]["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"])
        assert command[command.index("--cache-dir") + 1] == analysis
        assert "--cache-dir=" + str(production) not in command
        assert not Path(job["output"]).exists()
        assert not Path(analysis).exists()
    assert report["summary"][0]["worker_synth_ms_mean"] == 0.05
    assert report["summary"][0]["worker_synth_ms_median"] == 0.05
    assert report["summary"][2]["worker_synth_ms_mean"] == 0.03
    assert report["summary"][0]["successful_runs"] == 2
    assert env.read_bytes() == before_env
    assert (bank / "source.wav").read_bytes() == before_source
    assert sorted(path.name for path in production.iterdir()) == ["keep.esp"]
    assert (production / "keep.esp").read_bytes() == b"existing production analysis"
    assert dict(os.environ) == before_process_environment
    persisted = json.loads(Path(report["report"]).read_text())
    assert persisted["ok"] and len(persisted["samples"]) == 8
    assert "do-not-print" not in json.dumps(report)


@pytest.mark.parametrize("old_bridge", [False, True])
def test_effective_old_and_current_bridge_cache_stays_temporary(prepared, old_bridge):
    module, validator, helper, bank, env, production, _ = prepared
    _, values = helper.read_environment(env)
    command = shlex.split(values["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"])
    if old_bridge:
        wrapper = Path(command[1])
        wrapper.write_bytes(b"".join(line for line in wrapper.read_bytes().splitlines(keepends=True)
                                    if b'add_argument("--implementation-id"' not in line))
        assert "--implementation-id" not in helper.bridge_options(wrapper.read_bytes())
    temporary = env.parent / "benchmark-cache"
    settings = module.isolated_settings(helper, values, directory=bank, container="voicepeak-arm64",
                                        analysis=temporary, fragments=env.parent / "temporary-fragments", threads=4)
    effective = validator.controls(settings, directory=bank, container="voicepeak-arm64", timeout=120)
    argv = shlex.split(effective["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])
    actual = Path(argv[argv.index("--cache-dir") + 1])
    assert actual.is_relative_to(temporary)
    assert str(production) not in argv
    assert effective["PHONE_WORKER_TETO_RENDER_THREADS"] == "4"
    assert effective["PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR"] == str(env.parent / "temporary-fragments")


def test_english_defaults_are_english_and_text_whitespace_is_normalized(prepared):
    report = perform(prepared, language="en-US", rounds=1)
    assert report["ok"] and report["text"] == "Hello. I am Miku. How are you?"
    assert len(report["samples"]) == 4
    report = perform(prepared, text="  Bom\n dia.  ", rounds=1)
    assert report["text"] == "Bom dia."


@pytest.mark.parametrize("changes", [{"timeout": 0}, {"timeout": 121}, {"timeout": float("nan")},
    {"timeout": float("inf")}, {"timeout": True}, {"rounds": 0}, {"rounds": 4}, {"rounds": True},
    {"language": "ja-JP"}, {"container": "container\ncommand"}, {"text": ""},
    {"text": "a" * 181}, {"text": "bad\x00text"}])
def test_invalid_limits_fail_before_rendering(prepared, changes):
    report = perform(prepared, **changes)
    assert not report["ok"] and "error" in report
    assert prepared[-1] == []


@pytest.mark.parametrize("target", ["bank", "env", "cache", "symlink"])
def test_report_cannot_replace_configuration_bank_cache_or_symlink(prepared, target):
    _, _, _, bank, env, production, calls = prepared
    paths = {"bank": bank / "source.wav", "env": env, "cache": production / "keep.esp"}
    if target == "symlink":
        link = env.parent / "unsafe-report.json"
        link.symlink_to(env)
        paths["symlink"] = link
    before_env = env.read_bytes()
    report = perform(prepared, report=paths[target])
    assert not report["ok"] and "error" in report
    assert not calls and env.read_bytes() == before_env
    assert (bank / "source.wav").read_bytes() == b"original voicebank"
    assert (production / "keep.esp").read_bytes() == b"existing production analysis"


def test_wav_mismatch_is_reported_and_temporary_files_are_removed(prepared, monkeypatch):
    _, validator, _, _, _, _, calls = prepared
    original = validator.run_sample
    def changed(job):
        result = original(job)
        if job["settings"]["PHONE_WORKER_MIKU_RENDER_THREADS"] == "4":
            result["render"]["sha256"] = "f" * 64
        return result
    monkeypatch.setattr(validator, "run_sample", changed)
    report = perform(prepared, rounds=1)
    assert not report["ok"] and not report["wav_identical"]
    assert report["temporary_files_removed"]
    assert [row["wav_matches_reference"] for row in report["samples"]] == [True, True, False, False]
    assert all(not Path(call["job"]["output"]).exists() for call in calls)


def test_failed_warmup_does_not_produce_comparison_results(prepared, monkeypatch):
    _, validator, _, _, _, _, _ = prepared
    def failed(job):
        raise TimeoutError("prazo ESPER esgotado")
    monkeypatch.setattr(validator, "run_sample", failed)
    report = perform(prepared)
    assert not report["ok"] and "aquecimento falhou" in report["error"]
    assert not report["samples"] and report["warmup"]["ok"] is False
    assert report["temporary_files_removed"]


def test_help_is_in_portuguese(capsys):
    module = load_script()
    with pytest.raises(SystemExit) as result:
        module.main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "renderizações paralelas" in output and "nenhum WAV será mantido" in output
