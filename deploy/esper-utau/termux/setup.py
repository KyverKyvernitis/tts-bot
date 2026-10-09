#!/usr/bin/env python3
"""Install and synthesize-probe the pinned ESPER release in an existing ARM64 guest."""
from __future__ import annotations

import argparse
import array
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

VERSION = "v2.5.0"
SOURCE_COMMIT = "2ff1a4692fa88fb1b60933408125632d0c5d8356"
DEFAULT_ROOT = Path.home() / ".esper-utau"
DEFAULT_CONTAINER = "voicepeak-arm64"
TOOLKIT_ROOT = Path(__file__).resolve().parent
REQUIRED_PACKAGES = ("libc6", "libstdc++6", "libgcc-s1", "zlib1g")
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
DOTNET_FREE_SPACE_RESERVE = 400 * 1024 * 1024
# Exact source shipped in kits v1 and v2/v3. Other local launchers belong to
# the operator and must never be replaced by an installation retry.
KNOWN_LAUNCHER_SHA256 = frozenset({
    "da55173230453b3e06c74416c88bdec73a94ae86e8afda320a2b7f12b04305e1",
    "072c37648c164db924add41a56dd234fe97080703a5317cbe96cc9f86bb912c5",
})
ASSETS = {
    "engine": {
        "name": "ESPER-Utau", "bytes": 107335629,
        "sha256": "e2cb6dc113593cb3b788debb51996f591a5f9d47bbd2a54df57bc1ecd843602f",
        "url": "https://github.com/CdrSonan/ESPER-Utau/releases/download/v2.5.0/ESPER-Utau-Linux-arm64",
    },
    "config": {
        "name": "esper-config.ini", "bytes": 762,
        "sha256": "21951154b9bafdebde0b3a1d6533bb1fde549113b63b99a573b49ea68fadbe83",
        "url": "https://github.com/CdrSonan/ESPER-Utau/releases/download/v2.5.0/esper-config.ini",
    },
}
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
SIGNAL_MESSAGE = re.compile(r"(?:terminated with signal|uncaught target signal|segmentation fault|core dumped)", re.I)


class SetupError(ValueError):
    pass


def valid_timeout(value: str | float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("timeout deve ser um número entre 10 e 300 segundos") from exc
    if not math.isfinite(number) or not 10 <= number <= 300:
        raise argparse.ArgumentTypeError("timeout deve estar entre 10 e 300 segundos")
    return number


def valid_container(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(value)):
        raise SetupError("nome do container inválido")
    return str(value)


def real_directories(path: Path) -> None:
    for directory in (*reversed(path.parents), path):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise SetupError("pasta do runtime ou de cache é um link ou não é diretório real")


def regular_digest(path: Path, maximum: int = MAX_DOWNLOAD_BYTES) -> tuple[int, str, bytes]:
    original = path.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise SetupError("arquivo existente não é regular ou é link simbólico")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum
                or (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino)):
            raise SetupError("arquivo não regular, vazio ou acima do limite permitido")
        digest = hashlib.sha256()
        header = stream.read(64)
        digest.update(header)
        total = len(header)
        while True:
            block = stream.read(65536)
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise SetupError("arquivo cresceu acima do limite permitido")
            digest.update(block)
    return total, digest.hexdigest(), header


def verify_asset(path: Path, asset: str) -> dict:
    expected = ASSETS[asset]
    size, digest, header = regular_digest(path)
    if size != expected["bytes"] or digest != expected["sha256"]:
        raise SetupError(f"{expected['name']}: tamanho/SHA-256 divergente; arquivo existente foi preservado")
    if asset == "engine" and (header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00"):
        raise SetupError("executável não é ELF64 Linux ARM64")
    return {"ok": True, "bytes": size, "sha256": digest, "elf_arm64": asset == "engine"}


def checked_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS
            or parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}):
        raise SetupError("redirecionamento de download não autorizado")
    return url


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return super().redirect_request(request, response, code, message, headers, checked_url(newurl))


