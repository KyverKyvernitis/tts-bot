from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import types
import wave
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/miku-utau/termux/install-voicebank.py"


def load_script(path=SCRIPT):
    spec = importlib.util.spec_from_file_location("miku_install_test_" + path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wave_bytes():
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\x01\x02" * 441)
    return data.getvalue()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    module = load_script()
    source = tmp_path / "mukki english cvvc.zip"
    contents = {
        "character.txt": b"name=MIKU_V4_English\nauthor=fixture\n",
        "prefix.map": b"C4\t\t_D4\nD4\t\t_D4\n",
        "oto.ini": b"",
        "D4/oto.ini": b"a.wav=a_D4,0,1,-10,1,0\n",
        "D4/a.wav": wave_bytes(),
    }
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in contents.items():
            archive.writestr(name, value)
    inventory = [[name, len(value), hashlib.sha256(value).hexdigest()] for name, value in sorted(contents.items())]
    inventory_hash = hashlib.sha256(json.dumps(inventory, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    for name, value in {
        "SOURCE_SHA256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "SOURCE_BYTES": source.stat().st_size,
        "EXPECTED_UNPACKED_BYTES": sum(map(len, contents.values())),
        "EXPECTED_FILES": len(contents), "INVENTORY_SHA256": inventory_hash,
        "MINIMUM_ALIASES": 1,
    }.items():
        monkeypatch.setattr(module, name, value)
    wrapper = tmp_path / "esper/bin/resampler.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# fixture installed ESPER bridge; never executed by installation\n")
    environment = tmp_path / ".phone-worker.env"
    command = shlex.join([sys.executable, str(wrapper), "--container", "voicepeak-arm64"])
    original = ("# user configuration\nTOKEN=fixture-private-value\n"
                "PHONE_WORKER_TETO_FLAGS=Gb-8B0T0\n"
                "PHONE_WORKER_TETO_SPEECH_RATE=1.25\n"
                f"PHONE_WORKER_TETO_RESAMPLER_COMMAND={module.shell_value(command)}\n"
                "UNRELATED='literal $(touch should-not-execute)'\n").encode()
    environment.write_bytes(original)
    return module, source, tmp_path / "voicebanks/hatsune-miku-english", environment, original


def install(prepared):
    module, source, destination, environment, _ = prepared
    return module.install_voicebank(archive=source, directory=destination, env=environment)


def test_pinned_release_identifies_downloaded_english_bank():
    module = load_script()
    assert module.SOURCE_SHA256 == "e1fc9a37a7e45e0ad4209b2b0f37d90541b2528c85d1b58a81c0b399ecaf79be"
    assert module.SOURCE_BYTES == 71819802
    assert module.EXPECTED_FILES == 3384
    assert module.ALIAS_SUFFIX == "_D4"


def test_install_roundtrip_preserves_teto_and_literals_without_os_link(prepared, monkeypatch):
    module, source, destination, environment, original = prepared
    monkeypatch.delattr(os, "link", raising=False)
    report = install(prepared)
    assert report["ok"] and report["installed"] and report["voicebank_ready"]
    assert report["production_voice_changed"] is False
    assert report["miku_synthesis_verified"] is False
    assert Path(report["configuration_backup"]).read_bytes() == original
    assert environment.read_bytes().startswith(original)
    _, values = module.read_environment(environment)
    assert values["PHONE_WORKER_MIKU_ENABLED"] == "true"
    assert values["PHONE_WORKER_MIKU_VOICEBANK_DIR"] == str(destination)
    assert values["PHONE_WORKER_MIKU_ENGLISH_VOICEBANK_DIR"] == str(destination)
    assert values["PHONE_WORKER_MIKU_ALIAS_SUFFIX"] == "_D4"
    assert "TOKEN" not in values and "UNRELATED" not in values
    assert report["voicebank"]["name"] == "MIKU_V4_English"
    assert "fixture-private-value" not in json.dumps(report)
    assert not (environment.parent / "should-not-execute").exists()


def test_idempotent_bank_does_not_require_archive_or_change_mtimes(prepared):
    module, source, destination, environment, _ = prepared
    assert install(prepared)["ok"]
    source.unlink()
    before_env = environment.read_bytes()
    stamp = (destination / "D4/a.wav").stat().st_mtime_ns
    second = install(prepared)
    assert second["ok"] and second["installed"] is False
    assert second["worker_configuration_changed"] is False
    assert environment.read_bytes() == before_env
    assert (destination / "D4/a.wav").stat().st_mtime_ns == stamp


def test_existing_modified_bank_is_preserved_and_not_reconfigured(prepared):
    _, _, destination, environment, _ = prepared
    assert install(prepared)["ok"]
    sample = destination / "D4/a.wav"
    sample.write_bytes(b"local changed sample")
    before = environment.read_bytes()
    failed = install(prepared)
    assert failed["ok"] is False
    assert sample.read_bytes() == b"local changed sample"
    assert environment.read_bytes() == before


def test_destination_containing_user_files_is_never_replaced(prepared):
    _, _, destination, environment, original = prepared
    destination.mkdir(parents=True)
    user_file = destination / "my-recording.wav"
    user_file.write_bytes(b"personal bank")
    report = install(prepared)
    assert report["ok"] is False
    assert user_file.read_bytes() == b"personal bank"
    assert environment.read_bytes() == original


def test_bad_pin_preserves_configuration_and_absent_destination(prepared):
    _, source, destination, environment, original = prepared
    data = bytearray(source.read_bytes())
    data[10] ^= 1
    source.write_bytes(data)
    result = install(prepared)
    assert not result["ok"] and "SHA-256" in result["error"]
    assert not destination.exists()
    assert environment.read_bytes() == original
    assert not list(destination.parent.glob(".miku-stage-*"))


def test_environment_publish_failure_rolls_back_new_bank(prepared, monkeypatch):
    module, _, destination, environment, original = prepared
    def fail(*args):
        raise OSError("fixture publication failure")
    monkeypatch.setattr(module, "publish_environment", fail)
    result = install(prepared)
    assert not result["ok"] and not result["installed"]
    assert result["checks"]["rollback"]["ok"]
    assert not destination.exists()
    assert environment.read_bytes() == original


def test_concurrent_environment_change_preserves_new_settings_and_rolls_back(prepared, monkeypatch):
    module, _, destination, environment, original = prepared
    inspect = module.inspect_bank
    def edit_during_inspect(directory):
        environment.write_bytes(original + b"NEW_SETTING=keep\n")
        return inspect(directory)
    monkeypatch.setattr(module, "inspect_bank", edit_during_inspect)
    result = install(prepared)
    assert not result["ok"] and not destination.exists()
    assert environment.read_bytes() == original + b"NEW_SETTING=keep\n"


@pytest.mark.parametrize("which", ["archive", "destination", "parent", "env"])
def test_symlink_targets_are_rejected(prepared, which):
    module, source, destination, environment, original = prepared
    if which == "archive":
        target = source.with_suffix(".real.zip")
        source.rename(target)
        source.symlink_to(target)
    elif which == "destination":
        target = destination.parent.parent / "other-bank"
        target.mkdir()
        destination.parent.mkdir()
        destination.symlink_to(target, target_is_directory=True)
    elif which == "parent":
        target = destination.parent.parent / "other-voicebanks"
        target.mkdir()
        destination.parent.symlink_to(target, target_is_directory=True)
    else:
        target = environment.with_suffix(".real.env")
        environment.rename(target)
        environment.symlink_to(target)
    assert install(prepared)["ok"] is False
    assert environment.read_bytes() == original


@pytest.mark.parametrize("name", ["../escape.wav", "/absolute.wav", "D4/../../escape", "C:/a.wav", "D4\\..\\escape", "D4//a.wav", "D4/./a.wav"])
def test_unsafe_zip_names_cannot_escape_stage(tmp_path, name):
    module = load_script()
    source = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(name, b"payload")
    with zipfile.ZipFile(source) as archive, pytest.raises(module.InstallError, match="caminho"):
        module.checked_members(archive)
    assert not (tmp_path.parent / "escape.wav").exists()


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK])
def test_special_zip_entries_rejected(tmp_path, kind):
    module = load_script()
    source = tmp_path / "special.zip"
    entry = zipfile.ZipInfo("D4/payload")
    entry.create_system = 3
    entry.external_attr = (kind | 0o600) << 16
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(entry, b"outside")
    with zipfile.ZipFile(source) as archive, pytest.raises(module.InstallError, match="link"):
        module.checked_members(archive)


