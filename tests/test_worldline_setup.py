from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/worldline-r/termux/setup.py"
spec = importlib.util.spec_from_file_location("_worldline_test_setup", SCRIPT)
assert spec and spec.loader
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def elf(machine=183):
    data = bytearray(128)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", data, 16, 3, machine)
    return bytes(data)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    data = elf()
    real_runtime = setup.doctor.load_runtime_module()
    runtime = SimpleNamespace(
        RELEASE_SHA256=hashlib.sha256(data).hexdigest(), RELEASE_URL=real_runtime.RELEASE_URL,
        LICENSE_URL=real_runtime.LICENSE_URL, SOURCE_COMMIT=real_runtime.SOURCE_COMMIT,
        SOURCE_VERSION=real_runtime.SOURCE_VERSION,
    )
    state = SimpleNamespace(data=data, runtime=runtime, commands=[], library_dir=tmp_path / "worldline" / "lib",
                            installed=set(setup.doctor.REQUIRED_PACKAGES), architecture="arm64", downloads=0,
                            probe_override=None, fail_step=None)
    monkeypatch.setattr(setup.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(setup.shutil, "which", lambda executable: "/bin/proot-distro" if executable == "proot-distro" else None)
    monkeypatch.setattr(setup.doctor, "load_runtime_module", lambda: runtime)

    def download(_runtime):
        state.downloads += 1
        return state.data

    monkeypatch.setattr(setup, "download_library", download)

    def bounded(command, timeout=20):
        state.commands.append(command)
        if "/usr/bin/dpkg" in command:
            step, output = "architecture", state.architecture + "\n"
        elif "/usr/bin/dpkg-query" in command:
            step = "packages"
            output = "".join(f"{name}\tinstall ok installed\n" for name in sorted(state.installed))
        elif "/usr/bin/apt-get" in command:
            step = "install" if "install" in command else "update"
            output = ""
            if step == "install":
                state.installed.update(command[command.index("--no-install-recommends") + 1:])
        elif "/usr/bin/python3" in command:
            step = "probe"
            document = {
                "runtime_ok": True, "api_verified": True, "abi_verified": True,
                "library_hash_verified": True, "synthetic_render_verified": True,
                "native_architecture": "arm64", "source_commit": runtime.SOURCE_COMMIT,
                "source_version": runtime.SOURCE_VERSION, "library_sha256": runtime.RELEASE_SHA256,
            }
            document.update(state.probe_override or {})
            output = json.dumps(document)
        else:
            pytest.fail(f"unexpected command: {command}")
        if state.fail_step == step:
            return {"ok": False, "code": 0, "signal": 11, "error": "PRoot/QEMU reportou término por sinal"}
        return {"ok": True, "code": 0, "output": output}

    monkeypatch.setattr(setup.doctor, "bounded", bounded)
    return state


def install(state):
    return setup.install_runtime(library_dir=state.library_dir)


def test_setup_publishes_verified_arm64_library_with_mit_notice_and_no_worker_changes(prepared, tmp_path):
    environment = tmp_path / ".phone-worker.env"
    environment.write_bytes(b"PHONE_WORKER_TETO_BACKEND=voicebank\nTOKEN=private-value\n")
    before = environment.read_bytes()
    report = install(prepared)
    assert report["runtime_ready"] is True
    assert report["tts_ready"] is False
    assert report["phrase_adapter_available"] is False
    assert report["teto_synthesis_verified"] is False
    assert report["portuguese_speech_verified"] is False
    assert (prepared.library_dir / "libworldline.so").read_bytes() == prepared.data
    assert (prepared.library_dir / "LICENSE.openutau.txt").read_bytes().startswith(b"The MIT License (MIT)")
    assert environment.read_bytes() == before
    assert "private-value" not in json.dumps(report)
    assert prepared.downloads == 1
    assert not report["packages_installed"]
    assert not any("/usr/bin/apt-get" in command for command in prepared.commands)
    command = next(command for command in prepared.commands if "/usr/bin/python3" in command)
    assert "--render-probe" in command
    assert "--bind" in command
    assert not any(name in json.dumps(prepared.commands).lower() for name in ("box64", "dotnet", "x11", "voicepeak/"))


def test_setup_works_when_android_python_has_no_os_link(prepared, monkeypatch):
    monkeypatch.delattr(setup.os, "link", raising=False)
    assert install(prepared)["runtime_ready"] is True


def test_idempotent_matching_library_does_not_download_or_replace_it(prepared):
    prepared.library_dir.mkdir(parents=True)
    library = prepared.library_dir / "libworldline.so"
    library.write_bytes(prepared.data)
    before = library.stat()
    report = install(prepared)
    assert report["runtime_ready"] is True
    assert report["library_changed"] is False
    assert prepared.downloads == 0
    assert library.stat().st_ino == before.st_ino
    assert library.stat().st_mtime_ns == before.st_mtime_ns
    assert "backup" not in report


def test_atomic_replace_keeps_private_exact_backup_of_previous_library(prepared, monkeypatch):
    prepared.library_dir.mkdir(parents=True)
    library = prepared.library_dir / "libworldline.so"
    previous = b"previous-library-exact-contents"
    library.write_bytes(previous)
    calls = []
    actual_replace = setup.os.replace

    def tracked_replace(source, destination):
        if Path(destination) == library:
            assert Path(source).parent.name.startswith(".worldline-stage-")
            assert library.read_bytes() == previous
            backups = list(library.parent.glob("libworldline.so.backup-*"))
            assert len(backups) == 1 and backups[0].read_bytes() == previous
        calls.append(Path(destination))
        return actual_replace(source, destination)

    monkeypatch.setattr(setup.os, "replace", tracked_replace)
    report = install(prepared)
    assert report["runtime_ready"] is True
    assert report["library_changed"] is True
    backup = Path(report["backup"])
    assert backup.read_bytes() == previous
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert library.read_bytes() == prepared.data
    assert library in calls


@pytest.mark.parametrize("bad", [elf(62), b"not-an-elf" * 10, elf() + b"altered"])
def test_wrong_elf_or_hash_cannot_reach_native_loader_or_replace_previous_library(prepared, bad):
    prepared.library_dir.mkdir(parents=True)
    library = prepared.library_dir / "libworldline.so"
    previous = b"keep-this-existing-library"
    library.write_bytes(previous)
    prepared.data = bad
    report = install(prepared)
    assert report["runtime_ready"] is False
    assert library.read_bytes() == previous
    assert not any("/usr/bin/python3" in command for command in prepared.commands)
    assert not list(prepared.library_dir.glob("libworldline.so.backup-*"))
    assert not list(prepared.library_dir.glob(".worldline-stage-*"))


@pytest.mark.parametrize("field,value", [
    ("runtime_ok", False), ("api_verified", True + 0), ("abi_verified", False),
    ("library_hash_verified", False), ("synthetic_render_verified", False),
    ("native_architecture", "x64"), ("source_commit", "another-commit"),
    ("source_version", "newer"), ("library_sha256", "wrong"),
])
def test_incomplete_or_wrong_pinned_native_evidence_never_publishes(prepared, field, value):
    prepared.probe_override = {field: value}
    report = install(prepared)
    assert report["runtime_ready"] is False
    assert not (prepared.library_dir / "libworldline.so").exists()
    assert not (prepared.library_dir / "LICENSE.openutau.txt").exists()
    assert "probe" in report["error"]


def test_malformed_probe_json_never_publishes(prepared, monkeypatch):
    actual = setup.doctor.bounded

    def fake(command, timeout=20):
        if "/usr/bin/python3" in command:
            return {"ok": True, "code": 0, "output": "not-json"}
        return actual(command, timeout=timeout)

    monkeypatch.setattr(setup.doctor, "bounded", fake)
    report = install(prepared)
    assert not report["runtime_ready"]
    assert "JSON" in report["error"]
    assert not (prepared.library_dir / "libworldline.so").exists()


@pytest.mark.parametrize("architecture", ["amd64", "armhf", "arm64\nproot info: vpid 1: terminated with signal 11"])
def test_wrong_or_false_zero_guest_architecture_has_no_mutations(prepared, architecture):
    prepared.architecture = architecture
    report = install(prepared)
    assert not report["runtime_ready"]
    assert prepared.downloads == 0
    assert not prepared.library_dir.exists()
    assert len(prepared.commands) == 1


def test_wrong_host_architecture_stops_before_proot_or_download(prepared, monkeypatch):
    monkeypatch.setattr(setup.platform, "machine", lambda: "x86_64")
    report = install(prepared)
    assert not report["runtime_ready"]
    assert not prepared.commands and prepared.downloads == 0
    assert not prepared.library_dir.exists()


def test_only_missing_allowlisted_packages_are_installed_and_rechecked(prepared):
    prepared.installed.remove("python3")
    report = install(prepared)
    assert report["runtime_ready"]
    assert report["packages_installed"] == ["python3"]
    command = next(command for command in prepared.commands if "/usr/bin/apt-get" in command and "install" in command)
    assert command[command.index("--no-install-recommends") + 1:] == ["python3"]
    assert report["checks"]["packages_after"]["packages"]["python3"] is True


def test_unhealthy_libc_cannot_trigger_apt_install(prepared):
    prepared.installed.remove("libc6")
    report = install(prepared)
    assert not report["runtime_ready"]
    assert "libc6" in report["error"]
    assert not any("/usr/bin/apt-get" in command for command in prepared.commands)
    assert prepared.downloads == 0


@pytest.mark.parametrize("step", ["architecture", "packages", "probe", "update", "install"])
def test_proot_crash_with_exit_zero_is_not_accepted(prepared, step):
    if step in {"update", "install"}:
        prepared.installed.remove("python3")
    prepared.fail_step = step
    report = install(prepared)
    assert not report["runtime_ready"]
    assert not (prepared.library_dir / "libworldline.so").exists()
    assert any(value.get("signal") == 11 for value in report["checks"].values())


def test_shared_bounded_function_recognizes_actual_false_zero_output():
    report = setup.doctor.bounded([sys.executable, "-c", "print('proot info: vpid 1: terminated with signal 11')"])
    assert report["ok"] is False and report["code"] == 0 and report["signal"] == 11


@pytest.mark.parametrize("target", ["directory", "parent", "library"])
def test_symlink_destinations_are_refused_without_guest_mutations(prepared, tmp_path, target):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    prepared.library_dir.parent.mkdir()
    if target == "directory":
        prepared.library_dir.symlink_to(elsewhere, target_is_directory=True)
    elif target == "parent":
        prepared.library_dir.parent.rmdir()
        prepared.library_dir.parent.symlink_to(elsewhere, target_is_directory=True)
    else:
        prepared.library_dir.mkdir()
        previous = elsewhere / "existing-library"
        previous.write_bytes(b"untouched")
        (prepared.library_dir / "libworldline.so").symlink_to(previous)
    report = install(prepared)
    assert not report["runtime_ready"]
    assert not prepared.commands and prepared.downloads == 0
    if target == "library":
        assert previous.read_bytes() == b"untouched"


class Download(io.BytesIO):
    def __init__(self, data, headers=None, url="https://raw.githubusercontent.com/openutau/OpenUtau/pinned/libworldline.so"):
        super().__init__(data)
        self.headers = headers or {}
        self.url = url

    def geturl(self):
        return self.url


@pytest.mark.parametrize("response", [
    Download(b"", {"Content-Length": str(setup.MAX_LIBRARY_BYTES + 1)}),
    Download(b"", {"Content-Length": "invalid"}),
    Download(b"unsafe", url="http://raw.githubusercontent.com/file"),
    Download(b"unsafe", url="https://example.com/file"),
    Download(b"x" * (setup.MAX_LIBRARY_BYTES + 1)),
])
def test_downloader_rejects_oversized_or_redirected_responses(prepared, monkeypatch, response):
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *args, **kwargs: response)
    with pytest.raises(setup.SetupError):
        # Fixture replaces download_library; use its saved source implementation below.
        real_download(prepared.runtime)


