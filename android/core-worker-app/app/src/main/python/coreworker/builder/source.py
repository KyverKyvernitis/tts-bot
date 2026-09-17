"""Source download, ZIP validation/extraction and project discovery for APK builds."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import http.client
import os
import urllib.error
from pathlib import Path
from typing import Any


class SourceDownloadTransientError(RuntimeError):
    """Falha de transporte/truncamento que pode ser tentada novamente."""


class SourceHashMismatchError(ValueError):
    """Conteúdo completo não corresponde ao job; retry do mesmo URL é inseguro."""


def download_source(
    url: str,
    target: Path,
    expected_sha: str,
    expected_bytes: int,
    server_url: str,
    *,
    same_origin,
    short,
    max_source_bytes: int,
    attempts: int,
    urlopen,
    request_type,
    sleep,
    replace,
    open_directory,
    fsync,
    close,
    getpid,
) -> dict[str, Any]:
    if not same_origin(url, server_url):
        raise ValueError("source_zip_url precisa apontar para a mesma origem autenticada da VPS")
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        target.unlink()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        partial = target.with_name(f".{target.name}.{getpid()}.{attempt}.part")
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        try:
            request = request_type(url, headers={
                "User-Agent": "CoreWorkerApkSelfBuilder/2",
                "Cache-Control": "no-cache",
                "Accept-Encoding": "identity",
            })
            digest = hashlib.sha256()
            total = 0
            declared_bytes = 0
            with urlopen(request, timeout=60) as response:
                final_url = response.geturl()
                if not same_origin(final_url, server_url):
                    raise ValueError("redirect do source zip saiu da origem autenticada da VPS")
                raw_length = str(response.headers.get("Content-Length") or "").strip()
                if raw_length:
                    try:
                        declared_bytes = int(raw_length)
                    except ValueError as exc:
                        raise SourceDownloadTransientError("content_length_invalid") from exc
                    if declared_bytes < 0 or declared_bytes > max_source_bytes:
                        raise ValueError("Content-Length do source zip excede o limite")
                    if expected_bytes > 0 and declared_bytes != expected_bytes:
                        raise SourceHashMismatchError(
                            f"source_length_contract_mismatch: job={expected_bytes}, http={declared_bytes}"
                        )
                with partial.open("wb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > max_source_bytes:
                            raise ValueError("source zip excede o limite do autobuilder")
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    fsync(output.fileno())
            if declared_bytes and total != declared_bytes:
                raise SourceDownloadTransientError(
                    f"network_truncation: Content-Length={declared_bytes}, recebido={total}"
                )
            if expected_bytes > 0 and total != expected_bytes:
                raise SourceDownloadTransientError(
                    f"network_truncation_or_length_mismatch: esperado={expected_bytes}, recebido={total}"
                )
            actual = digest.hexdigest()
            if expected_sha and actual.lower() != expected_sha.lower():
                raise SourceHashMismatchError(
                    f"source_hash_mismatch: esperado={expected_sha.lower()}, recebido={actual.lower()}"
                )
            replace(partial, target)
            try:
                directory_fd = open_directory(str(target.parent), os.O_RDONLY)
                try:
                    fsync(directory_fd)
                finally:
                    close(directory_fd)
            except OSError:
                pass
            return {
                "url": final_url,
                "bytes": total,
                "sha256": actual,
                "contentLength": declared_bytes or None,
                "attempts": attempt,
                "stagedAtomically": True,
            }
        except SourceHashMismatchError:
            raise
        except urllib.error.HTTPError as exc:
            if int(exc.code or 0) in {408, 425, 429} or 500 <= int(exc.code or 0) < 600:
                last_error = SourceDownloadTransientError(f"source_http_{exc.code}")
            else:
                raise ValueError(f"source_http_{exc.code}") from exc
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EDQUOT, errno.ENOSPC, errno.EROFS}:
                raise ValueError(f"source_staging_io_permanent: errno={exc.errno}") from exc
            last_error = exc
        except (SourceDownloadTransientError, urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
            last_error = exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
        if attempt < attempts:
            sleep(float(attempt))
    raise SourceDownloadTransientError(
        f"source_download_retry_exhausted: {short(last_error, 300)}"
    ) from last_error


def safe_extract_zip(
    source: Path,
    target: Path,
    *,
    zipfile_type,
    copyfileobj,
    is_inside,
    max_entries: int,
    max_expanded_bytes: int,
) -> dict[str, Any]:
    """Validate the complete archive before the first extraction write."""
    root = target.resolve()
    count = 0
    expanded = 0
    validated: list[tuple[Any, Path, bool]] = []
    seen: set[str] = set()
    with zipfile_type(source) as archive:
        for info in archive.infolist():
            count += 1
            if count > max_entries:
                raise ValueError("source zip contém arquivos demais")
            name = str(info.filename or "").replace("\\", "/")
            if not name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError("source zip contém caminho inseguro")
            normalized = name.rstrip("/")
            if not normalized:
                raise ValueError("source zip contém caminho inseguro")
            if normalized in seen:
                raise ValueError("source zip contém membro duplicado")
            seen.add(normalized)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("source zip contém link simbólico")
            expanded += max(0, int(info.file_size or 0))
            if expanded > max_expanded_bytes:
                raise ValueError("source zip excede limite expandido")
            destination = (root / normalized).resolve()
            if not is_inside(destination, root):
                raise ValueError("source zip tenta sair do workspace")
            validated.append((info, destination, name.endswith("/")))

        target.mkdir(parents=True, exist_ok=True)
        for info, destination, is_directory in validated:
            if is_directory:
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, destination.open("wb") as dst:
                copyfileobj(src, dst, 1024 * 1024)
    return {"files": count, "expandedBytes": expanded}


def find_project(source_root: Path, project_subdir: str, *, safe_rel) -> Path:
    rel = safe_rel(project_subdir or "android/core-worker-app")
    direct = source_root / rel
    if (direct / "app/build.gradle").is_file():
        return direct
    children = [item for item in source_root.iterdir() if item.is_dir()]
    for child in children[:20]:
        nested = child / rel
        if (nested / "app/build.gradle").is_file():
            return nested
    candidates = list(source_root.glob("**/app/build.gradle"))
    candidates = [path.parent.parent for path in candidates if len(path.relative_to(source_root).parts) <= 8]
    if len(candidates) == 1:
        return candidates[0]
    raise FileNotFoundError("projeto android/core-worker-app não encontrado no source zip")