def test_zip_duplicate_after_slash_normalization_rejected(tmp_path):
    module = load_script()
    source = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("D4/a.wav", b"one")
        archive.writestr("D4\\a.wav", b"two")
    with zipfile.ZipFile(source) as archive, pytest.raises(module.InstallError, match="duplicada"):
        module.checked_members(archive)


def test_update_preserves_comments_deduplicates_only_own_keys_and_shell_quotes(tmp_path):
    module = load_script()
    environment = tmp_path / "worker.env"
    original = ("# first\nTOKEN='other value'\n"
                "PHONE_WORKER_MIKU_ENABLED=false # first own comment\n"
                "export PHONE_WORKER_MIKU_ENABLED=true # duplicate comment\n"
                "OTHER=$(do-not-run)\n").encode()
    updated = module.updated_environment(original, {"PHONE_WORKER_MIKU_ENABLED": "true",
        "PHONE_WORKER_MIKU_VOICEBANK_DIR": "/tmp/path with # hash/$literal/`name`"})
    environment.write_bytes(updated)
    _, values = module.read_environment(environment)
    assert values["PHONE_WORKER_MIKU_VOICEBANK_DIR"] == "/tmp/path with # hash/$literal/`name`"
    assert updated.count(b"PHONE_WORKER_MIKU_ENABLED=") == 1
    assert b"# first own comment" in updated and b"# duplicate comment" in updated
    assert b"TOKEN='other value'\n" in updated and b"OTHER=$(do-not-run)\n" in updated
    assert b"\\$literal" in updated and b"\\`name\\`" in updated


