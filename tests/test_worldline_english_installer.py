from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import struct
import types
import wave
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/worldline-r/termux/install-english-voicebank.py"


@pytest.fixture
def installer():
    module = types.ModuleType("english_voicebank_installer_test")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), "exec"), module.__dict__)
    return module


def make_bank_zip(path: Path, *, aliases=500):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as writer:
        writer.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack("<h", 1200) * 441)
    entries = {
        "install.txt": b"Keep this installation notice.\n",
        "重音テト/character.txt": "name=重音テト\n".encode(),
        "重音テト/English/README.txt": b"Keep these voicebank terms.\n",
        "重音テト/English/sample.wav": audio.getvalue(),
        "重音テト/English/oto.ini": "".join(
            f"sample.wav=phoneme-{i},0,0,0,0,0\n" for i in range(aliases)
        ).encode(),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return entries


def offline_source(installer, monkeypatch, archive, *, pin_fixture=True):
    if pin_fixture:
        data = archive.read_bytes()
        monkeypatch.setattr(installer, "SOURCE_BYTES", len(data))
        monkeypatch.setattr(installer, "SOURCE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(installer, "download_archive", lambda output, timeout: shutil.copyfile(archive, output))


def test_atomic_install_preserves_all_names_notices_bank_and_worker_settings(tmp_path, installer, monkeypatch):
    source = tmp_path / "source.zip"
    expected = make_bank_zip(source)
    offline_source(installer, monkeypatch, source)
    envfile = tmp_path / ".phone-worker.env"
    envfile.write_bytes(b"PHONE_WORKER_TETO_VOICEBANK_MODE=standard\nTOKEN=untouched\n")
    japanese = tmp_path / "kasane-teto"
    japanese.mkdir()
    (japanese / "oto.ini").write_bytes(b"original Japanese bank")
    destination = tmp_path / "banks" / "english"
    before = dict(os.environ)
    # Termux Android Python may omit os.link; publication must not require it.
    monkeypatch.delattr(installer.os, "link", raising=False)
    result = installer.install_voicebank(directory=destination)
    assert result["ok"] and result["installed"] and result["voicebank_ready"]
    assert result["checks"]["publication"]["atomic"]
    assert result["voicebank"]["aliases"] == 500
    assert result["voicebank"]["root"] == str(destination)
    assert not result["worker_configuration_changed"]
    assert not result["portuguese_quality_verified"] and not result["teto_synthesis_verified"]
    assert dict(os.environ) == before
    assert envfile.read_bytes() == b"PHONE_WORKER_TETO_VOICEBANK_MODE=standard\nTOKEN=untouched\n"
    assert (japanese / "oto.ini").read_bytes() == b"original Japanese bank"
    actual = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    assert actual == expected
    assert not list(destination.parent.glob(".teto-english-stage-*"))
    assert not list(destination.parent.glob("*.lock"))
    # A second run preserves the installed bank rather than overwriting it.
    monkeypatch.setattr(installer, "download_archive", lambda *_: pytest.fail("download on existing bank"))
    repeated = installer.install_voicebank(directory=destination)
    assert not repeated["ok"] and not repeated["installed"]
    assert "preservada" in repeated["error"]
    assert {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()} == expected


@pytest.mark.parametrize("failure", ["wrong-hash", "few-aliases", "no-oto", "download-timeout"])
def test_failure_does_not_publish_or_leave_staging_files(tmp_path, installer, monkeypatch, failure):
    archive = tmp_path / "source.zip"
    make_bank_zip(archive, aliases=1 if failure == "few-aliases" else 500)
    if failure == "no-oto":
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("character.txt", "name=Kasane Teto\n")
    offline_source(installer, monkeypatch, archive)
    if failure == "wrong-hash":
        monkeypatch.setattr(installer, "SOURCE_SHA256", "0" * 64)
    elif failure == "download-timeout":
        def fail(*_):
            raise installer.InstallError("download excedeu o timeout")
        monkeypatch.setattr(installer, "download_archive", fail)
    destination = tmp_path / "english"
    result = installer.install_voicebank(directory=destination)
    assert not result["ok"] and not result["installed"]
    assert result.get("error")
    assert not destination.exists()
    assert not list(tmp_path.glob(".teto-english-stage-*"))
    assert not list(tmp_path.glob("*.lock"))


@pytest.mark.parametrize("name", ["../escape.wav", "/escape.wav", "folder/../../escape.wav",
                                  "C:/escape.wav", "folder//escape.wav", "folder/./escape.wav",
                                  "..\\escape.wav"])
def test_zip_paths_cannot_escape_the_bank(tmp_path, installer, name):
    source = tmp_path / "bad.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(name, b"bad")
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(installer.InstallError):
        installer.extract_archive(source, stage)
    assert not list(stage.iterdir()) and not (tmp_path / "escape.wav").exists()


@pytest.mark.parametrize("kind", ["symlink", "parent-is-file", "duplicate-decoded-name", "file-size"])
def test_invalid_archive_is_rejected_before_extraction(tmp_path, installer, monkeypatch, kind):
    source = tmp_path / "bad.zip"
    with zipfile.ZipFile(source, "w") as archive:
        if kind == "symlink":
            entry = zipfile.ZipInfo("link.wav")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../outside.wav")
        elif kind == "parent-is-file":
            archive.writestr("folder", "file")
            archive.writestr("folder/oto.ini", "data")
        elif kind == "duplicate-decoded-name":
            archive.writestr("folder\\sample.wav", "one")
            archive.writestr("folder/sample.wav", "two")
        else:
            archive.writestr("sample.wav", "too large")
            monkeypatch.setattr(installer, "MAX_FILE_BYTES", 1)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(installer.InstallError):
        installer.extract_archive(source, stage)
    assert not list(stage.iterdir())


def test_symlink_destination_and_busy_lock_are_preserved(tmp_path, installer, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "saved.wav").write_bytes(b"preserved")
    link = tmp_path / "link"
    link.symlink_to(existing, target_is_directory=True)
    monkeypatch.setattr(installer, "download_archive", lambda *_: pytest.fail("download before destination checks"))
    assert not installer.install_voicebank(directory=link)["ok"]
    assert link.is_symlink() and (existing / "saved.wav").read_bytes() == b"preserved"
    lock = tmp_path / ".english.teto-english-install.lock"
    lock.write_bytes(b"another process")
    assert not installer.install_voicebank(directory=tmp_path / "english")["ok"]
    assert lock.read_bytes() == b"another process"


def test_official_redirect_policy(installer):
    assert installer.official_url(installer.SOURCE_URL)
    for url in ("http://kasaneteto.jp/bank.zip", "https://other.test/bank.zip",
                "https://kasaneteto.jp:444/bank.zip", "https://user@kasaneteto.jp/bank.zip"):
        assert not installer.official_url(url)
        with pytest.raises(installer.InstallError):
            installer.OfficialRedirects().redirect_request(None, None, 302, "", {}, url)


def test_real_official_zip_pin_crc_index_and_all_original_files(tmp_path, installer, monkeypatch):
    configured = os.environ.get("TETO_ENGLISH_TEST_ARCHIVE")
    if not configured:
        pytest.skip("set TETO_ENGLISH_TEST_ARCHIVE to exercise the downloaded official bank offline")
    source = Path(configured)
    offline_source(installer, monkeypatch, source, pin_fixture=False)
    destination = tmp_path / "english"
    result = installer.install_voicebank(directory=destination)
    assert result["ok"], result
    assert result["checks"]["archive_pin"]["sha256"] == installer.SOURCE_SHA256
    assert result["checks"]["extraction"] == {"ok": True, "entries": 1125,
                                               "unpacked_bytes": 94153463, "crc_verified": True}
    assert result["voicebank"]["aliases"] == 2673
    with zipfile.ZipFile(source) as archive:
        expected = {installer.decoded_name(info).replace("\\", "/"): archive.read(info)
                    for info in archive.infolist() if not info.is_dir()}
    actual = {str(path.relative_to(destination)): path.read_bytes()
              for path in destination.rglob("*") if path.is_file()}
    assert actual == expected
    assert not result["worker_configuration_changed"] and not result["teto_synthesis_verified"]