real_download = setup.download_library


def test_downloader_uses_official_https_with_timeout_and_byte_limit(prepared, monkeypatch):
    calls = []

    def open_url(request, timeout):
        calls.append((request.full_url, timeout))
        return Download(prepared.data, {"Content-Length": str(len(prepared.data))})

    monkeypatch.setattr(setup.urllib.request, "urlopen", open_url)
    assert real_download(prepared.runtime) == prepared.data
    assert calls == [(prepared.runtime.RELEASE_URL, 30.0)]


def test_concurrent_library_change_is_preserved(prepared, monkeypatch):
    actual = setup.doctor.bounded

    def changed(command, timeout=20):
        report = actual(command, timeout=timeout)
        if "/usr/bin/python3" in command:
            (prepared.library_dir / "libworldline.so").write_bytes(b"concurrent-user-file")
        return report

    monkeypatch.setattr(setup.doctor, "bounded", changed)
    report = install(prepared)
    assert not report["runtime_ready"]
    assert (prepared.library_dir / "libworldline.so").read_bytes() == b"concurrent-user-file"
    assert not list(prepared.library_dir.glob("libworldline.so.backup-*"))


def test_probe_failure_preserves_existing_library_bytes_and_inode(prepared):
    prepared.library_dir.mkdir(parents=True)
    library = prepared.library_dir / "libworldline.so"
    library.write_bytes(b"previous-library")
    before = library.stat()
    prepared.probe_override = {"synthetic_render_verified": False}
    report = install(prepared)
    assert not report["runtime_ready"]
    assert library.read_bytes() == b"previous-library"
    assert library.stat().st_ino == before.st_ino
    assert not list(prepared.library_dir.glob("libworldline.so.backup-*"))