def test_validator_wav_guard_rejects_silence_and_truncation():
    module = load_script(ROOT / "deploy/miku-utau/termux/validate-miku.py")
    assert module.wav_evidence(wave_bytes())["wav_verified"]
    data = wave_bytes()
    with pytest.raises(ValueError, match="truncado"):
        module.wav_evidence(data[:-4])
    with pytest.raises(ValueError, match="silencioso"):
        module.wav_evidence(data[:44] + b"\0" * (len(data) - 44))


def test_resampler_command_honors_container_without_editing_teto_setting(prepared):
    module, _, destination, environment, original = prepared
    _, values = module.read_environment(environment)
    old_command = values["PHONE_WORKER_TETO_RESAMPLER_COMMAND"]
    configured = module.configuration(destination, values, container="miku-arm64")
    command = shlex.split(configured["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"])
    assert command.count("--container") == 1
    assert command[command.index("--container") + 1] == "miku-arm64"
    assert "--implementation-id" in command
    assert values["PHONE_WORKER_TETO_RESAMPLER_COMMAND"] == old_command
    assert environment.read_bytes() == original


@pytest.mark.parametrize("change", [
    {"voice_id": "kasane-teto"}, {"reading_language": "en-US"},
    {"pitch_offset_semitones": -2.0}, {"speech_rate": 1.25},
])
def test_validator_rejects_audio_with_other_voice_or_request_controls(tmp_path, monkeypatch, change):
    module = load_script(ROOT / "deploy/miku-utau/termux/validate-miku.py")
    output = tmp_path / "sample.wav"
    helper = types.SimpleNamespace(check_existing=lambda path: {"name": "Miku"},
                                   atomic_write=lambda path, data: path.write_bytes(data))
    monkeypatch.setattr(module, "installer", lambda: helper)
    monkeypatch.setattr(module, "controls", lambda *args, **kwargs: {})
    class Renderer:
        def __init__(self, **kwargs):
            assert kwargs["voice_id"] == "hatsune-miku"
        def status(self, **kwargs):
            return {"ready": True, "voice_id": "hatsune-miku"}
        def synthesize(self, *args, **kwargs):
            return {"audio": wave_bytes(), "voice_id": "hatsune-miku", "reading_language": "pt-BR",
                    "pitch_offset_semitones": 0.0, "speech_rate": 1.0, **change}
    monkeypatch.setitem(sys.modules, "teto_renderer", types.SimpleNamespace(TetoRenderer=Renderer))
    with pytest.raises(ValueError, match="diferentes"):
        module.render_sample({"voicebank": str(tmp_path), "settings": {}, "container": "arm64",
                              "timeout": 30, "language": "pt-BR", "text": "Olá", "output": str(output)})
    assert not output.exists()


def test_validator_controls_are_voice_local_and_do_not_change_process(prepared, monkeypatch):
    helper, _, destination, environment, _ = prepared
    module = load_script(ROOT / "deploy/miku-utau/termux/validate-miku.py")
    monkeypatch.setattr(module, "installer", lambda: helper)
    monkeypatch.setenv("PHONE_WORKER_TETO_FLAGS", "old-teto-flags")
    monkeypatch.setenv("PHONE_WORKER_TETO_ALIAS_SUFFIX", "old-teto-suffix")
    _, values = helper.read_environment(environment)
    before = dict(os.environ)
    configured = module.controls(values, directory=destination, container="voicepeak-arm64", timeout=90)
    assert dict(os.environ) == before
    assert configured["PHONE_WORKER_TETO_FLAGS"] == ""
    assert configured["PHONE_WORKER_TETO_ALIAS_SUFFIX"] == "_D4"
    assert configured["PHONE_WORKER_TETO_VOICEBANK_DIR"] == str(destination)
    assert configured["PHONE_WORKER_ESPER_CONTAINER"] == "voicepeak-arm64"
