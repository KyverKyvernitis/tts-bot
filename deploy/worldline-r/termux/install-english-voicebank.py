#!/usr/bin/env python3
"""Install the pinned official Teto English CVVC bank without selecting it."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

SOURCE_URL = "https://kasaneteto.jp/assets/download/utau/TETO-English-150401.zip"
TERMS_URL = "https://kasaneteto.jp/guidelines/voice.html"
SOURCE_SHA256 = "addb3ab9dbe3dce7cb40fe6ba0c93ab5814d02acf5b091d0a762de370b813d6e"
SOURCE_BYTES = 83775797
EXPECTED_UNPACKED_BYTES = 94153463
DEFAULT_DIRECTORY = Path.home() / "voicebanks" / "kasane-teto-english"
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 10000
MINIMUM_ALIASES = 500
TOOLKIT_ROOT = Path(__file__).resolve().parent


class InstallError(ValueError):
    pass


def official_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "kasaneteto.jp"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password)


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if not official_url(url):
            raise InstallError("redirecionamento fora do site oficial HTTPS; download cancelado")
        return super().redirect_request(request, response, code, message, headers, url)


def timeout_value(value: float) -> float:
    if not math.isfinite(value) or not 1 <= value <= 120:
        raise InstallError("timeout do download deve estar entre 1 e 120 segundos")
    return value


def stream_download(output: Path, timeout: float) -> dict:
    """Runs in a child whose parent enforces the whole download deadline."""
    timeout = timeout_value(timeout)
    if not official_url(SOURCE_URL):
        raise InstallError("URL oficial fixada é inválida")
    request = urllib.request.Request(SOURCE_URL, headers={
        "User-Agent": "Teto-English-Termux-kit/1", "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(OfficialRedirects())
    deadline = time.monotonic() + timeout
    digest = hashlib.sha256()
    count = 0
    with opener.open(request, timeout=min(timeout, 15.0)) as response:
        if not official_url(response.geturl()):
            raise InstallError("download redirecionado para destino não autorizado")
        size = response.headers.get("Content-Length")
        if size is not None and (not size.isdigit() or int(size) != SOURCE_BYTES):
            raise InstallError("tamanho do arquivo oficial mudou; nada foi instalado")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            while True:
                if time.monotonic() >= deadline:
                    raise InstallError("download excedeu o timeout; tente novamente com conexão estável")
                block = response.read(65536)
                if not block:
                    break
                count += len(block)
                if count > MAX_DOWNLOAD_BYTES or count > SOURCE_BYTES:
                    raise InstallError("download excede o tamanho oficial ou o limite de 128 MiB")
                digest.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
    if count != SOURCE_BYTES or digest.hexdigest() != SOURCE_SHA256:
        raise InstallError("tamanho ou SHA-256 diverge do arquivo oficial fixado; nada foi instalado")
    return {"bytes": count, "sha256": digest.hexdigest()}


def download_archive(output: Path, timeout: float) -> None:
    timeout = timeout_value(timeout)
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--_download", str(output),
               "--timeout", str(timeout)]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("download excedeu o timeout; nada foi instalado") from exc
    if completed.returncode != 0 or len(completed.stdout) > 4096 or len(completed.stderr) > 4096:
        raise InstallError("não foi possível baixar e confirmar a bank oficial; tente novamente com conexão estável")


def archive_identity(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    count = 0
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != SOURCE_BYTES or info.st_size > MAX_DOWNLOAD_BYTES:
            raise InstallError("ZIP não é arquivo regular com o tamanho oficial fixado")
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            count += len(block)
            if count > MAX_DOWNLOAD_BYTES:
                raise InstallError("ZIP acima do limite de 128 MiB")
            digest.update(block)
    if count != SOURCE_BYTES or digest.hexdigest() != SOURCE_SHA256:
        raise InstallError("SHA-256 do ZIP diverge do arquivo oficial fixado")
    return {"bytes": count, "sha256": digest.hexdigest()}


def decoded_name(info: zipfile.ZipInfo) -> str:
    """Honor Unicode names, then repair legacy CP932 decoded as CP437."""
    if "\x00" in info.orig_filename or "\x00" in info.filename:
        raise InstallError("nome de arquivo contém byte nulo")
    if info.flag_bits & 0x800:
        return info.filename
    try:
        original = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        # Some Python versions already honor the Info-ZIP Unicode Path field.
        return info.filename
    extra = info.extra
    while len(extra) >= 4:
        kind, size = struct.unpack("<HH", extra[:4])
        value, extra = extra[4:4 + size], extra[4 + size:]
        if len(value) != size:
            raise InstallError("metadados de nome Unicode truncados")
        if kind == 0x7075 and size >= 5 and value[0] == 1:
            expected_crc = struct.unpack("<I", value[1:5])[0]
            if expected_crc == zlib.crc32(original):
                try:
                    return value[5:].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise InstallError("nome Unicode inválido no ZIP") from exc
    try:
        return original.decode("cp932")
    except UnicodeDecodeError:
        return info.filename


def checked_members(archive: zipfile.ZipFile) -> tuple[list[tuple[zipfile.ZipInfo, PurePosixPath]], int]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_ENTRIES:
        raise InstallError("ZIP vazio ou com mais de 10.000 entradas")
    members = []
    seen: dict[PurePosixPath, bool] = {}
    total = 0
    for info in entries:
        name = decoded_name(info).replace("\\", "/")
        raw_parts = name.rstrip("/").split("/")
        if (not name or name.startswith("/") or any(part in ("", ".", "..") for part in raw_parts)
                or any(":" in part or any(ord(char) < 32 for char in part) for part in raw_parts)
                or len(raw_parts) > 20 or len(name.encode("utf-8")) > 1024):
            raise InstallError("caminho inseguro no ZIP; extração cancelada")
        relative = PurePosixPath(*raw_parts)
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        is_directory = info.is_dir() or kind == stat.S_IFDIR
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or stat.S_ISLNK(mode):
            raise InstallError("links simbólicos ou arquivos especiais não são permitidos no ZIP")
        if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise InstallError("ZIP criptografado ou com compressão não suportada")
        if relative in seen:
            raise InstallError("caminho duplicado no ZIP após decodificar os nomes")
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES or (is_directory and info.file_size):
            raise InstallError("arquivo acima de 32 MiB ou diretório inválido no ZIP")
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise InstallError("ZIP descompactado excede 512 MiB")
        seen[relative] = is_directory
        members.append((info, relative))
    for relative, is_directory in seen.items():
        if any(parent in seen and not seen[parent] for parent in relative.parents):
            raise InstallError("arquivo e diretório disputam o mesmo caminho no ZIP")
    return members, total


def extract_archive(source: Path, destination: Path) -> dict:
    with zipfile.ZipFile(source) as archive:
        members, expected_total = checked_members(archive)
        if shutil.disk_usage(destination.parent).free < expected_total + 32 * 1024 * 1024:
            raise InstallError("espaço livre insuficiente para extrair a bank")
        extracted = 0
        for info, relative in members:
            path = destination.joinpath(*relative.parts)
            if info.is_dir() or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFDIR:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            count = 0
            with archive.open(info) as stream, os.fdopen(descriptor, "wb") as target:
                while True:
                    block = stream.read(65536)
                    if not block:
                        break
                    count += len(block)
                    extracted += len(block)
                    if count > info.file_size or extracted > MAX_UNPACKED_BYTES:
                        raise InstallError("arquivo extraído excedeu o tamanho declarado")
                    target.write(block)
            if count != info.file_size:
                raise InstallError("arquivo extraído está truncado")
        # Reading each complete member above also verifies its ZIP CRC.
        if extracted != expected_total:
            raise InstallError("volume total extraído diverge do ZIP")
    return {"entries": len(members), "unpacked_bytes": extracted, "crc_verified": True}


def real_directories(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError("destino e suas pastas pai devem ser diretórios reais, sem links simbólicos")


def empty_destination(path: Path) -> bool:
    real_directories(path)
    if path.exists():
        if any(path.iterdir()):
            raise InstallError("destino já contém arquivos; a bank existente foi preservada. Use --directory com outra pasta vazia")
        return True
    return False


def voicebank_index(directory: Path):
    """Use bundled index code without importing the renderer package init."""
    source = TOOLKIT_ROOT.parent.parent / "termux" / "phone-worker" / "teto_renderer"
    package_name = "_teto_english_install_index"
    package = types.ModuleType(package_name)
    package.__path__ = [str(source)]
    names = [package_name, package_name + ".errors", package_name + ".voicebank"]
    previous = {name: sys.modules.get(name) for name in names}
    sys.modules[package_name] = package
    try:
        for filename in ("errors", "voicebank"):
            path = source / (filename + ".py")
            module = types.ModuleType(package_name + "." + filename)
            module.__file__ = str(path)
            module.__package__ = package_name
            sys.modules[module.__name__] = module
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        try:
            index = sys.modules[package_name + ".voicebank"].VoicebankIndex.load(
                directory, minimum_aliases=MINIMUM_ALIASES,
            )
        except sys.modules[package_name + ".errors"].TetoVoicebankError as exc:
            raise InstallError(f"voicebank oficial incompleta: {exc}") from exc
        if not list(directory.rglob("character.txt")) or not list(directory.rglob("*.txt")):
            raise InstallError("metadados ou avisos de uso da bank estão ausentes")
        return index
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def install_voicebank(*, directory: Path | None = None, timeout: float = 120.0) -> dict:
    report = {
        "ok": False, "installed": False, "voicebank_ready": False,
        "voicebank_profile": "english-cvvc", "download_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256, "terms_url": TERMS_URL,
        "worker_configuration_changed": False, "teto_synthesis_verified": False,
        "portuguese_speech_verified": False, "portuguese_quality_verified": False,
        "note": "Instala a bank oficial em pasta separada e preserva seus avisos. Não seleciona a voz no bot nem sintetiza áudio.",
        "checks": {},
    }
    lock = None
    try:
        timeout = timeout_value(timeout)
        destination = Path(os.path.abspath((directory or DEFAULT_DIRECTORY).expanduser()))
        report["directory"] = str(destination)
        empty_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        real_directories(destination.parent)
        lock_path = destination.parent / ("." + destination.name + ".teto-english-install.lock")
        try:
            lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError as exc:
            raise InstallError("há uma instalação em andamento ou um lock antigo; nenhum arquivo foi alterado") from exc
        os.write(lock, str(os.getpid()).encode("ascii"))
        empty_destination(destination)
        if shutil.disk_usage(destination.parent).free < SOURCE_BYTES + EXPECTED_UNPACKED_BYTES + 32 * 1024 * 1024:
            raise InstallError("espaço livre insuficiente para baixar e extrair a bank")
        with tempfile.TemporaryDirectory(prefix=".teto-english-stage-", dir=destination.parent) as temporary:
            stage = Path(temporary)
            archive = stage / "source.zip"
            download_archive(archive, timeout)
            report["checks"]["archive_pin"] = {"ok": True, **archive_identity(archive)}
            data = stage / "bank"
            data.mkdir(mode=0o700)
            report["checks"]["extraction"] = {"ok": True, **extract_archive(archive, data)}
            staged_index = voicebank_index(data)
            report["checks"]["voicebank_index"] = {"ok": True, "aliases": staged_index.alias_count,
                                                   "wav_decode_checked": False}
            if empty_destination(destination):
                # rmdir never removes another process's new files.
                destination.rmdir()
            data.rename(destination)
            report["installed"] = True
            report["checks"]["publication"] = {"ok": True, "atomic": True}
            installed_index = voicebank_index(destination)
            report["voicebank"] = installed_index.snapshot()
            report["ok"] = True
            report["voicebank_ready"] = True
    except (InstallError, OSError, ValueError, zipfile.BadZipFile, NotImplementedError,
            urllib.error.URLError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if lock is not None:
            os.close(lock)
            lock_path.unlink(missing_ok=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Instala somente a voicebank oficial English CVVC da Teto, sem configurar o bot")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--timeout", type=float, default=120.0, help="prazo total do download, de 1 a 120 segundos")
    parser.add_argument("--_download", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options._download is not None:
        try:
            result = stream_download(options._download, options.timeout)
            print(json.dumps({"ok": True, **result}))
            return 0
        except (InstallError, OSError, ValueError, urllib.error.URLError) as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
            return 1
    result = install_voicebank(directory=options.directory, timeout=options.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
