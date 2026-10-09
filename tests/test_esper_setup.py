from __future__ import annotations

import array
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import wave

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/esper-utau/termux/setup.py"
spec = importlib.util.spec_from_file_location("_esper_test_setup", SCRIPT)
assert spec and spec.loader
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def elf(machine=183):
    raw = bytearray(128)
    raw[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", raw, 16, 3, machine)
    return bytes(raw)


def write_wave(path, *, silent=False, rate=44100, seconds=0.238):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(array.array("h", [0 if silent else 1000] * int(rate * seconds)).tobytes())


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    bundle = tmp_path / "kit" / "termux"
    bundle.mkdir(parents=True)
    (bundle / "resampler.py").write_text("#!/usr/bin/env python3\n# fixture wrapper\n")
    (bundle.parent / "LICENSE.txt").write_text("MIT License\nCopyright fixture\n")
    assets = copy.deepcopy(setup.ASSETS)
    payloads = {"engine": elf(), "config": b"[defaults]\nsmoothing=0.1\n"}
    for name, raw in payloads.items():
        assets[name].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    state = SimpleNamespace(root=tmp_path / "runtime", bundle=bundle, assets=assets, payloads=payloads,
                            downloads=[], commands=[], installed=set(setup.REQUIRED_PACKAGES),
                            architecture="arm64", failure=None, probe_mode="valid", probes=0)
    monkeypatch.setattr(setup, "TOOLKIT_ROOT", bundle)
    monkeypatch.setattr(setup, "ASSETS", assets)
    monkeypatch.setattr(setup.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(setup.shutil, "which", lambda executable: "/bin/" + executable)

    def download(asset, output, timeout):
        state.downloads.append(asset)
        output.write_bytes(state.payloads[asset])

    monkeypatch.setattr(setup, "download_asset", download)

    def run(command, timeout, *, environment=None):
        state.commands.append(command)
        if "--print-architecture" in command:
            step, output = "architecture", state.architecture + "\n"
        elif "/usr/bin/dpkg-query" in command:
            step, output = "packages", "".join(f"{name}\tinstall ok installed\n" for name in state.installed)
        elif "/usr/bin/apt-get" in command:
            step, output = "apt", ""
            if "install" in command:
                state.installed.update(command[command.index("--no-install-recommends") + 1:])
        elif str(bundle / "resampler.py") in command:
            step, output = "probe", ""
            state.probes += 1
            assert timeout == 65
            assert environment["PHONE_WORKER_ESPER_CONTAINER"] == "voicepeak-arm64"
            assert environment["PHONE_WORKER_ESPER_TIMEOUT"] == "60"
            stage = Path(environment["PHONE_WORKER_ESPER_ROOT"])
            assert (stage / "releases" / setup.VERSION / "ESPER-Utau").read_bytes() == payloads["engine"]
            if stage != state.root:
                assert not (state.root / "releases" / setup.VERSION).exists()
            arguments = command[-13:]
            source, destination = map(Path, arguments[:2])
            assert source.parent != destination.parent
            with wave.open(str(source), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (44100, 1, 2)
            if state.probe_mode == "missing":
                pass
            elif state.probe_mode == "text":
                destination.write_bytes(b"not a wave" * 10)
            else:
                write_wave(destination, silent=state.probe_mode == "silent", rate=22050 if state.probe_mode == "wrong-rate" else 44100,
                           seconds=0.6 if state.probe_mode == "long" else 0.238)
        else:
            pytest.fail(f"unexpected command: {command}")
        if step == state.failure:
            return {"ok": False, "code": 0, "output": "proot terminated with signal 11"}
        return {"ok": True, "code": 0, "output": output}

    monkeypatch.setattr(setup, "run_process", run)
    return state


def install(state):
    return setup.install_runtime(root=state.root)


def test_stages_probes_and_atomically_publishes_runtime_without_changing_worker(prepared, tmp_path):
    environment = tmp_path / ".phone-worker.env"
    environment.write_bytes(b"PHONE_WORKER_TETO_BACKEND=worldline-r\nTOKEN=secret\n")
    before = environment.read_bytes()
    report = install(prepared)
    assert report["ok"] and report["runtime_ready"] and report["runtime_changed"]
    release = prepared.root / "releases" / setup.VERSION
    assert (release / "ESPER-Utau").read_bytes() == prepared.payloads["engine"]
    assert (release / "ESPER-Utau").stat().st_mode & 0o111
    assert json.loads((release / "release.json").read_text())["native_architecture"] == "arm64"
    assert (release / "LICENSE.txt").read_text().startswith("MIT License")
    assert (prepared.root / "bin" / "resampler.py").read_bytes() == (prepared.bundle / "resampler.py").read_bytes()
    assert (prepared.root / "bin" / "esper-utau-resampler").read_bytes() == (prepared.bundle / "resampler.py").read_bytes()
    assert report["checks"]["native_probe"]["synthetic_render_verified"]
    assert environment.read_bytes() == before
    assert "secret" not in json.dumps(report)
    assert not report["worker_configuration_changed"] and not report["production_backend_changed"]
    assert not report["teto_synthesis_verified"] and not report["portuguese_quality_verified"]
    assert prepared.downloads == ["engine", "config"]
    assert not report["packages_installed"]
    assert not (prepared.root / ".setup.lock").exists()
    assert not list(prepared.root.glob(".esper-stage-*"))


def test_idempotent_matching_release_is_probed_without_download_or_replacement(prepared):
    assert install(prepared)["ok"]
    path = prepared.root / "releases" / setup.VERSION / "ESPER-Utau"
    before = path.stat()
    report = install(prepared)
    assert report["ok"] and not report["runtime_changed"]
    assert prepared.downloads == ["engine", "config"]
    assert prepared.probes == 2
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_android_without_os_link_is_supported(prepared, monkeypatch):
    monkeypatch.delattr(setup.os, "link", raising=False)
    assert install(prepared)["runtime_ready"]


@pytest.mark.parametrize("architecture", ["x86_64", "armv7l"])
def test_wrong_host_does_not_download_or_create_runtime(prepared, monkeypatch, architecture):
    monkeypatch.setattr(setup.platform, "machine", lambda: architecture)
    report = install(prepared)
    assert not report["ok"]
    assert not prepared.downloads and not prepared.commands and not prepared.root.exists()


@pytest.mark.parametrize("executable", ["python", "ffmpeg", "proot-distro"])
def test_missing_host_requirement_is_reported_without_guest_changes(prepared, monkeypatch, executable):
    monkeypatch.setattr(setup.shutil, "which", lambda name: None if name == executable else "/bin/" + name)
    report = install(prepared)
    assert not report["ok"] and executable in report["error"]
    assert not prepared.downloads and not prepared.commands


def test_missing_guest_is_reported_without_creating_guest_or_downloading(prepared):
    prepared.failure = "architecture"
    report = install(prepared)
    assert not report["ok"] and not prepared.downloads
    assert not any("install" in c[:3] for c in prepared.commands)


def test_wrong_guest_architecture_never_attempts_emulation(prepared):
    prepared.architecture = "amd64"
    assert not install(prepared)["ok"]
    assert not prepared.downloads
    assert not any("qemu" in json.dumps(c) or "box64" in json.dumps(c) for c in prepared.commands)


def test_unconfigured_libc_is_not_hidden_by_package_install(prepared):
    prepared.installed.remove("libc6")
    report = install(prepared)
    assert not report["ok"] and "libc6" in report["error"]
    assert not any("/usr/bin/apt-get" in c for c in prepared.commands)


def test_only_missing_guest_dependencies_are_installed_and_requeried(prepared):
    prepared.installed.remove("zlib1g")
    report = install(prepared)
    assert report["ok"] and report["packages_installed"] == ["zlib1g"]
    command = next(c for c in prepared.commands if "/usr/bin/apt-get" in c and "install" in c)
    assert command[command.index("--no-install-recommends") + 1:] == ["zlib1g"]
    assert "guest_packages_after" in report["checks"]


@pytest.mark.parametrize("asset", ["engine", "config"])
def test_existing_changed_engine_or_ini_is_preserved_and_never_downloaded(prepared, asset):
    assert install(prepared)["ok"]
    path = prepared.root / "releases" / setup.VERSION / prepared.assets[asset]["name"]
    changed = b"operator existing customized file"
    path.write_bytes(changed)
    report = install(prepared)
    assert not report["ok"] and path.read_bytes() == changed
    assert prepared.downloads == ["engine", "config"]
    assert prepared.probes == 1


def test_existing_custom_launcher_is_preserved(prepared):
    path = prepared.root / "bin" / "resampler.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"custom launcher")
    assert not install(prepared)["ok"]
    assert path.read_bytes() == b"custom launcher"
    assert not prepared.downloads


def test_download_hash_mismatch_never_publishes_or_erases_old_release(prepared):
    old = prepared.root / "releases" / "v2.4.0"
    old.mkdir(parents=True)
    (old / "operator.txt").write_bytes(b"preserved")
    prepared.payloads["engine"] = b"bad official-looking download"
    report = install(prepared)
    assert not report["ok"] and not report["runtime_changed"]
    assert not (prepared.root / "releases" / setup.VERSION).exists()
    assert (old / "operator.txt").read_bytes() == b"preserved"
    assert not (prepared.root / ".setup.lock").exists()


@pytest.mark.parametrize("mode", ["missing", "text", "silent", "wrong-rate", "long"])
def test_zero_exit_cannot_publish_invalid_synthetic_audio(prepared, mode):
    prepared.probe_mode = mode
    report = install(prepared)
    assert not report["ok"] and not report["runtime_ready"]
    assert not (prepared.root / "releases" / setup.VERSION).exists()
    assert not (prepared.root / "bin" / "resampler.py").exists()


def test_failed_reprobe_preserves_already_installed_release(prepared):
    assert install(prepared)["ok"]
    path = prepared.root / "releases" / setup.VERSION / "ESPER-Utau"
    before = path.read_bytes()
    prepared.failure = "probe"
    report = install(prepared)
    assert not report["ok"] and not report["runtime_changed"]
    assert "proot terminated with signal 11" in report["error"]
    assert path.read_bytes() == before


def test_lock_owned_by_another_installer_is_preserved(prepared):
    prepared.root.mkdir()
    lock = prepared.root / ".setup.lock"
    lock.write_bytes(b"another installer")
    assert not install(prepared)["ok"]
    assert lock.read_bytes() == b"another installer"
    assert not prepared.downloads


def test_symlink_root_cannot_redirect_installation(prepared, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    prepared.root.symlink_to(elsewhere, target_is_directory=True)
    assert not install(prepared)["ok"]
    assert not list(elsewhere.iterdir()) and not prepared.downloads


def test_symlink_asset_is_refused_even_when_no_os_nofollow(prepared, tmp_path, monkeypatch):
    real = tmp_path / "engine"
    real.write_bytes(prepared.payloads["engine"])
    link = tmp_path / "engine-link"
    link.symlink_to(real)
    monkeypatch.delattr(setup.os, "O_NOFOLLOW", raising=False)
    with pytest.raises(setup.SetupError, match="link"):
        setup.verify_asset(link, "engine")


@pytest.mark.parametrize("url", ["http://github.com/release", "https://evil.test/release", "https://github.com:444/release", "https://user:pass@github.com/release"])
def test_download_redirects_cannot_leave_official_https_hosts(url):
    with pytest.raises(setup.SetupError):
        setup.checked_url(url)


@pytest.mark.parametrize("asset,change", [("engine", "length"), ("engine", "hash"), ("config", "hash")])
def test_corrupt_stream_is_rejected_before_any_publication(prepared, tmp_path, monkeypatch, asset, change):
    expected = prepared.assets[asset]
    raw = prepared.payloads[asset]
    if change == "length":
        raw = raw[:-1]
    else:
        raw = b"x" * len(raw)
    response = io.BytesIO(raw)
    response.headers = {}
    response.geturl = lambda: expected["url"]
    monkeypatch.setattr(setup.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *args, **kwargs: response))
    with pytest.raises(setup.SetupError):
        setup.stream_download(asset, tmp_path / "download", 20)


def test_valid_official_stream_is_checked_and_written_privately(prepared, tmp_path, monkeypatch):
    response = io.BytesIO(prepared.payloads["engine"])
    response.headers = {"Content-Length": str(len(prepared.payloads["engine"]))}
    response.geturl = lambda: "https://release-assets.githubusercontent.com/github-production-release-asset/file"
    monkeypatch.setattr(setup.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *args, **kwargs: response))
    path = tmp_path / "download"
    setup.stream_download("engine", path, 20)
    assert path.read_bytes() == prepared.payloads["engine"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_zero_exit_with_proot_signal_is_failure():
    result = setup.run_process([sys.executable, "-c", "print('proot info: vpid 1: terminated with signal 11')"], 5)
    assert result["code"] == 0 and not result["ok"]


@pytest.mark.parametrize("value", ["nan", "inf", "9", "301", "not-a-number"])
def test_download_deadline_rejects_invalid_values(value):
    with pytest.raises(setup.argparse.ArgumentTypeError):
        setup.valid_timeout(value)


def test_container_arguments_cannot_inject_guest_options(prepared):
    report = setup.install_runtime(root=prepared.root, container="--bind=/private")
    assert not report["ok"] and not prepared.commands


def test_release_pins_match_independently_verified_official_api_assets():
    assert setup.ASSETS["engine"]["bytes"] == 107335629
    assert setup.ASSETS["engine"]["sha256"] == "e2cb6dc113593cb3b788debb51996f591a5f9d47bbd2a54df57bc1ecd843602f"
    assert setup.ASSETS["config"]["bytes"] == 762
    assert setup.ASSETS["config"]["sha256"] == "21951154b9bafdebde0b3a1d6533bb1fde549113b63b99a573b49ea68fadbe83"
