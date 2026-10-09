from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import struct
import types
import wave

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/worldline-r/termux/compare-quality.py"


@pytest.fixture
def comparison():
    module = types.ModuleType("worldline_quality_comparison_test")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), "exec"), module.__dict__)
    return module


def pcm(*, seconds=0.01, rate=44100, channels=1, width=2, silence=False) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as writer:
        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        frames = round(rate * seconds)
        sample = b"\0" * width if silence else struct.pack("<h", 1200)[:width]
        writer.writeframes(sample * frames * channels)
    return target.getvalue()


def fake_factory(comparison, calls, *, label, ready=True, audio=None, native=True,
                 result_profile=None, backend="worldline-r"):
    class Renderer:
        def __init__(self, *, resource_guard):
            assert resource_guard()["ok"] is True
            calls.append((label, "init", dict(os.environ)))

        def status(self, *, force=False):
            assert force is True
            mode = os.environ["PHONE_WORKER_TETO_VOICEBANK_MODE"]
            profile = "english-cvvc" if mode == "english" else "standard"
            return {"ready": ready, "backend": backend, "voicebank_profile": profile,
                    "last_error": "missing runtime" if not ready else "",
                    "aliases": 550 if mode == "english" else 345,
                    "voicebank_fingerprint": "bank-fingerprint-" + profile}

        def synthesize(self, text, **kwargs):
            calls.append((label, "render", text, kwargs, dict(os.environ)))
            mode = os.environ["PHONE_WORKER_TETO_VOICEBANK_MODE"]
            profile = "english-cvvc" if mode == "english" else "standard"
            return {"audio": pcm() if audio is None else audio, "backend": backend,
                    "native_phrase_render_verified": native,
                    "voicebank_profile": result_profile or profile,
                    "renderer_fingerprint": "adapter-" + label,
                    "voicebank_fingerprint": "bank-fingerprint-" + profile}

    return Renderer


def test_cli_generates_ab_with_fixed_defaults_and_preserves_environment_files(tmp_path, comparison, monkeypatch, capsys):
    monkeypatch.setattr(comparison.Path, "home", lambda: tmp_path)
    envfile = tmp_path / ".phone-worker.env"
    original = b"PHONE_WORKER_TETO_BACKEND=utau\nPHONE_WORKER_TETO_SPEECH_RATE=1.2\n"
    envfile.write_bytes(original)
    calls = []
    monkeypatch.setattr(comparison, "LegacyEnvelopeRenderer", fake_factory(comparison, calls, label="legacy"))
    monkeypatch.setattr(comparison, "WorldlineRenderer", fake_factory(comparison, calls, label="balanced"))
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "utau")
    monkeypatch.setenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.5")
    monkeypatch.setenv("PHONE_WORKER_TETO_BASE_PITCH", "G3")
    monkeypatch.setenv("PHONE_WORKER_TETO_VOICEBANK_MODE", "auto")
    before = dict(os.environ)
    directory = tmp_path / "results"
    result = comparison.main(["--output-dir", str(directory), "--text", "  Eu   sou Teto. "])
    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rendered"] == 2 and summary["failed"] == 0
    assert summary["teto_synthesis_verified"] is True
    assert not summary["portuguese_quality_verified"]
    assert not summary["worker_configuration_changed"] and not summary["production_backend_changed"]
    assert envfile.read_bytes() == original and dict(os.environ) == before
    renders = [call for call in calls if call[1] == "render"]
    assert [call[0] for call in renders] == ["legacy", "balanced"]
    for call in renders:
        _, _, text, kwargs, settings = call
        assert text == "Eu sou Teto."
        assert 0 < kwargs["timeout_seconds"] <= 90
        assert kwargs["pitch_offset_semitones"] == 0.0
        assert settings["PHONE_WORKER_TETO_SPEECH_RATE"] == "1.0"
        assert settings["PHONE_WORKER_TETO_BASE_PITCH"] == "C4"
        assert settings["PHONE_WORKER_TETO_VOICEBANK_MODE"] == "standard"
        assert settings["PHONE_WORKER_TETO_MAX_AUDIO_SECONDS"] == "20"
        assert settings["PHONE_WORKER_TETO_BACKEND"] == "worldline-r"
    for stem in ("01-anterior", "02-juncoes-corrigidas"):
        record = json.loads((directory / f"{stem}.json").read_text())
        wav = (directory / f"{stem}.wav").read_bytes()
        assert record["ok"] and record["render"]["sha256"] == hashlib.sha256(wav).hexdigest()
        assert record["render"]["sample_rate"] == 44100
    assert json.loads((directory / "summary.json").read_text()) == summary
    assert json.loads((directory / "03-teto-english.json").read_text())["skipped"]


