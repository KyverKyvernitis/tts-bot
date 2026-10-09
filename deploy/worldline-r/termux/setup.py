#!/usr/bin/env python3
"""Prepare only the pinned WORLDLINE-R ARM64 runtime in an existing Termux guest."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile
import time
import types
import urllib.error
import urllib.parse
import urllib.request

TOOLKIT_ROOT = Path(__file__).resolve().parent
DEFAULT_LIBRARY_DIR = Path.home() / ".worldline-r" / "lib"
MAX_LIBRARY_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30.0
INSTALLABLE_PACKAGES = ("python3", "libstdc++6", "libgcc-s1")


def load_doctor():
    # Loading bundled Python source performs no native calls and writes no pycache.
    path = TOOLKIT_ROOT / "diagnostic.py"
    module = types.ModuleType("_worldline_setup_doctor")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


doctor = load_doctor()


class SetupError(ValueError):
    pass


def check_directory(path: Path) -> None:
    """Reject the install directory and immediate parent if redirected by symlinks."""
    for candidate in (path.parent, path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise SetupError("a pasta da biblioteca e sua pasta pai devem ser diretórios reais, sem links simbólicos")


def regular_info(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LIBRARY_BYTES:
        raise SetupError("biblioteca existente não é arquivo regular ou excede 8 MiB; preserve-a em outro local antes de continuar")
    return info


def read_regular(path: Path, maximum: int = MAX_LIBRARY_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise SetupError("arquivo não regular ou acima do limite permitido")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise SetupError("arquivo cresceu acima do limite permitido")
    return data


def verify_library(data: bytes, runtime) -> dict:
    if not 64 <= len(data) <= MAX_LIBRARY_BYTES:
        raise SetupError("download da biblioteca tem tamanho inválido")
    if data[:6] != b"\x7fELF\x02\x01" or data[16:20] != b"\x03\x00\xb7\x00":
        raise SetupError("download não é uma biblioteca ELF64 Linux ARM64")
    digest = hashlib.sha256(data).hexdigest()
    if digest != runtime.RELEASE_SHA256:
        raise SetupError("SHA-256 do download diverge da versão oficial fixada; nada foi instalado")
    return {"ok": True, "elf_arm64": True, "sha256": digest, "bytes": len(data)}


def download_library(runtime) -> bytes:
    url = runtime.RELEASE_URL
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
        raise SetupError("URL oficial fixada da biblioteca é inválida")
    request = urllib.request.Request(url, headers={"User-Agent": "WORLDLINE-R-Termux-kit/1", "Accept-Encoding": "identity"})
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "raw.githubusercontent.com":
                raise SetupError("download redirecionado para destino não autorizado")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_LIBRARY_BYTES):
                raise SetupError("download excede 8 MiB ou informa tamanho inválido")
            output = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise SetupError("download excedeu 30 segundos; tente novamente com conexão estável")
                block = response.read(min(65536, MAX_LIBRARY_BYTES + 1 - len(output)))
                if not block:
                    break
                output.extend(block)
                if len(output) > MAX_LIBRARY_BYTES:
                    raise SetupError("download excede o limite de 8 MiB")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SetupError("não foi possível baixar a biblioteca oficial por HTTPS; verifique a conexão e tente novamente") from exc
    return bytes(output)


def checked_guest(report: dict, name: str, container: str, arguments: list[str], *, timeout: float = 20,
                  library: Path | None = None) -> dict:
    result = doctor.bounded(doctor.guest_command(container, arguments, library=library), timeout=timeout)
    report["checks"][name] = doctor.outcome(result)
    if not result.get("ok"):
        raise SetupError(f"etapa {name} falhou; consulte checks e corrija o guest antes de tentar novamente")
    return result


def guest_packages(report: dict, container: str, name: str) -> dict[str, bool]:
    # Enumerating installed packages succeeds even when a required package is absent.
    result = checked_guest(report, name, container,
                           ["/usr/bin/dpkg-query", "-W", "-f=${Package}\t${Status}\\n"])
    packages = doctor.package_inventory(result)
    report["checks"][name]["packages"] = packages
    return packages


def validate_runtime_probe(result: dict, runtime) -> dict:
    try:
        data = json.loads(result.get("output", ""))
    except (TypeError, ValueError) as exc:
        raise SetupError("probe nativo não retornou JSON válido; biblioteca anterior foi preservada") from exc
    if not isinstance(data, dict):
        raise SetupError("probe nativo não retornou um objeto JSON")
    booleans = ("runtime_ok", "api_verified", "abi_verified", "library_hash_verified", "synthetic_render_verified")
    if (any(data.get(name) is not True for name in booleans)
            or data.get("native_architecture") != "arm64"
            or data.get("source_commit") != runtime.SOURCE_COMMIT
            or data.get("source_version") != runtime.SOURCE_VERSION
            or data.get("library_sha256") != runtime.RELEASE_SHA256):
        raise SetupError("probe não confirmou API, ABI e render sintético ARM64 da versão fixada; biblioteca anterior foi preservada")
    return {name: data[name] for name in (*booleans, "native_architecture", "source_commit", "source_version", "library_sha256")}


def durable_write(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def backup_existing(path: Path, expected_data: bytes) -> Path:
    # Re-read before publishing, so a concurrent edit cannot silently lose data.
    if read_regular(path) != expected_data:
        raise SetupError("biblioteca existente mudou durante a instalação; repita após encerrar outros instaladores")
    descriptor, filename = tempfile.mkstemp(prefix=f"{path.name}.backup-", dir=path.parent)
    backup = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(expected_data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


def install_runtime(*, container: str = doctor.DEFAULT_CONTAINER, library_dir: Path | None = None,
                    probe_timeout: float = 60.0) -> dict:
    report = {
        "runtime_ready": False, "tts_ready": False, "phrase_adapter_available": False,
        "teto_synthesis_verified": False, "portuguese_speech_verified": False,
        "box64_required": False, "voicepeak_required": False, "container": container,
        "checks": {}, "packages_installed": [], "library_changed": False,
        "note": "Instala somente WORLDLINE-R. O adaptador de frases ainda falta; este teste não usa voicebank nem comprova fala da Teto.",
    }
    try:
        container = doctor.validate_container(container)
        destination = doctor.safe_path(library_dir or DEFAULT_LIBRARY_DIR)
        library = destination / "libworldline.so"
        check_directory(destination)
        existing_info = regular_info(library)
        old_data = read_regular(library) if existing_info is not None else None
        report["library"] = str(library)
        architecture = platform.machine().lower()
        report["native_architecture"] = architecture
        report["checks"]["host_arm64"] = {"ok": architecture in {"aarch64", "arm64"}}
        if architecture not in {"aarch64", "arm64"}:
            raise SetupError("execute este setup no Termux ARM64 do Poco; host atual não é ARM64")
        if sys.version_info < (3, 10):
            raise SetupError("Python 3.10 ou superior é necessário no Termux")
        proot = shutil.which("proot-distro")
        report["checks"]["proot_distro"] = {"ok": bool(proot)}
        if not proot:
            raise SetupError("proot-distro ausente; instale-o no Termux e use um guest Linux ARM64 existente")
        if not math.isfinite(probe_timeout) or not 1 <= probe_timeout <= 120:
            raise SetupError("timeout do probe deve estar entre 1 e 120 segundos")
        result = checked_guest(report, "guest_architecture", container, ["/usr/bin/dpkg", "--print-architecture"])
        guest_architecture = result.get("output", "").strip()
        report["guest_architecture"] = guest_architecture if guest_architecture in {"arm64", "amd64", "armhf", "i386"} else None
        if guest_architecture != "arm64":
            raise SetupError("o container selecionado deve ser Linux ARM64 nativo; não use o antigo guest x86_64/QEMU")
        packages = guest_packages(report, container, "packages_before")
        if not packages.get("libc6"):
            raise SetupError("libc6 do guest não está instalada/configurada; repare o guest antes de instalar WORLDLINE-R")
        missing = [name for name in INSTALLABLE_PACKAGES if not packages.get(name)]
        if missing:
            environment = ["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get"]
            checked_guest(report, "apt_update", container, [*environment, "update"], timeout=180)
            checked_guest(report, "apt_install", container,
                          [*environment, "install", "-y", "--no-install-recommends", *missing], timeout=300)
            report["packages_installed"] = missing
            packages = guest_packages(report, container, "packages_after")
        if not all(packages.values()):
            raise SetupError("guest continua sem os quatro pacotes necessários; instalação da biblioteca cancelada")
        runtime = doctor.load_runtime_module()
        report.update({"source_commit": runtime.SOURCE_COMMIT, "source_version": runtime.SOURCE_VERSION,
                       "download_url": runtime.RELEASE_URL, "license_source": runtime.LICENSE_URL})
        license_data = read_regular(TOOLKIT_ROOT.parent / "LICENSE.openutau.txt", maximum=65536)
        if not license_data.startswith(b"The MIT License (MIT)"):
            raise SetupError("aviso MIT empacotado está ausente ou inválido; extraia novamente o kit completo")
        notice = destination / "LICENSE.openutau.txt"
        if notice.exists() or notice.is_symlink():
            if read_regular(notice, maximum=65536) != license_data:
                raise SetupError("aviso de licença existente diverge do kit; preserve-o em outro local antes de continuar")
        check_directory(destination)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        check_directory(destination)
        matching = False
        if old_data is not None:
            try:
                verify_library(old_data, runtime)
                matching = True
            except SetupError:
                pass
        with tempfile.TemporaryDirectory(prefix=".worldline-stage-", dir=destination) as temporary:
            stage = Path(temporary)
            if matching:
                data = old_data
                candidate = library
                report["checks"]["download"] = {"ok": True, "skipped": True, "reason": "biblioteca fixada já presente"}
            else:
                data = download_library(runtime)
                report["checks"]["download"] = {"ok": True, "skipped": False}
                # No ctypes call, nor publication, happens before this pin/ELF check.
                report["checks"]["library_pin"] = verify_library(data, runtime)
                candidate = stage / "libworldline.so"
                durable_write(candidate, data)
            report["checks"]["library_pin"] = verify_library(data, runtime)
            result = checked_guest(report, "native_probe", container,
                                   ["/usr/bin/python3", "/opt/worldline-r-kit/runtime-probe.py", "--library",
                                    f"/opt/worldline-r-lib/{candidate.name}", "--render-probe", "--timeout", str(probe_timeout)],
                                   timeout=probe_timeout + 10, library=candidate)
            report["checks"]["native_probe"].update(validate_runtime_probe(result, runtime))
            check_directory(destination)
            current = read_regular(library) if regular_info(library) is not None else None
            if current != old_data:
                raise SetupError("biblioteca existente mudou durante o teste; nenhum arquivo foi substituído")
            # Stage the notice too. Android Python may have no os.link, so only os.replace is used.
            if not notice.exists():
                stage_notice = stage / "LICENSE.openutau.txt"
                durable_write(stage_notice, license_data)
                if notice.is_symlink() or notice.exists():
                    raise SetupError("aviso de licença mudou durante a instalação; tente novamente")
                os.replace(stage_notice, notice)
            if not matching:
                if old_data is not None:
                    backup = backup_existing(library, old_data)
                    report["backup"] = str(backup)
                os.replace(candidate, library)
                report["library_changed"] = True
            report["checks"]["publication"] = {"ok": True, "atomic": True, "previous_library_preserved": old_data is not None}
        report["runtime_ready"] = True
        report["synthetic_render_verified"] = True
    except (OSError, ValueError) as exc:
        report["error"] = str(exc)
    return report


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Instala a biblioteca WORLDLINE-R ARM64 fixada e verifica sua API e um tom sintético.")
    parser.add_argument("--container", default=doctor.DEFAULT_CONTAINER, help="guest Linux ARM64 existente (padrão: voicepeak-arm64)")
    parser.add_argument("--library-dir", type=Path, help="pasta para biblioteca e aviso MIT (padrão: ~/.worldline-r/lib)")
    parser.add_argument("--probe-timeout", type=float, default=60.0, help="limite do teste nativo em segundos, entre 1 e 120")
    options = parser.parse_args(arguments)
    report = install_runtime(container=options.container, library_dir=options.library_dir, probe_timeout=options.probe_timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["runtime_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