def test_failed_atomic_publication_keeps_previous_library_and_exact_backup(prepared, monkeypatch):
    prepared.library_dir.mkdir(parents=True)
    library = prepared.library_dir / "libworldline.so"
    library.write_bytes(b"previous-library")
    actual_replace = setup.os.replace

    def failing_replace(source, destination):
        if Path(destination) == library:
            raise OSError("simulated storage failure")
        return actual_replace(source, destination)

    monkeypatch.setattr(setup.os, "replace", failing_replace)
    report = install(prepared)
    assert not report["runtime_ready"]
    assert library.read_bytes() == b"previous-library"
    assert Path(report["backup"]).read_bytes() == b"previous-library"
    assert not list(prepared.library_dir.glob(".worldline-stage-*"))


def test_apt_success_without_installed_package_is_rejected_before_download(prepared, monkeypatch):
    prepared.installed.remove("python3")
    actual = setup.doctor.bounded

    def fake(command, timeout=20):
        value = actual(command, timeout=timeout)
        if "/usr/bin/apt-get" in command and "install" in command:
            prepared.installed.remove("python3")
        return value

    monkeypatch.setattr(setup.doctor, "bounded", fake)
    report = install(prepared)
    assert not report["runtime_ready"]
    assert prepared.downloads == 0
    assert not prepared.library_dir.exists()


