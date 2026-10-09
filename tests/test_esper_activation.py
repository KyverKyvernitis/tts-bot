from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import struct
import types
import wave

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/esper-utau/termux/activate-esper.py"


@pytest.fixture
def helper():
    module = types.ModuleType("esper_activation_test")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), "exec"), module.__dict__)
    return module


@pytest.fixture
def prepared(tmp_path, helper, monkeypatch):
    bundle = tmp_path / "kit"
    bundle.mkdir()
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    source = b"#!/usr/bin/env python3\n# known wrapper\n"
    (bundle / "resampler.py").write_bytes(source)
    (root / "bin" / "resampler.py").write_bytes(source)
    bank = tmp_path / "Teto English"
    bank.mkdir()
    env = tmp_path / ".phone-worker.env"
    env.write_bytes(b"# existing configuration\r\nTOKEN='literal $secret `thing`'\r\n"
                    b"PHONE_WORKER_TETO_BACKEND=worldline-r\r\nexport PHONE_WORKER_TETO_BACKEND=voicepeak\r\n"
                    b"OTHER=$(touch /should-not-execute)\r\nPHONE_WORKER_ESPER_DEADLINE=1234\r\n")
    monkeypatch.setattr(helper, "TOOLKIT_ROOT", bundle)
    checks = []

    def verify(values):
        checks.append(values)
        return {"native_probe": {"ok": True, "synthetic_render_verified": True},
                "phrase_probe": {"ok": True, "teto_synthesis_verified": True}}

    monkeypatch.setattr(helper, "verify_runtime", verify)
    return types.SimpleNamespace(root=root, bank=bank, env=env, bundle=bundle, checks=checks, original=env.read_bytes())


def activate(helper, prepared, **kwargs):
    return helper.activate(env=prepared.env, root=prepared.root, container="voicepeak-arm64",
                           voicebank=prepared.bank, **kwargs)


def decoded_env(data):
    result = {}
    for line in data.decode().splitlines():
        if line.startswith("PHONE_WORKER_") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = json.loads(value)
    return result