def stream_download(asset: str, output: Path, timeout: float) -> None:
    expected = ASSETS[asset]
    request = urllib.request.Request(checked_url(expected["url"]), headers={
        "User-Agent": "ESPER-Utau-Termux-kit/1", "Accept-Encoding": "identity",
    })
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(OfficialRedirects())
    with opener.open(request, timeout=min(20, timeout)) as response:
        checked_url(response.geturl())
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) != expected["bytes"]):
            raise SetupError("servidor informou tamanho diferente do release fixado")
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "wb") as destination:
            while True:
                if time.monotonic() >= deadline:
                    raise SetupError("download excedeu o tempo permitido")
                block = response.read(65536)
                if not block:
                    break
                total += len(block)
                if total > min(MAX_DOWNLOAD_BYTES, expected["bytes"]):
                    raise SetupError("download excedeu o tamanho do release fixado")
                destination.write(block)
                digest.update(block)
            destination.flush()
            os.fsync(destination.fileno())
    if total != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        raise SetupError("download incompleto ou SHA-256 diferente do release oficial fixado")


def run_process(command: list[str], timeout: float, *, environment: dict | None = None) -> dict:
    # Capture on disk so unexpected guest output cannot exhaust the host RAM.
    with tempfile.TemporaryFile() as capture:
        proc = subprocess.Popen(command, stdout=capture, stderr=capture, env=environment, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, BaseException) as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                return {"ok": False, "code": None, "error": "comando excedeu o tempo permitido"}
            raise
        capture.seek(0)
        raw = capture.read(65537)
    if len(raw) > 65536:
        return {"ok": False, "code": code, "error": "saída do comando excedeu 64 KiB"}
    output = raw.decode("utf-8", errors="replace")
    signal_failure = bool(SIGNAL_MESSAGE.search(output))
    return {"ok": code == 0 and not signal_failure, "code": code, "output": output,
            **({"error": "guest reportou falha por sinal"} if signal_failure else {})}


def download_asset(asset: str, output: Path, timeout: float) -> None:
    result = run_process([sys.executable, str(Path(__file__).resolve()), "--_download", asset,
                          "--_output", str(output), "--download-timeout", str(timeout)], timeout + 2)
    if not result["ok"]:
        raise SetupError(f"download de {ASSETS[asset]['name']} falhou; verifique a conexão e tente novamente")
    verify_asset(output, asset)


def guest_command(container: str, arguments: list[str]) -> list[str]:
    return ["proot-distro", "login", valid_container(container), "--", "/usr/bin/env", "LANG=C", "LC_ALL=C", *arguments]


def checked_guest(report: dict, name: str, container: str, arguments: list[str], timeout: float = 20) -> dict:
    result = run_process(guest_command(container, arguments), timeout)
    report["checks"][name] = result
    if not result["ok"]:
        raise SetupError(f"etapa {name} falhou; repare o guest antes de continuar")
    return result


def packages(report: dict, container: str, label: str) -> dict[str, bool]:
    result = checked_guest(report, label, container,
                           ["/usr/bin/dpkg-query", "-W", "-f=${Package}\t${Status}\\n"])
    inventory = {name: False for name in REQUIRED_PACKAGES}
    relevant_lines = []
    for line in result.get("output", "").splitlines():
        name, _, state = line.partition("\t")
        name = name.split(":", 1)[0]
        if name in inventory:
            inventory[name] = state.strip() == "install ok installed"
            relevant_lines.append(f"{name}\t{state.strip()}\n")
    # dpkg-query still checks the complete inventory; the JSON only needs the
    # four dependencies instead of hundreds of unrelated package names.
    report["checks"][label]["output"] = "".join(relevant_lines)
    report["checks"][label]["packages"] = inventory
    return inventory


def write_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def wav_evidence(path: Path) -> dict:
    # A successful process alone does not prove that the engine synthesized.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 44 < info.st_size <= 128 * 1024:
        raise SetupError("probe não produziu WAV regular dentro do limite permitido")
    with wave.open(str(path), "rb") as wav:
        sample_rate, channels, width, frames = wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()
        if (sample_rate, channels, width, wav.getcomptype()) != (44100, 1, 2, "NONE"):
            raise SetupError("probe não produziu PCM mono 44.1 kHz/16 bits")
        if not 0.08 <= frames / sample_rate <= 0.35:
            raise SetupError("duração do WAV sintético fora do intervalo esperado")
        raw = wav.readframes(frames)
        if len(raw) != frames * 2:
            raise SetupError("WAV sintético truncado")
    samples = array.array("h", raw)
    peak = max((abs(sample) for sample in samples), default=0)
    if not peak:
        raise SetupError("probe retornou apenas silêncio")
    return {"synthetic_render_verified": True, "sample_rate": sample_rate, "channels": channels,
            "sample_width_bytes": width, "frames": frames, "duration_seconds": frames / sample_rate, "peak": peak}