def test_failed_legacy_is_independent_and_removes_its_stale_wav(tmp_path, comparison, monkeypatch, capsys):
    monkeypatch.setattr(comparison.Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(comparison, "LegacyEnvelopeRenderer", fake_factory(comparison, calls, label="legacy", ready=False))
    monkeypatch.setattr(comparison, "WorldlineRenderer", fake_factory(comparison, calls, label="balanced"))
    for stem in ("01-anterior", "02-juncoes-corrigidas", "03-teto-english"):
        (tmp_path / f"{stem}.wav").write_bytes(b"stale")
    assert comparison.main(["--output-dir", str(tmp_path)]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["rendered"] == 1 and summary["failed"] == 1
    assert not (tmp_path / "01-anterior.wav").exists()
    assert not (tmp_path / "03-teto-english.wav").exists()
    assert (tmp_path / "02-juncoes-corrigidas.wav").read_bytes() != b"stale"
    assert "missing runtime" in summary["samples"][0]["error"]
    assert [call[0] for call in calls if call[1] == "render"] == ["balanced"]


@pytest.mark.parametrize("kwargs", [
    {"audio": b"not WAV"}, {"audio": pcm(silence=True)}, {"audio": pcm(rate=22050)},
    {"audio": pcm(channels=2)}, {"audio": pcm(seconds=20.01)}, {"native": False},
    {"backend": "other-character"}, {"result_profile": "english-cvvc"},
])
def test_sample_requires_actual_bounded_native_pcm_and_requested_profile(tmp_path, comparison, kwargs):
    calls = []
    renderer = fake_factory(comparison, calls, label="bad", **kwargs)(resource_guard=lambda: {"ok": True})
    destination = tmp_path / "sample.wav"
    with comparison.temporary_settings({"PHONE_WORKER_TETO_VOICEBANK_MODE": "standard"}):
        with pytest.raises(ValueError):
            comparison._render_sample(renderer, "Teto", destination, 10, "standard")
    assert not destination.exists()


def test_invalid_explicit_english_does_not_fall_back_to_standard(tmp_path, comparison, monkeypatch, capsys):
    monkeypatch.setattr(comparison.Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(comparison, "LegacyEnvelopeRenderer", fake_factory(comparison, calls, label="legacy"))
    good = fake_factory(comparison, calls, label="balanced")

    class FailEnglish(good):
        def status(self, **kwargs):
            result = super().status(**kwargs)
            if os.environ["PHONE_WORKER_TETO_VOICEBANK_MODE"] == "english":
                result.update(ready=False, last_error="voicebank English sem oto.ini")
            return result

    monkeypatch.setattr(comparison, "WorldlineRenderer", FailEnglish)
    assert comparison.main(["--english-voicebank", str(tmp_path / "missing-English"),
                            "--output-dir", str(tmp_path / "results")]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["rendered"] == 2 and summary["failed"] == 1
    assert summary["samples"][2]["voicebank_mode"] == "english"
    assert "sem oto.ini" in summary["samples"][2]["error"]
    assert len([call for call in calls if call[1] == "render"]) == 2


def test_automatic_english_requires_valid_500_alias_bank(tmp_path, comparison, monkeypatch, capsys):
    monkeypatch.setattr(comparison.Path, "home", lambda: tmp_path)
    candidate = tmp_path / "voicebanks/kasane-teto-english"
    candidate.mkdir(parents=True)
    index_calls = []

    def load(path, *, minimum_aliases):
        index_calls.append((path, minimum_aliases))
        return types.SimpleNamespace(alias_count=550, fingerprint="english-index")

    monkeypatch.setattr(comparison.VoicebankIndex, "load", load)
    calls = []
    monkeypatch.setattr(comparison, "LegacyEnvelopeRenderer", fake_factory(comparison, calls, label="legacy"))
    monkeypatch.setattr(comparison, "WorldlineRenderer", fake_factory(comparison, calls, label="balanced"))
    assert comparison.main(["--output-dir", str(tmp_path / "results")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert index_calls == [(candidate, 500)]
    assert summary["rendered"] == 3 and summary["english_candidate"]["available"]
    english = summary["samples"][2]
    assert english["render"]["voicebank_profile"] == "english-cvvc"
    assert english["render"]["voicebank_fingerprint"].endswith("english-cvvc")
    assert Path(english["render"]["output"]).name == "03-teto-english.wav"


def test_legacy_envelope_override_does_not_modify_requests(comparison):
    requests = [{"position_ms": 50, "length_ms": 100, "fade_in_ms": 30, "fade_out_ms": 10}]
    before = [dict(request) for request in requests]
    renderer = comparison.LegacyEnvelopeRenderer()
    assert renderer._balance_crossfades(requests) == 0
    assert requests == before
    assert renderer.ENVELOPE_MODE == "legacy-native-envelopes"


def test_short_sample_budget_caps_the_native_status_probe(comparison):
    deadlines = []

    class Renderer:
        @staticmethod
        def _run(command, *, deadline):
            deadlines.append(deadline)
            return "proof", ""

    renderer = Renderer()
    comparison._cap_native_deadlines(renderer, 101.0)
    assert renderer._run(["status"], deadline=108.0) == ("proof", "")
    assert renderer._run(["render"], deadline=100.5) == ("proof", "")
    assert deadlines == [101.0, 100.5]


def test_wav_destination_symlink_is_preserved_and_not_written(tmp_path, comparison, monkeypatch, capsys):
    monkeypatch.setattr(comparison.Path, "home", lambda: tmp_path)
    target = tmp_path / "private"
    target.write_bytes(b"do not overwrite")
    (tmp_path / "01-anterior.wav").symlink_to(target)
    calls = []
    monkeypatch.setattr(comparison, "LegacyEnvelopeRenderer", fake_factory(comparison, calls, label="legacy"))
    monkeypatch.setattr(comparison, "WorldlineRenderer", fake_factory(comparison, calls, label="balanced"))
    assert comparison.main(["--output-dir", str(tmp_path)]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert target.read_bytes() == b"do not overwrite"
    assert "link simbólico" in summary["samples"][0]["error"]
    assert not any(call[0] == "legacy" for call in calls)


@pytest.mark.parametrize("arguments", [
    ["--timeout", "nan"], ["--timeout", "121"], ["--timeout", "0"],
    ["--container", "wrong;command"], ["--text", " "], ["--text", "t" * 181],
])
def test_invalid_cli_values_are_rejected_before_any_render(comparison, arguments):
    with pytest.raises(SystemExit) as error:
        comparison.main(arguments)
    assert error.value.code == 2