def test_activation_checks_then_backs_up_preserves_credentials_and_selects_esper(helper, prepared):
    result = activate(helper, prepared)
    assert result["ok"] and result["worker_configuration_changed"] and result["restart_required"]
    assert result["teto_synthesis_verified"] and not result["portuguese_quality_verified"]
    text = prepared.env.read_bytes()
    assert b"TOKEN='literal $secret `thing`'\r\n" in text
    assert b"OTHER=$(touch /should-not-execute)\r\n" in text
    assert text.count(b"PHONE_WORKER_TETO_BACKEND=") == 1
    assert b"PHONE_WORKER_ESPER_DEADLINE=" not in text
    assert Path(result["publication"]["backup"]).read_bytes() == prepared.original
    assert prepared.env.stat().st_mode & 0o777 == 0o600
    values = decoded_env(text)
    assert values["PHONE_WORKER_TETO_BACKEND"] == "utau"
    assert values["PHONE_WORKER_TETO_FLAGS"] == "B0"
    assert values["PHONE_WORKER_TETO_VOICEBANK_MODE"] == "english"
    assert values["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] == str(prepared.bank)
    assert values["PHONE_WORKER_TETO_LENGTH_MODE"] == "total"
    assert values["PHONE_WORKER_TETO_BASE_PITCH"] == "C4"
    assert values["PHONE_WORKER_TETO_SPEECH_RATE"] == "1.0"
    assert values["PHONE_WORKER_TETO_MODULATION"] == "15"
    assert values["PHONE_WORKER_TTS_AGENT_TIMEOUT_SECONDS"] == "120"
    assert values["PHONE_WORKER_JOB_TIMEOUT_SECONDS"] == "120"
    assert values["PHONE_WORKER_ESPER_TIMEOUT"] == "60"
    digest = hashlib.sha256((prepared.root / "bin/resampler.py").read_bytes()).hexdigest()
    assert values["PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR"].endswith("/fragments/" + digest)
    command = shlex.split(values["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])
    assert command[1:] == [str(prepared.root / "bin/resampler.py"), "--root", str(prepared.root),
                           "--container", "voicepeak-arm64", "--timeout", "60",
                           "--implementation-id", digest]
    assert len(prepared.checks) == 1
    assert "secret" not in json.dumps(result)


def test_android_without_os_link_and_idempotent_rerun(helper, prepared, monkeypatch):
    monkeypatch.delattr(helper.os, "link", raising=False)
    first = activate(helper, prepared)
    before = prepared.env.stat()
    second = activate(helper, prepared)
    assert first["ok"] and second["ok"] and not second["worker_configuration_changed"]
    assert "backup" not in second["publication"]
    assert prepared.env.stat().st_ino == before.st_ino
    assert len(list(prepared.env.parent.glob(".phone-worker.env.bak-esper-*"))) == 1
    assert len(prepared.checks) == 2


def test_failed_native_or_phrase_validation_never_changes_env(helper, prepared, monkeypatch):
    def failure(values):
        raise helper.ActivationError("phrase failed")
    monkeypatch.setattr(helper, "verify_runtime", failure)
    result = activate(helper, prepared)
    assert not result["ok"] and not result["worker_configuration_changed"]
    assert prepared.env.read_bytes() == prepared.original
    assert not list(prepared.env.parent.glob(".phone-worker.env.bak-esper-*"))


def test_unknown_installed_wrapper_preserved_and_not_executed(helper, prepared):
    path = prepared.root / "bin/resampler.py"
    path.write_bytes(b"# user modification\n")
    result = activate(helper, prepared)
    assert not result["ok"] and "difere do kit" in result["error"]
    assert path.read_bytes() == b"# user modification\n"
    assert prepared.env.read_bytes() == prepared.original and not prepared.checks


@pytest.mark.parametrize("destination", ["env", "wrapper", "parent"])
def test_symlink_destinations_preserved(helper, prepared, tmp_path, destination):
    if destination == "env":
        real = tmp_path / "actual-env"
        prepared.env.rename(real)
        prepared.env.symlink_to(real)
    elif destination == "wrapper":
        path = prepared.root / "bin/resampler.py"
        real = tmp_path / "actual-wrapper"
        path.rename(real)
        path.symlink_to(real)
    else:
        real = tmp_path / "real-bin"
        (prepared.root / "bin").rename(real)
        (prepared.root / "bin").symlink_to(real, target_is_directory=True)
    result = activate(helper, prepared)
    assert not result["ok"] and not prepared.checks
    assert prepared.env.read_bytes() == prepared.original


def test_concurrent_env_edit_during_probe_is_not_overwritten(helper, prepared, monkeypatch):
    changed = prepared.original + b"NEW_LOCAL_SETTING=keep\n"
    def verify(values):
        prepared.env.write_bytes(changed)
        return {"native_probe": {"ok": True}, "phrase_probe": {"ok": True}}
    monkeypatch.setattr(helper, "verify_runtime", verify)
    result = activate(helper, prepared)
    assert not result["ok"] and "mudou durante" in result["error"]
    assert prepared.env.read_bytes() == changed
    assert not list(prepared.env.parent.glob(".phone-worker.env.bak-esper-*"))


def test_wrapper_change_during_probe_does_not_activate_unknown_script(helper, prepared, monkeypatch):
    def verify(values):
        (prepared.root / "bin/resampler.py").write_bytes(b"# changed\n")
        return {}
    monkeypatch.setattr(helper, "verify_runtime", verify)
    assert not activate(helper, prepared)["ok"]
    assert prepared.env.read_bytes() == prepared.original


def test_failed_atomic_replace_leaves_original_and_recovery_backup(helper, prepared, monkeypatch):
    def failure(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(helper.os, "replace", failure)
    result = activate(helper, prepared)
    assert not result["ok"] and not result["worker_configuration_changed"]
    assert prepared.env.read_bytes() == prepared.original
    backups = list(prepared.env.parent.glob(".phone-worker.env.bak-esper-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == prepared.original
    assert not list(prepared.env.parent.glob("..phone-worker.env.esper-*"))


def test_shell_quoting_roundtrips_spaces_quotes_and_prevents_substitution(helper, tmp_path):
    # Use the exact decoder shared with the Python facade, plus a shell parse.
    code = (SCRIPT.parents[2] / "termux/phone-worker/phone_worker.py").read_text()
    start = code.index("def _decode_env_value(")
    stop = code.index("\ndef _load_env_file(", start)
    namespace = {"json": json, "contextlib": __import__("contextlib")}
    exec(code[start:stop], namespace)
    value = str(tmp_path / "spaces $variable `subshell` 'single' \"double\" \\backslash")
    quoted = helper.shell_value(value)
    assert namespace["_decode_env_value"](quoted) == value
    output = helper.bounded(["bash", "-c", "SETTING=" + quoted + '; printf "%s" "$SETTING"'], 5)
    assert output == value


@pytest.mark.parametrize("flags", ["B-20", "B101", "B25g3", "$(touch bad)", ""])
def test_invalid_flags_fail_before_checks_or_publication(helper, prepared, flags):
    result = activate(helper, prepared, flags=flags)
    assert not result["ok"] and not prepared.checks
    assert prepared.env.read_bytes() == prepared.original


def test_cli_failure_reports_no_credentials(helper, prepared, capsys):
    prepared.env.write_bytes(b"TOKEN=super-secret\n\x00")
    assert helper.main(["--env", str(prepared.env), "--root", str(prepared.root), "--voicebank", str(prepared.bank)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False
    assert "super-secret" not in output


def test_negative_selected_level_is_explicitly_marked_experimental(helper, prepared):
    result = activate(helper, prepared, flags="B-50")
    assert result["ok"] and result["flags_experimental"]
    assert decoded_env(prepared.env.read_bytes())["PHONE_WORKER_TETO_FLAGS"] == "B-50"


def pcm(*, silence=False, rate=44100):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack("<h", 0 if silence else 1234) * 4410)
    return stream.getvalue()


def test_real_phrase_child_uses_selected_settings_checks_pcm_and_restores_environment(helper, monkeypatch):
    class Renderer:
        def __init__(self, resource_guard):
            assert resource_guard()["ok"]
        def status(self, force):
            assert force and os.environ["PHONE_WORKER_TETO_FLAGS"] == "B25"
            return {"ready": True, "voicebank_profile": "english-cvvc"}
        def synthesize(self, text, **kwargs):
            assert text == helper.PHRASE and kwargs["timeout_seconds"] == 120
            assert kwargs["pitch_offset_semitones"] == 0
            assert float(os.environ["PHONE_WORKER_ESPER_DEADLINE"]) > __import__("time").monotonic()
            return {"audio": pcm(), "voicebank_profile": "english-cvvc", "rendered_phonemes": 8}
    monkeypatch.setitem(__import__("sys").modules, "teto_renderer", types.SimpleNamespace(TetoRenderer=Renderer))
    before = dict(os.environ)
    result = helper.phrase_child({"PHONE_WORKER_TETO_FLAGS": "B25"})
    assert result["teto_synthesis_verified"] and result["evidence"]["wav_verified"]
    assert dict(os.environ) == before


@pytest.mark.parametrize("audio", [b"fake wave", pcm(silence=True), pcm(rate=22050)])
def test_silent_invalid_and_wrong_rate_audio_is_rejected(helper, audio):
    with pytest.raises(helper.ActivationError):
        helper.wav_evidence(audio)


def test_runtime_checks_use_verified_wrapper_command_and_ephemeral_child(helper, tmp_path, monkeypatch):
    values = helper.configuration(root=tmp_path, container="voicepeak-arm64", voicebank=tmp_path,
                                  flags="B0", wrapper_hash="a" * 64)
    monkeypatch.setattr(helper.shutil, "which", lambda item: "/bin/" + item)
    monkeypatch.setenv("PHONE_WORKER_ESPER_DEADLINE", "stale")
    calls = []
    native = {"ok": True, "synthetic_render_verified": True, "wav_verified": True,
              "native_architecture": "arm64", "engine_sha256": helper.ENGINE_SHA256,
              "config_sha256": helper.CONFIG_SHA256, "implementation_id": "a" * 64}
    def bounded(command, timeout, *, environment=None):
        calls.append((command, timeout))
        if command[:2] == ["ffmpeg", "-version"]:
            return "ffmpeg version test"
        if "--print-architecture" in command:
            return "arm64\n"
        assert "PHONE_WORKER_ESPER_DEADLINE" not in environment
        if "--probe" in command:
            assert command[-1] == "--probe" and timeout == 65
            assert command[:-1] == shlex.split(values["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])
            assert command[command.index("--implementation-id") + 1] == "a" * 64
            return json.dumps(native)
        assert "--_validate" in command and timeout == 125
        assert json.loads(Path(command[-1]).read_text()) == values
        return json.dumps({"ok": True, "teto_synthesis_verified": True})
    monkeypatch.setattr(helper, "bounded", bounded)
    report = helper.verify_runtime(values)
    assert report["native_probe"] == native and report["phrase_probe"]["teto_synthesis_verified"]
    assert len(calls) == 4


def test_runtime_probe_rejects_unpinned_asset_report(helper, tmp_path, monkeypatch):
    values = helper.configuration(root=tmp_path, container="voicepeak-arm64", voicebank=tmp_path,
                                  flags="B0", wrapper_hash="a" * 64)
    monkeypatch.setattr(helper.shutil, "which", lambda item: "/bin/" + item)
    def bounded(command, timeout, *, environment=None):
        return "arm64\n" if "--print-architecture" in command else json.dumps({"ok": True, "synthetic_render_verified": True})
    monkeypatch.setattr(helper, "bounded", bounded)
    with pytest.raises(helper.ActivationError, match="nativa ESPER"):
        helper.verify_runtime(values)


def test_runtime_probe_rejects_report_from_different_wrapper(helper, tmp_path, monkeypatch):
    values = helper.configuration(root=tmp_path, container="voicepeak-arm64", voicebank=tmp_path,
                                  flags="B0", wrapper_hash="a" * 64)
    monkeypatch.setattr(helper.shutil, "which", lambda item: "/bin/" + item)
    def bounded(command, timeout, *, environment=None):
        if "--print-architecture" in command:
            return "arm64\n"
        return json.dumps({"ok": True, "synthetic_render_verified": True, "wav_verified": True,
                           "native_architecture": "arm64", "engine_sha256": helper.ENGINE_SHA256,
                           "config_sha256": helper.CONFIG_SHA256, "implementation_id": "b" * 64})
    monkeypatch.setattr(helper, "bounded", bounded)
    with pytest.raises(helper.ActivationError, match="nativa ESPER"):
        helper.verify_runtime(values)


def test_wrapper_update_changes_real_phrase_fingerprint_with_same_python_and_flags(helper, prepared, monkeypatch):
    # The real renderer stamps Python, which remains unchanged during wrapper
    # updates. The implementation ID in its command must invalidate old audio.
    monkeypatch.syspath_prepend(str(helper.WORKER_DIR))
    from teto_renderer import TetoRenderer
    renderer = TetoRenderer.__new__(TetoRenderer)
    index = types.SimpleNamespace(fingerprint="same-voicebank")
    first = activate(helper, prepared)
    assert first["ok"]
    before = decoded_env(prepared.env.read_bytes())
    monkeypatch.setenv("PHONE_WORKER_TETO_RESAMPLER_COMMAND", before["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])
    fingerprint_before = renderer._render_fingerprint(index)
    source = (prepared.bundle / "resampler.py").read_bytes() + b"# changed implementation\n"
    (prepared.bundle / "resampler.py").write_bytes(source)
    (prepared.root / "bin/resampler.py").write_bytes(source)
    second = activate(helper, prepared)
    assert second["ok"] and second["worker_configuration_changed"]
    after = decoded_env(prepared.env.read_bytes())
    monkeypatch.setenv("PHONE_WORKER_TETO_RESAMPLER_COMMAND", after["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])
    assert renderer._render_fingerprint(index) != fingerprint_before
    assert shlex.split(before["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])[0] == shlex.split(after["PHONE_WORKER_TETO_RESAMPLER_COMMAND"])[0]
    assert before["PHONE_WORKER_TETO_FLAGS"] == after["PHONE_WORKER_TETO_FLAGS"] == "B0"


@pytest.mark.parametrize("digest", ["known-hash", "a" * 63, "g" * 64, "A" * 64])
def test_configuration_rejects_invalid_implementation_digest(helper, tmp_path, digest):
    with pytest.raises(helper.ActivationError, match="SHA-256"):
        helper.configuration(root=tmp_path, container="voicepeak-arm64", voicebank=tmp_path,
                             flags="B0", wrapper_hash=digest)

