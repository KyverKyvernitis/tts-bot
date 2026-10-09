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
import zipfile

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


def test_failed_probe_keeps_verified_downloads_and_next_retry_reuses_them(prepared):
    prepared.failure = "probe"
    failure = install(prepared)
    assert not failure["ok"] and not failure["runtime_ready"]
    assert not (prepared.root / "releases" / setup.VERSION).exists()
    assert not (prepared.root / "bin" / "resampler.py").exists()
    cache = prepared.root / "downloads" / setup.VERSION
    snapshots = {}
    for asset, expected in prepared.assets.items():
        path = cache / expected["name"]
        assert path.read_bytes() == prepared.payloads[asset]
        assert path.stat().st_mode & 0o777 == 0o600
        snapshots[asset] = path.stat()
    assert not failure["checks"]["download"]["skipped"]
    assert not failure["checks"]["download"]["cached_download"]
    prepared.failure = None
    recovered = install(prepared)
    assert recovered["ok"] and recovered["runtime_ready"]
    assert recovered["checks"]["download"]["cached_download"]
    assert recovered["checks"]["download"]["skipped"]
    assert set(recovered["checks"]["download"]["cached_assets"]) == {"engine", "config"}
    assert prepared.downloads == ["engine", "config"]
    assert prepared.probes == 2
    for asset, expected in prepared.assets.items():
        path = cache / expected["name"]
        published = prepared.root / "releases" / setup.VERSION / expected["name"]
        assert path.stat().st_ino == snapshots[asset].st_ino
        assert path.stat().st_mtime_ns == snapshots[asset].st_mtime_ns
        assert published.stat().st_ino != path.stat().st_ino


def test_retry_revalidates_cache_and_preserves_modified_download(prepared):
    prepared.failure = "probe"
    assert not install(prepared)["ok"]
    path = prepared.root / "downloads" / setup.VERSION / "ESPER-Utau"
    changed = b"customized cached engine"
    path.write_bytes(changed)
    prepared.failure = None
    report = install(prepared)
    assert not report["ok"] and "preservado" in report["error"]
    assert path.read_bytes() == changed
    assert prepared.downloads == ["engine", "config"]
    assert prepared.probes == 1
    assert not (prepared.root / "releases" / setup.VERSION).exists()


def test_config_download_failure_keeps_engine_for_partial_retry(prepared, monkeypatch):
    original_download = setup.download_asset

    def download(asset, output, timeout):
        if asset == "config":
            raise setup.SetupError("connection failed")
        original_download(asset, output, timeout)

    monkeypatch.setattr(setup, "download_asset", download)
    failure = install(prepared)
    assert not failure["ok"]
    cache = prepared.root / "downloads" / setup.VERSION
    assert (cache / "ESPER-Utau").read_bytes() == prepared.payloads["engine"]
    assert not (cache / "esper-config.ini").exists()
    assert not list(cache.glob(".download-stage-*"))
    monkeypatch.setattr(setup, "download_asset", original_download)
    recovered = install(prepared)
    assert recovered["ok"]
    assert recovered["checks"]["download"]["cached_download"]
    assert not recovered["checks"]["download"]["skipped"]
    assert recovered["checks"]["download"]["cached_assets"] == ["engine"]
    assert prepared.downloads == ["engine", "config"]


def test_malformed_cached_config_is_preserved_before_any_download(prepared):
    cache = prepared.root / "downloads" / setup.VERSION
    cache.mkdir(parents=True)
    changed = b"operator modified configuration"
    (cache / "esper-config.ini").write_bytes(changed)
    report = install(prepared)
    assert not report["ok"] and not prepared.downloads and not prepared.probes
    assert (cache / "esper-config.ini").read_bytes() == changed
    assert not (cache / "ESPER-Utau").exists()
    assert not (prepared.root / "releases" / setup.VERSION).exists()


def test_symlink_download_cache_cannot_redirect_installation(prepared, tmp_path):
    outside = tmp_path / "foreign"
    outside.mkdir()
    parent = prepared.root / "downloads"
    parent.mkdir(parents=True)
    (parent / setup.VERSION).symlink_to(outside, target_is_directory=True)
    report = install(prepared)
    assert not report["ok"] and not prepared.downloads and not prepared.commands
    assert not list(outside.iterdir())


def test_invalid_download_is_not_retained_as_a_reusable_asset(prepared):
    prepared.payloads["engine"] = b"wrong binary"
    report = install(prepared)
    assert not report["ok"]
    cache = prepared.root / "downloads" / setup.VERSION
    assert not (cache / "ESPER-Utau").exists()
    assert not list(cache.glob(".download-stage-*"))


def test_setup_accounts_for_download_stage_and_dotnet_space_before_network(prepared, monkeypatch):
    asset_bytes = sum(asset["bytes"] for asset in prepared.assets.values())
    expected_free = 2 * asset_bytes + 400 * 1024 * 1024
    monkeypatch.setattr(setup.shutil, "disk_usage", lambda path: SimpleNamespace(free=expected_free - 1))
    report = install(prepared)
    assert not report["ok"] and not prepared.downloads
    assert report["checks"]["free_space"]["required_bytes"] == expected_free
    assert not (prepared.root / "downloads" / setup.VERSION).exists()