def probe_runtime(root: Path, container: str, cache_dir: Path) -> dict:
    wrapper = TOOLKIT_ROOT / "resampler.py"
    if not wrapper.is_file():
        raise SetupError("resampler.py ausente no kit; extraia o ZIP completo")
    real_directories(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="esper-setup-probe-", dir=cache_dir) as temporary:
        work = Path(temporary)
        source_dir, output_dir = work / "input", work / "output"
        source_dir.mkdir(); output_dir.mkdir()
        source, output = source_dir / "synthetic.wav", output_dir / "probe.wav"
        waveform = array.array("h", (int(7000 * math.sin(2 * math.pi * 261.625565 * i / 44100)
                                        + 2000 * math.sin(4 * math.pi * 261.625565 * i / 44100))
                                     for i in range(22050)))
        if sys.byteorder != "little":
            waveform.byteswap()
        with wave.open(str(source), "wb") as wav:
            wav.setparams((1, 2, 44100, 0, "NONE", "not compressed")); wav.writeframes(waveform.tobytes())
        environment = dict(os.environ)
        environment.update({"PHONE_WORKER_ESPER_ROOT": str(root), "PHONE_WORKER_ESPER_CONTAINER": container,
                            "PHONE_WORKER_ESPER_CACHE_DIR": str(work / "analysis"), "PHONE_WORKER_ESPER_TIMEOUT": "60"})
        command = [sys.executable, str(wrapper), str(source), str(output), "C4", "100", "", "0", "240", "30",
                   "-400", "100", "0", "!140", "AA#100#"]
        result = run_process(command, 65, environment=environment)
        if not result["ok"]:
            detail = str(result.get("output") or result.get("error") or "sem diagnóstico")[-2000:]
            raise SetupError(f"ESPER não confirmou renderização sintética ARM64: {detail}")
        evidence = wav_evidence(output)
        return {"ok": True, "native_architecture": "arm64", **evidence}


def release_descriptor() -> dict:
    return {"version": VERSION, "source_commit": SOURCE_COMMIT, "native_architecture": "arm64",
            "assets": ASSETS, "license": "MIT", "source_url": "https://github.com/CdrSonan/ESPER-Utau"}


def cache_downloads(directory: Path, timeout: float, report: dict) -> None:
    """Keep independently verified assets even if a subsequent probe fails."""
    real_directories(directory)
    directory.mkdir(parents=True, exist_ok=True)
    reused = []
    # Refuse a changed cache before downloading any other asset. Existing files
    # are preserved rather than silently overwritten with the official release.
    for asset, expected in ASSETS.items():
        target = directory / expected["name"]
        if target.exists() or target.is_symlink():
            report["checks"][asset + "_pin"] = verify_asset(target, asset)
            reused.append(asset)
    deadline = time.monotonic() + timeout
    for asset, expected in ASSETS.items():
        if asset in reused:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SetupError("download excedeu o tempo total permitido")
        target = directory / expected["name"]
        with tempfile.TemporaryDirectory(prefix=".download-stage-", dir=directory) as temporary:
            staged = Path(temporary) / expected["name"]
            download_asset(asset, staged, remaining)
            evidence = verify_asset(staged, asset)
            if target.exists() or target.is_symlink():
                raise SetupError("cache mudou durante o download; arquivo existente preservado")
            staged.chmod(0o600)
            staged.rename(target)
            report["checks"][asset + "_pin"] = evidence
    report["checks"]["download"] = {
        "ok": True, "skipped": len(reused) == len(ASSETS),
        "cached_download": bool(reused), "cached_assets": reused,
        "cache_directory": str(directory),
    }


def checked_launcher(path: Path, wrapper_digest: str) -> str | None:
    if not (path.exists() or path.is_symlink()):
        return None
    _, digest, _ = regular_digest(path, 1024 * 1024)
    if digest != wrapper_digest and digest not in KNOWN_LAUNCHER_SHA256:
        raise SetupError("launcher existente é diferente; foi preservado")
    return digest


