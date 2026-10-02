"""Tracked working-tree ZIPs, validated cache and shared bounded generation.

Source files keep their current contents. Sensitive names and paths are excluded;
this service does not redact source code or use the committed Git tree.
"""

from __future__ import annotations

import asyncio
import errno
import io
import logging
import os
import stat
import subprocess
import time
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


logger = logging.getLogger(__name__)
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ARCHIVE_TIMEOUT_SECONDS = 70.0
BASE_ARCHIVE_CACHE_TTL_SECONDS = 60.0
BASE_ARCHIVE_POLICY_VERSION = 1
BASE_ARCHIVE_COMPRESSLEVEL = 6

BASE_ARCHIVE_ROOT_NAME = "tts-bot-main"
BASE_ARCHIVE_MAX_BYTES = 23 * 1024 * 1024
# Limite por arquivo da base leve. A base leve é para análise/patch,
# não para transportar binários pesados toda vez.
BASE_ARCHIVE_MAX_FILE_BYTES = 1_250_000
BASE_ARCHIVE_SENSITIVE_NAMES = {
    ".env",
    "cookies.txt",
    "cookie.txt",
    "youtube-cookies.txt",
}
BASE_ARCHIVE_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)

# A base leve é enviada para análise de código, não para deploy.
# Mantemos ela leve pulando assets, binários, builds e manifestos gerados.
BASE_ARCHIVE_ASSET_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".flac",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".otf",
    ".png",
    ".svg",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
}
BASE_ARCHIVE_BINARY_EXTENSIONS = {
    ".7z",
    ".a",
    ".aar",
    ".apk",
    ".apks",
    ".bin",
    ".class",
    ".dat",
    ".deb",
    ".dex",
    ".dll",
    ".dylib",
    ".elf",
    ".exe",
    ".gz",
    ".jar",
    ".lz4",
    ".lzma",
    ".o",
    ".onnx",
    ".pt",
    ".rpm",
    ".so",
    ".tar",
    ".tgz",
    ".wasm",
    ".xz",
    ".zip",
    ".zst",
}
BASE_ARCHIVE_BINARY_DIR_MARKERS = (
    "android/core-worker-app/app/src/main/jnilibs/",
    "android/core-worker-app/app/src/main/assets/core-linux/bin/",
    "android/core-worker-app/app/src/main/assets/core-linux/rootfs/",
    "android/core-worker-app/releases/",
    "android/core-worker-app/app/build/",
    "build/",
    "dist/",
)
BASE_ARCHIVE_ASSET_DIR_NAMES = {
    "assets",
    "audio",
    "fonts",
    "images",
    "media",
    "sounds",
    "sfx",
}
BASE_ARCHIVE_MANIFEST_NAMES = {
    "asset-manifest.json",
    "manifest.json",
    "manifest.webmanifest",
    "site.webmanifest",
}


def _is_sensitive_tracked_file(rel: str) -> bool:
    normalized = rel.replace("\\", "/").lstrip("/")
    name = Path(normalized).name.lower()
    lowered = normalized.lower()
    if name in BASE_ARCHIVE_SENSITIVE_NAMES:
        return True

    # .env real e variantes locais são sensíveis.
    # .env.example/.env.sample/.env.template são exemplos rastreados e devem ir no zip.
    allowed_env_examples = {".env.example", ".env.sample", ".env.template"}
    if name.startswith(".env") and name not in allowed_env_examples:
        return True

    if lowered.endswith(BASE_ARCHIVE_SENSITIVE_SUFFIXES):
        return True
    # Banco/log/cookies não deveriam estar rastreados, mas se estiverem, não anexa no Discord.
    if lowered.endswith((".sqlite", ".sqlite3", ".db", ".log")):
        return True
    if "cookies" in lowered and lowered.endswith(".txt"):
        return True
    return False