def test_guest_report_only_prints_required_packages(prepared):
    prepared.installed.update({"large-unrelated-package", "another-package"})
    report = install(prepared)
    assert report["ok"]
    check = report["checks"]["guest_packages_before"]
    assert all(check["packages"].values())
    assert len(check["output"].splitlines()) == len(setup.REQUIRED_PACKAGES)
    assert "unrelated" not in check["output"] and "another-package" not in check["output"]


def known_prior_launcher(monkeypatch, version="v1"):
    path = Path(f"/workspace/library-files/teto-esper-termux-kit-{version}.zip")
    if not path.exists():
        # Keep the upgrade behavior covered in a normal checkout as well. The
        # shipped prior artifact, when available, exercises its exact real hash.
        source = b"#!/usr/bin/env python3\n# known previous release fixture\n"
        digest = hashlib.sha256(source).hexdigest()
        monkeypatch.setattr(setup, "KNOWN_LAUNCHER_SHA256", setup.KNOWN_LAUNCHER_SHA256 | {digest})
    else:
        with zipfile.ZipFile(path) as archive:
            source = archive.read("deploy/esper-utau/termux/resampler.py")
    assert hashlib.sha256(source).hexdigest() in setup.KNOWN_LAUNCHER_SHA256
    return source


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_known_launchers_are_backed_up_exactly_and_updated_after_probe(prepared, monkeypatch, version):
    previous = known_prior_launcher(monkeypatch, version)
    directory = prepared.root / "bin"
    directory.mkdir(parents=True)
    launchers = [directory / "resampler.py", directory / "esper-utau-resampler"]
    for path in launchers:
        path.write_bytes(previous)
    report = install(prepared)
    assert report["ok"] and report["runtime_changed"]
    assert len(report["launcher_updates"]) == 2
    for update in report["launcher_updates"]:
        path, backup = Path(update["path"]), Path(update["backup"])
        assert backup.read_bytes() == previous
        assert path.read_bytes() == (prepared.bundle / "resampler.py").read_bytes()
        assert path.stat().st_mode & 0o777 == 0o755
    # Already-current wrappers cause no new backup, replacement or download.
    snapshots = {path: path.stat().st_ino for path in launchers}
    retry = install(prepared)
    assert retry["ok"] and not retry["runtime_changed"] and not retry["launcher_updates"]
    assert {path: path.stat().st_ino for path in launchers} == snapshots
    assert prepared.downloads == ["engine", "config"]


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_failed_probe_never_upgrades_known_prior_launcher(prepared, monkeypatch, version):
    previous = known_prior_launcher(monkeypatch, version)
    launcher = prepared.root / "bin" / "resampler.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(previous)
    prepared.failure = "probe"
    report = install(prepared)
    assert not report["ok"]
    assert launcher.read_bytes() == previous
    assert not list(launcher.parent.glob("*.backup-*"))
    assert not (prepared.root / "releases" / setup.VERSION).exists()


def test_existing_foreign_backup_is_preserved_with_prior_launcher(prepared, monkeypatch):
    previous = known_prior_launcher(monkeypatch)
    launcher = prepared.root / "bin" / "resampler.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(previous)
    digest = hashlib.sha256(previous).hexdigest()
    backup = launcher.with_name(f"{launcher.name}.backup-{digest[:12]}")
    backup.write_bytes(b"operator-owned unrelated backup")
    report = install(prepared)
    assert not report["ok"] and "backup existente" in report["error"]
    assert launcher.read_bytes() == previous
    assert backup.read_bytes() == b"operator-owned unrelated backup"


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_previously_published_runtime_reuses_assets_and_upgrades_known_launcher(prepared, monkeypatch, version):
    assert install(prepared)["ok"]
    release = prepared.root / "releases" / setup.VERSION
    engine = release / "ESPER-Utau"
    before = engine.stat()
    previous = known_prior_launcher(monkeypatch, version)
    launcher = prepared.root / "bin" / "resampler.py"
    launcher.write_bytes(previous)
    monkeypatch.delattr(setup.os, "link", raising=False)
    report = install(prepared)
    assert report["ok"] and report["runtime_changed"]
    assert len(report["launcher_updates"]) == 1
    assert Path(report["launcher_updates"][0]["backup"]).read_bytes() == previous
    assert launcher.read_bytes() == (prepared.bundle / "resampler.py").read_bytes()
    assert engine.stat().st_ino == before.st_ino
    assert engine.stat().st_mtime_ns == before.st_mtime_ns
    assert prepared.downloads == ["engine", "config"]
    assert prepared.probes == 2


@pytest.mark.parametrize("asset", ["engine", "config"])
def test_known_gc_wrapper_upgrade_still_checks_installed_asset_pins(prepared, monkeypatch, asset):
    assert install(prepared)["ok"]
    launcher = prepared.root / "bin/resampler.py"
    previous = known_prior_launcher(monkeypatch, "v2")
    launcher.write_bytes(previous)
    path = prepared.root / "releases" / setup.VERSION / setup.ASSETS[asset]["name"]
    changed = path.read_bytes() + b"local modification"
    path.write_bytes(changed)
    report = install(prepared)
    assert not report["ok"] and "SHA-256 divergente" in report["error"]
    assert launcher.read_bytes() == previous and path.read_bytes() == changed
    assert not list(launcher.parent.glob("*.backup-*"))
    assert prepared.downloads == ["engine", "config"] and prepared.probes == 1