def atomic_launcher(path: Path, data: bytes, previous_digest: str | None) -> Path | None:
    """Publish current code, backing up an unchanged, known prior launcher."""
    digest = hashlib.sha256(data).hexdigest()
    current_digest = checked_launcher(path, digest)
    if current_digest != previous_digest:
        raise SetupError("launcher mudou durante instalação; arquivo existente preservado")
    if current_digest == digest:
        return None
    backup = None
    if current_digest is not None:
        previous = path.read_bytes()
        if hashlib.sha256(previous).hexdigest() != current_digest:
            raise SetupError("launcher mudou antes do backup; arquivo existente preservado")
        backup = path.with_name(f"{path.name}.backup-{current_digest[:12]}")
        if backup.exists() or backup.is_symlink():
            _, backup_digest, _ = regular_digest(backup, 1024 * 1024)
            if backup_digest != current_digest:
                raise SetupError("backup existente é diferente; arquivo e launcher foram preservados")
        else:
            descriptor, filename = tempfile.mkstemp(prefix=".esper-launcher-backup-", dir=path.parent)
            temporary = Path(filename)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(previous); output.flush(); os.fsync(output.fileno())
                if backup.exists() or backup.is_symlink():
                    raise SetupError("backup mudou durante instalação; arquivo existente preservado")
                temporary.rename(backup)
            finally:
                temporary.unlink(missing_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".esper-launcher-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        temporary.chmod(0o755)
        if checked_launcher(path, digest) != previous_digest:
            raise SetupError("launcher mudou antes da publicação; arquivo existente preservado")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def install_runtime(*, root: Path | None = None, container: str = DEFAULT_CONTAINER, download_timeout: float = 180) -> dict:
    report = {"ok": False, "runtime_ready": False, "runtime_changed": False, "version": VERSION,
              "native_architecture": platform.machine().lower(), "container": container, "checks": {},
              "packages_installed": [], "worker_configuration_changed": False, "production_backend_changed": False,
              "teto_synthesis_verified": False, "portuguese_speech_verified": False,
              "portuguese_quality_verified": False, "box64_required": False, "voicepeak_required": False,
              "note": "Verifica ESPER com áudio sintético. Não seleciona o motor no bot nem comprova a fala da Teto."}
    lock = None
    lock_path = None
    try:
        container = valid_container(container)
        download_timeout = valid_timeout(download_timeout)
        destination = Path(os.path.abspath(Path(root or DEFAULT_ROOT).expanduser()))
        release = destination / "releases" / VERSION
        cache = destination / "cache"
        downloads = destination / "downloads" / VERSION
        launcher = destination / "bin" / "resampler.py"
        convenience_launcher = launcher.with_name("esper-utau-resampler")
        real_directories(destination)
        real_directories(release)
        real_directories(launcher.parent)
        real_directories(cache)
        real_directories(downloads)
        report["root"] = str(destination)
        if report["native_architecture"] not in {"aarch64", "arm64"}:
            raise SetupError("execute este setup no Termux ARM64 do Poco")
        if sys.version_info < (3, 10):
            raise SetupError("Python 3.10 ou superior é necessário")
        for executable in ("python", "ffmpeg", "proot-distro"):
            present = bool(shutil.which(executable))
            report["checks"]["host_" + executable.replace("-", "_")] = {"ok": present}
            if not present:
                raise SetupError(f"{executable} ausente no Termux; instale antes de continuar")
        wrapper = TOOLKIT_ROOT / "resampler.py"
        wrapper_bytes = wrapper.read_bytes()
        wrapper_digest = hashlib.sha256(wrapper_bytes).hexdigest()
        license_bytes = (TOOLKIT_ROOT.parent / "LICENSE.txt").read_bytes()
        prior_launchers = {path: checked_launcher(path, wrapper_digest)
                           for path in (launcher, convenience_launcher)}
        existing = release.exists()
        if existing:
            report["checks"]["engine_pin"] = verify_asset(release / ASSETS["engine"]["name"], "engine")
            report["checks"]["config_pin"] = verify_asset(release / ASSETS["config"]["name"], "config")
        architecture = checked_guest(report, "guest_architecture", container, ["/usr/bin/dpkg", "--print-architecture"])
        if architecture.get("output", "").strip() != "arm64":
            raise SetupError("guest deve ser Linux ARM64 instalado; não use guest x86_64/QEMU")
        inventory = packages(report, container, "guest_packages_before")
        if not inventory["libc6"]:
            raise SetupError("libc6 não está configurada no guest; repare o guest antes de continuar")
        missing = [name for name in REQUIRED_PACKAGES if not inventory[name]]
        if missing:
            checked_guest(report, "apt_update", container, ["/usr/bin/apt-get", "update"], 60)
            checked_guest(report, "apt_install", container,
                          ["/usr/bin/apt-get", "install", "-y", "--no-install-recommends", *missing], 60)
            report["packages_installed"] = missing
            if not all(packages(report, container, "guest_packages_after").values()):
                raise SetupError("pacotes do guest continuam ausentes ou não configurados")
        destination.mkdir(parents=True, exist_ok=True)
        lock_path = destination / ".setup.lock"
        lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.write(lock, str(os.getpid()).encode("ascii"))
        release.parent.mkdir(parents=True, exist_ok=True)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        if existing:
            report["checks"]["download"] = {"ok": True, "skipped": True, "cached_download": False}
            report["checks"]["native_probe"] = probe_runtime(destination, container, cache)
        else:
            if release.exists() or release.is_symlink():
                raise SetupError("destino mudou durante a instalação; execute novamente")
            cached_assets = set()
            for asset, expected in ASSETS.items():
                path = downloads / expected["name"]
                if path.exists() or path.is_symlink():
                    report["checks"][asset + "_pin"] = verify_asset(path, asset)
                    cached_assets.add(asset)
            runtime_bytes = sum(asset["bytes"] for asset in ASSETS.values())
            download_bytes = sum(expected["bytes"] for asset, expected in ASSETS.items()
                                 if asset not in cached_assets)
            required_free = runtime_bytes + download_bytes + DOTNET_FREE_SPACE_RESERVE
            report["checks"]["free_space"] = {"ok": shutil.disk_usage(destination).free >= required_free,
                                                "required_bytes": required_free,
                                                "dotnet_reserve_bytes": DOTNET_FREE_SPACE_RESERVE}
            if not report["checks"]["free_space"]["ok"]:
                raise SetupError("espaço livre insuficiente para o cache, a cópia do runtime e 400 MiB do .NET")
            cache_downloads(downloads, download_timeout, report)
            with tempfile.TemporaryDirectory(prefix=".esper-stage-", dir=destination) as temporary:
                stage = Path(temporary)
                staged_release = stage / "releases" / VERSION
                staged_release.mkdir(parents=True)
                for asset, expected in ASSETS.items():
                    output = staged_release / expected["name"]
                    shutil.copyfile(downloads / expected["name"], output)
                    report["checks"][asset + "_pin"] = verify_asset(output, asset)
                    output.chmod(0o755 if asset == "engine" else 0o600)
                write_file(staged_release / "release.json", (json.dumps(release_descriptor(), indent=2) + "\n").encode())
                write_file(staged_release / "LICENSE.txt", license_bytes)
                report["checks"]["native_probe"] = probe_runtime(stage, container, cache)
                if release.exists() or release.is_symlink():
                    raise SetupError("destino mudou antes da publicação; nada foi substituído")
                staged_release.rename(release)
                report["runtime_changed"] = True
        report["launcher_updates"] = []
        for target_launcher, previous_digest in prior_launchers.items():
            backup = atomic_launcher(target_launcher, wrapper_bytes, previous_digest)
            if backup is not None:
                report["runtime_changed"] = True
                report["launcher_updates"].append({"path": str(target_launcher), "backup": str(backup),
                                                   "previous_sha256": previous_digest})
        report.update({"ok": True, "runtime_ready": True, "launcher": str(convenience_launcher),
                       "resampler_script": str(launcher), "release": str(release),
                       "publication": {"ok": True, "atomic_release": True}})
    except (SetupError, argparse.ArgumentTypeError, OSError, ValueError, wave.Error) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if lock is not None:
            os.close(lock)
            if lock_path is not None:
                lock_path.unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--download-timeout", type=valid_timeout, default=180)
    parser.add_argument("--_download", choices=tuple(ASSETS), help=argparse.SUPPRESS)
    parser.add_argument("--_output", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options._download:
        try:
            if options._output is None:
                raise SetupError("destino interno de download ausente")
            stream_download(options._download, options._output, options.download_timeout)
        except (SetupError, urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            print(f"Download não foi verificado: {type(exc).__name__}", file=sys.stderr)
            return 2
        return 0
    report = install_runtime(root=options.root, container=options.container, download_timeout=options.download_timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