def _base_archive_skip_reason(rel: str, *, size: int | None = None) -> str | None:
    normalized = rel.replace("\\", "/").lstrip("/")
    lowered = normalized.lower()
    parts = [part.lower() for part in normalized.split("/") if part]
    name = parts[-1] if parts else ""
    suffixes = [suffix.lower() for suffix in Path(name).suffixes]
    suffix = suffixes[-1] if suffixes else ""

    if name in BASE_ARCHIVE_MANIFEST_NAMES:
        return "manifesto"
    if len(parts) >= 2 and parts[-2] == ".vite" and name == "manifest.json":
        return "manifesto"
    if suffix in BASE_ARCHIVE_ASSET_EXTENSIONS:
        return "asset"
    if suffix in BASE_ARCHIVE_BINARY_EXTENSIONS:
        return "binário"
    if len(suffixes) >= 2 and "".join(suffixes[-2:]) in {".tar.gz", ".tar.xz", ".tar.zst"}:
        return "binário"

    # Diretórios de mídia/binários ficam fora mesmo quando o arquivo não tem extensão.
    # Isso evita mandar Box64, libs nativas, APKs publicados, rootfs e assets pesados
    # toda vez que o dono pede a base Git leve.
    if any(part in BASE_ARCHIVE_ASSET_DIR_NAMES for part in parts):
        return "asset"
    if any(lowered.startswith(marker) or f"/{marker}" in lowered for marker in BASE_ARCHIVE_BINARY_DIR_MARKERS):
        return "binário"
    if "dist" in parts and any(part in {"assets", "audio", "images", "media"} for part in parts):
        return "asset"
    if "public" in parts and any(part in {"audio", "images", "assets", "media"} for part in parts):
        return "asset"
    if size is not None and size > BASE_ARCHIVE_MAX_FILE_BYTES:
        return f"arquivo grande ({size / (1024 * 1024):.1f} MB)"
    return None


class BaseArchiveError(Exception):
    """A safe, actionable message that can be shown to the requester."""


class BaseArchiveTimeout(BaseArchiveError):
    pass


class _SourceChanged(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BaseArchiveResult:
    payload: bytes
    filename: str
    file_count: int
    cache_hit: bool = False


@dataclass(frozen=True, slots=True)
class _SourceFile:
    relative: str
    metadata: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    root: Path
    files: tuple[_SourceFile, ...]
    signature: tuple


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    signature: tuple
    result: BaseArchiveResult
    expires_at: float


def archive_filename() -> str:
    tz_name = os.getenv("BOT_TIMEZONE") or os.getenv("VPS_TIMEZONE") or os.getenv("TZ") or "America/Sao_Paulo"
    try:
        tz = ZoneInfo(tz_name.strip() or "America/Sao_Paulo")
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    return "repo-" + datetime.now(tz).strftime("%Y%m%d-%H%M%S") + ".zip"


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BaseArchiveTimeout("A preparação da base excedeu o limite de tempo. Tente novamente.")
    return remaining


def _git(args: list[str], root: Path, deadline: float, *, timeout: float) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="surrogateescape", check=False,
            timeout=min(timeout, _remaining(deadline)),
        )
    except subprocess.TimeoutExpired as exc:
        raise BaseArchiveTimeout("A consulta ao Git excedeu o limite de tempo. Tente novamente.") from exc
    except OSError as exc:
        logger.warning("[utility/base] git unavailable: %s", type(exc).__name__)
        raise BaseArchiveError("Não consegui acessar o Git do projeto para preparar a base.") from exc
    _remaining(deadline)
    if result.returncode:
        logger.warning("[utility/base] git %s failed: returncode=%s", args[0], result.returncode)
        raise BaseArchiveError("O repositório Git não está acessível; a base não pôde ser preparada.")
    return result.stdout