def test_package_query_failure_does_not_turn_unknown_state_into_missing_packages(prepared):
    prepared.fail_step = "packages"
    prepared.installed.remove("python3")
    report = install(prepared)
    assert not report["runtime_ready"]
    assert not any("/usr/bin/apt-get" in command for command in prepared.commands)


def test_proot_distro_absence_has_actionable_error_without_mutations(prepared, monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    report = install(prepared)
    assert not report["runtime_ready"]
    assert "proot-distro ausente" in report["error"]
    assert not prepared.commands and prepared.downloads == 0


def test_preexisting_notice_is_preserved_and_blocks_unrelated_replacement(prepared):
    prepared.library_dir.mkdir(parents=True)
    notice = prepared.library_dir / "LICENSE.openutau.txt"
    notice.write_bytes(b"unrelated-user-content")
    report = install(prepared)
    assert not report["runtime_ready"]
    assert notice.read_bytes() == b"unrelated-user-content"
    assert prepared.downloads == 0


def test_downloader_deadline_prevents_an_unbounded_read_loop(prepared, monkeypatch):
    ticks = iter([100.0, 131.0])
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *args, **kwargs: Download(prepared.data))
    with pytest.raises(setup.SetupError, match="30 segundos"):
        real_download(prepared.runtime)
