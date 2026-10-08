#!/usr/bin/env python3
"""Fetch the official, pinned Linux app for an optional compatibility test.

This installs the application only. No Teto voice or activation code is bundled.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


ARCHIVE_URL = "https://www.ah-soft.com/voice/setup/archives/voicepeak_v1222.zip"
ARCHIVE_MEMBER = "VOICEPEAK 1.2.22/Linux/Voicepeak-linux64.zip"
EXPECTED_ARCHIVE_BYTES = 203747954
EXPECTED_LINUX_SHA256 = "b88f51ca0622406ec534d63b20fd9b753a6f900e24b4023bf3657292e15c54dc"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_LINUX_ZIP_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 100
DOWNLOAD_SECONDS = 300.0
SOCKET_TIMEOUT = 20.0
COPY_BLOCK_BYTES = 64 * 1024


class InstallError(ValueError):
    """An archive, destination or compatibility download is unsuitable."""


def _check_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InstallError("O limite total de cinco minutos foi excedido.")
    return remaining


def _copy_stream(source, target, *, limit: int, deadline: float, digest=None, set_timeout=None) -> int:
    copied = 0
    # read1 avoids waiting for a whole block from a slowly trickling HTTP peer.
    read = getattr(source, "read1", None) or source.read
    while True:
        remaining = _check_deadline(deadline)
        if set_timeout is not None:
            set_timeout(min(SOCKET_TIMEOUT, remaining))
        chunk = read(min(COPY_BLOCK_BYTES, limit - copied + 1))
        _check_deadline(deadline)
        if not chunk:
            return copied
        copied += len(chunk)
        if copied > limit:
            raise InstallError("O arquivo ultrapassa o limite de tamanho permitido.")
        target.write(chunk)
        if digest is not None:
            digest.update(chunk)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InstallError("O download oficial respondeu com um redirecionamento inesperado.")


def _download_archive(target: Path, *, deadline: float, progress=print) -> None:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "Teto-Termux-Compatibility/1"})
    timeout = min(SOCKET_TIMEOUT, _check_deadline(deadline))
    progress("Baixando o programa Linux da AHS; este arquivo não contém a voz Teto.")
    with opener.open(request, timeout=timeout) as response, target.open("wb") as out:
        if response.status != 200:
            raise InstallError("O servidor oficial não retornou o arquivo completo.")
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                advertised = int(length)
            except ValueError as exc:
                raise InstallError("Tamanho de download inválido.") from exc
            if advertised != EXPECTED_ARCHIVE_BYTES or advertised > MAX_ARCHIVE_BYTES:
                raise InstallError("O tamanho do download oficial mudou; confira a versão antes de continuar.")
        def set_timeout(seconds):
            # HTTPResponse closes fp after its last body chunk. Recheck it each
            # iteration rather than retaining a socket that may have closed.
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(seconds)

        copied = _copy_stream(response, out, limit=MAX_ARCHIVE_BYTES, deadline=deadline, set_timeout=set_timeout)
    if copied != EXPECTED_ARCHIVE_BYTES:
        raise InstallError("O download está incompleto ou a versão oficial mudou.")
    progress("Download concluído; verificando o programa Linux.")


def _validate_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not members or len(members) > MAX_MEMBERS:
        raise InstallError("Quantidade de arquivos inválida no programa Linux.")
    names: set[str] = set()
    files: set[str] = set()
    total = 0
    for member in members:
        name = member.filename
        raw_name = member.orig_filename
        directory = member.is_dir()
        canonical = name[:-1] if directory else name
        parts = canonical.split("/")
        if (
            "\x00" in raw_name
            or "\\" in name
            or name.startswith("/")
            or not parts
            or parts[0] != "Voicepeak"
            or any(part in ("", ".", "..") for part in parts)
            or len(name.encode("utf-8")) > 1024
            or (len(parts) == 1 and not directory)
        ):
            raise InstallError("Caminho inválido no programa Linux.")
        if canonical in names:
            raise InstallError("Caminho duplicado no programa Linux.")
        names.add(canonical)
        mode = (member.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not directory) or (kind == stat.S_IFREG and directory):
            raise InstallError("Links e arquivos especiais não são aceitos.")
        if member.flag_bits & 1 or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise InstallError("Formato de arquivo não permitido no programa Linux.")
        if member.file_size < 0 or member.file_size > MAX_FILE_BYTES or (directory and member.file_size):
            raise InstallError("Arquivo individual maior que o limite permitido.")
        total += member.file_size
        if total > MAX_EXTRACTED_BYTES:
            raise InstallError("O programa extraído ultrapassa o limite de tamanho permitido.")
        if not directory:
            files.add(canonical)
    for name in names:
        components = name.split("/")
        if any("/".join(components[:i]) in files for i in range(1, len(components))):
            raise InstallError("Um arquivo ocupa o caminho de uma pasta.")
    if "Voicepeak/voicepeak" not in files or not any(name.startswith("Voicepeak/fonts/") for name in files) or not any(name.startswith("Voicepeak/dic/") for name in files):
        raise InstallError("O programa Linux está incompleto: executável, fonts e dic são obrigatórios.")
    return members


def _extract_linux_zip(linux_zip: Path, staging: Path, *, deadline: float) -> Path:
    with zipfile.ZipFile(linux_zip) as archive:
        members = _validate_members(archive)
        if archive.testzip() is not None:
            raise InstallError("Falha de integridade CRC no programa Linux.")
        for member in members:
            _check_deadline(deadline)
            target = staging.joinpath(*member.filename.rstrip("/").split("/"))
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            with archive.open(member) as source, target.open("xb") as out:
                copied = _copy_stream(source, out, limit=member.file_size, deadline=deadline)
            if copied != member.file_size:
                raise InstallError("Arquivo extraído incompleto.")
            target.chmod(0o755 if member.filename == "Voicepeak/voicepeak" else 0o644)
    tree = staging / "Voicepeak"
    for directory in (tree, *(path for path in tree.rglob("*") if path.is_dir())):
        directory.chmod(0o755)
    return tree


def _rename_new(source: Path, destination: Path) -> None:
    """Commit without overwriting even a directory created concurrently."""
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise InstallError("Este sistema não oferece a instalação atômica sem sobrescrever pastas.")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        # AT_FDCWD=-100, RENAME_NOREPLACE=1. Available on current Android/Linux.
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            failure = ctypes.get_errno()
            if failure == errno.EEXIST:
                raise InstallError("Voicepeak já existe; a instalação atual foi preservada.")
            raise OSError(failure, os.strerror(failure))
    elif os.name == "nt":
        os.rename(source, destination)  # Windows rename refuses existing targets.
    else:
        raise InstallError("A instalação atômica deste script requer Android/Linux.")


def install_engine(*, archive: Path | None = None, destination: Path | None = None, progress=print) -> Path:
    parent = (destination or Path.home() / ".voicepeak-termux" / "engine").expanduser().resolve()
    engine = parent / "Voicepeak"
    if os.path.lexists(engine):
        raise InstallError("Voicepeak já existe; arquivos de configuração e licenças não serão sobrescritos.")
    if str(parent) == "/sdcard" or any(str(parent).startswith(prefix) for prefix in ("/sdcard/", "/storage/", "/mnt/media_rw/")):
        raise InstallError("Use a pasta privada do Termux, não o armazenamento compartilhado do Android.")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    with tempfile.TemporaryDirectory(prefix=".voicepeak-extract-", dir=parent) as temp:
        staging = Path(temp)
        outer = archive.expanduser().resolve() if archive is not None else staging / "official.zip"
        if archive is None:
            _download_archive(outer, deadline=deadline, progress=progress)
        if not outer.is_file() or not 0 < outer.stat().st_size <= MAX_ARCHIVE_BYTES:
            raise InstallError("O ZIP oficial não existe ou ultrapassa o limite de 256 MiB.")
        linux_zip = staging / "linux.zip"
        digest = hashlib.sha256()
        with zipfile.ZipFile(outer) as bundle:
            matches = [member for member in bundle.infolist() if member.filename == ARCHIVE_MEMBER]
            if len(bundle.infolist()) > 32 or len(matches) != 1 or matches[0].is_dir() or matches[0].file_size > MAX_LINUX_ZIP_BYTES:
                raise InstallError("O ZIP oficial não contém exatamente o programa Linux esperado.")
            with bundle.open(matches[0]) as source, linux_zip.open("xb") as out:
                _copy_stream(source, out, limit=MAX_LINUX_ZIP_BYTES, deadline=deadline, digest=digest)
        if digest.hexdigest() != EXPECTED_LINUX_SHA256:
            raise InstallError("O SHA256 do programa Linux não corresponde à versão oficial verificada.")
        tree = _extract_linux_zip(linux_zip, staging, deadline=deadline)
        _check_deadline(deadline)
        _rename_new(tree, engine)
    progress("Programa para teste instalado. A voz Teto exige compra e ativação oficiais.")
    progress("Executável: " + str(engine / "voicepeak"))
    return engine


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Instala o programa Linux oficial 1.2.22 para testar compatibilidade; não inclui voz ou licença Teto.")
    parser.add_argument("--archive", type=Path, help="ZIP oficial completo já baixado; evita download.")
    parser.add_argument("--destination", type=Path, help="Pasta que receberá Voicepeak/ (padrão: ~/.voicepeak-termux/engine).")
    args = parser.parse_args(argv)
    try:
        install_engine(archive=args.archive, destination=args.destination)
    except (InstallError, OSError, zipfile.BadZipFile, RuntimeError, urllib.error.URLError) as exc:
        print("Não foi possível instalar o programa: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