def _metadata(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _policy_signature() -> tuple:
    return (
        BASE_ARCHIVE_POLICY_VERSION, BASE_ARCHIVE_ROOT_NAME, BASE_ARCHIVE_MAX_BYTES,
        BASE_ARCHIVE_MAX_FILE_BYTES, BASE_ARCHIVE_COMPRESSLEVEL,
        tuple(sorted(BASE_ARCHIVE_SENSITIVE_NAMES)), BASE_ARCHIVE_SENSITIVE_SUFFIXES,
        tuple(sorted(BASE_ARCHIVE_ASSET_EXTENSIONS)), tuple(sorted(BASE_ARCHIVE_BINARY_EXTENSIONS)),
        BASE_ARCHIVE_BINARY_DIR_MARKERS, tuple(sorted(BASE_ARCHIVE_ASSET_DIR_NAMES)),
        tuple(sorted(BASE_ARCHIVE_MANIFEST_NAMES)),
    )


def _collect_snapshot(configured_root: Path, deadline: float) -> _Snapshot:
    started = time.monotonic()
    root_text = _git(["rev-parse", "--show-toplevel"], configured_root, deadline, timeout=8.0).strip()
    if not root_text:
        raise BaseArchiveError("O Git não retornou a raiz do projeto.")
    root = Path(root_text).resolve()
    tracked = tuple(sorted(set(filter(None, _git(["ls-files", "-z"], root, deadline, timeout=20.0).split("\0")))))
    if not tracked:
        raise BaseArchiveError("O Git não encontrou arquivos rastreados para anexar.")
    listed_at = time.monotonic()
    files: list[_SourceFile] = []
    # Check each directory once; unlike stat(), lstat() does not follow symlinks.
    directories: dict[Path, bool] = {}
    for relative in tracked:
        _remaining(deadline)
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            continue
        if _is_sensitive_tracked_file(relative) or _base_archive_skip_reason(relative):
            continue
        source = root / relative
        safe = True
        for parent in source.relative_to(root).parents:
            directory = root / parent
            if directory not in directories:
                try:
                    directories[directory] = stat.S_ISDIR(directory.lstat().st_mode)
                except (FileNotFoundError, NotADirectoryError):
                    directories[directory] = False
                except OSError as exc:
                    raise BaseArchiveError("Não consegui verificar os arquivos rastreados do projeto.") from exc
            if not directories[directory]:
                safe = False
                break
        if not safe:
            continue
        try:
            info = source.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue  # A tracked file deleted in the working tree has no current contents.
        except OSError as exc:
            raise BaseArchiveError("Não consegui verificar os arquivos rastreados do projeto.") from exc
        if not stat.S_ISREG(info.st_mode) or _base_archive_skip_reason(relative, size=info.st_size):
            continue
        files.append(_SourceFile(relative, _metadata(info)))
    if not files:
        raise BaseArchiveError("Nenhum arquivo rastreado elegível ficou disponível para a base leve.")
    frozen_files = tuple(files)
    signature = (str(root), _policy_signature(), tracked, frozen_files)
    logger.info("[utility/base] listing=%.3fs validation=%.3fs tracked=%d eligible=%d",
                listed_at - started, time.monotonic() - listed_at, len(tracked), len(files))
    return _Snapshot(root, frozen_files, signature)


def _read_source(root_fd: int, source: _SourceFile, deadline: float) -> bytes:
    """Open every path segment relative to a verified fd without following links."""
    fd = os.dup(root_fd)
    try:
        parts = PurePosixPath(source.relative).parts
        for part in parts[:-1]:
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if _metadata(os.fstat(stream.fileno())) != source.metadata:
                raise _SourceChanged
            chunks: list[bytes] = []
            count = 0
            while True:
                _remaining(deadline)
                chunk = stream.read(min(128 * 1024, BASE_ARCHIVE_MAX_FILE_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > BASE_ARCHIVE_MAX_FILE_BYTES:
                    raise _SourceChanged
            if count != source.metadata[3] or _metadata(os.fstat(stream.fileno())) != source.metadata:
                raise _SourceChanged
            return b"".join(chunks)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _SourceChanged from exc
    except OSError as exc:
        # ELOOP also covers a source replaced by a link after the snapshot.
        if exc.errno == errno.ELOOP:
            raise _SourceChanged from exc
        raise BaseArchiveError("Não consegui ler todos os arquivos da base. Verifique o acesso ao projeto.") from exc
    finally:
        os.close(fd)


def _write_archive(snapshot: _Snapshot, deadline: float) -> BaseArchiveResult:
    started = time.monotonic()
    bio = io.BytesIO()
    root_fd = os.open(snapshot.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=BASE_ARCHIVE_COMPRESSLEVEL) as zf:
            for source in snapshot.files:
                _remaining(deadline)
                content = _read_source(root_fd, source, deadline)
                stamp = time.localtime(source.metadata[4] / 1_000_000_000)[:6]
                if stamp[0] < 1980:
                    stamp = (1980, 1, 1, 0, 0, 0)
                elif stamp[0] > 2107:
                    stamp = (2107, 12, 31, 23, 59, 58)
                member = zipfile.ZipInfo(f"{BASE_ARCHIVE_ROOT_NAME}/{source.relative}", date_time=stamp)
                member.external_attr = (source.metadata[2] & 0xFFFF) << 16
                zf.writestr(member, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=BASE_ARCHIVE_COMPRESSLEVEL)
                _remaining(deadline)
                if bio.tell() > BASE_ARCHIVE_MAX_BYTES:
                    raise BaseArchiveError("A base ficou grande demais para anexar com segurança no Discord.")
    finally:
        os.close(root_fd)
    payload = bio.getvalue()
    if len(payload) > BASE_ARCHIVE_MAX_BYTES:
        raise BaseArchiveError("A base ficou grande demais para anexar com segurança no Discord.")
    logger.info("[utility/base] compression=%.3fs files=%d bytes=%d", time.monotonic() - started, len(snapshot.files), len(payload))
    return BaseArchiveResult(payload, archive_filename(), len(snapshot.files))


def _build_validated(configured_root: Path, deadline: float, snapshot: _Snapshot) -> tuple[_Snapshot, BaseArchiveResult]:
    for attempt in range(2):
        try:
            result = _write_archive(snapshot, deadline)
            after = _collect_snapshot(configured_root, deadline)
            if after.signature == snapshot.signature:
                return snapshot, result
        except _SourceChanged:
            after = _collect_snapshot(configured_root, deadline)
        if not attempt:
            snapshot = after
    raise BaseArchiveError("O projeto mudou durante a preparação. Aguarde as alterações terminarem e tente novamente.")


def build_git_tracked_base_archive_sync(root: Path = DEFAULT_REPO_ROOT, *, timeout: float = BASE_ARCHIVE_TIMEOUT_SECONDS) -> BaseArchiveResult:
    """One-shot generation, used by compatibility callers and focused checks."""
    try:
        deadline = time.monotonic() + timeout
        snapshot = _collect_snapshot(Path(root), deadline)
        return _build_validated(Path(root), deadline, snapshot)[1]
    except BaseArchiveError:
        raise
    except Exception as exc:
        logger.exception("[utility/base] one-shot archive generation failed")
        raise BaseArchiveError("Não consegui preparar a base agora. Tente novamente.") from exc


class BaseArchiveService:
    """One cached artifact and one shared worker, including after waiter timeouts."""

    def __init__(self, root: Path = DEFAULT_REPO_ROOT, *, generation_timeout: float = BASE_ARCHIVE_TIMEOUT_SECONDS,
                 cache_ttl: float = BASE_ARCHIVE_CACHE_TTL_SECONDS):
        self.root = Path(root)
        self.generation_timeout = generation_timeout
        self.cache_ttl = max(0.0, cache_ttl)
        self._cache: _CacheEntry | None = None
        self._inflight: asyncio.Task[BaseArchiveResult] | None = None
        self._expiry_handle: asyncio.TimerHandle | None = None

    def _get_or_build_sync(self) -> BaseArchiveResult:
        deadline = time.monotonic() + self.generation_timeout
        snapshot = _collect_snapshot(self.root, deadline)
        cache = self._cache
        if cache and cache.expires_at > time.monotonic() and cache.signature == snapshot.signature:
            logger.info("[utility/base] cache=hit bytes=%d", len(cache.result.payload))
            return replace(cache.result, cache_hit=True)
        self._cache = None
        logger.info("[utility/base] cache=miss")
        stable, result = _build_validated(self.root, deadline, snapshot)
        self._cache = _CacheEntry(stable.signature, result, time.monotonic() + self.cache_ttl)
        return result

    def _expire(self, cache: _CacheEntry) -> None:
        self._expiry_handle = None
        if self._cache is cache:
            self._cache = None

    async def _run(self) -> BaseArchiveResult:
        try:
            return await asyncio.to_thread(self._get_or_build_sync)
        except (BaseArchiveError, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.exception("[utility/base] archive generation failed")
            raise BaseArchiveError("Não consegui preparar a base agora. Tente novamente.") from exc
        finally:
            if self._expiry_handle:
                self._expiry_handle.cancel()
            cache = self._cache
            if cache:
                self._expiry_handle = asyncio.get_running_loop().call_later(
                    max(0.0, cache.expires_at - time.monotonic()), self._expire, cache,
                )

    def _finish_task(self, task: asyncio.Task) -> None:
        # A caller can leave before the shared task finishes; consume its exception.
        if not task.cancelled():
            task.exception()
        if self._inflight is task:
            self._inflight = None

    async def get_archive(self, *, wait_timeout: float | None = None) -> BaseArchiveResult:
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.create_task(self._run())
            self._inflight.add_done_callback(self._finish_task)
        timeout = self.generation_timeout + 1.0 if wait_timeout is None else wait_timeout
        try:
            return await asyncio.wait_for(asyncio.shield(self._inflight), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BaseArchiveTimeout("A base ainda está sendo preparada. Tente novamente em alguns instantes.") from exc


_services: dict[Path, BaseArchiveService] = {}


def get_base_archive_service(root: Path = DEFAULT_REPO_ROOT) -> BaseArchiveService:
    key = Path(root).absolute()
    if key not in _services:
        _services[key] = BaseArchiveService(key)
    return _services[key]
