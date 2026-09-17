"""APK artifact validation, durable promotion, publication and retention."""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any


def validate_apk(
    path: Path,
    *,
    expected_version_name: str = "",
    expected_version_code: int = 0,
    max_apk_bytes: int,
    inspect_identity,
    assert_expected_identity,
    sha256_file,
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size < 1024 * 1024:
        raise FileNotFoundError("APK gerado não encontrado ou pequeno demais")
    if path.stat().st_size > max_apk_bytes:
        raise ValueError("APK gerado excede o limite")
    identity = inspect_identity(path)
    assert_expected_identity(
        identity,
        expected_package="dev.core.worker",
        expected_version_name=expected_version_name,
        expected_version_code=expected_version_code,
    )
    return {
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        **identity,
    }


def raise_if_publish_cancelled(marker: Path | None, connection: Any = None, *, error_type) -> None:
    if marker is None or not marker.is_file():
        return
    if connection is not None:
        with contextlib.suppress(Exception):
            connection.close()
    raise error_type("lease_ownership_lost: publicação cancelada")


def multipart_publish(
    apk_path: Path,
    fields: dict[str, Any],
    publish_url: str,
    token: str,
    worker_id: str,
    worker_version: str,
    cancellation_marker: Path | None = None,
    *,
    safe_filename,
    short,
    raise_if_cancelled,
    https_connection=http.client.HTTPSConnection,
    http_connection=http.client.HTTPConnection,
    wall_time=time.time,
    getpid=os.getpid,
) -> dict[str, Any]:
    raise_if_cancelled(cancellation_marker)
    parsed = urllib.parse.urlsplit(publish_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL de publicação inválida")
    boundary = "----CoreWorkerApk" + hashlib.sha256(f"{wall_time()}:{getpid()}".encode()).hexdigest()[:24]

    parts: list[bytes] = []
    for name, value in fields.items():
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        parts.append((
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        ).encode("utf-8"))
    filename = safe_filename(fields.get("filename"), "CoreWorker-debug.apk")
    file_header = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"apk\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/vnd.android.package-archive\r\n\r\n"
    ).encode("utf-8")
    ending = f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_length = sum(len(item) for item in parts) + len(file_header) + apk_path.stat().st_size + len(ending)
    connection_cls = https_connection if parsed.scheme == "https" else http_connection
    connection = connection_cls(parsed.hostname, parsed.port, timeout=180)
    try:
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection.putrequest("POST", path)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("X-Core-Worker-ID", worker_id)
        connection.putheader("X-Core-Worker-Version", worker_version)
        connection.putheader("X-Phone-Worker-Token", token)
        connection.putheader("User-Agent", f"CoreWorkerApkSelfBuilder/{worker_version}")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        for item in parts:
            raise_if_cancelled(cancellation_marker, connection)
            connection.send(item)
        raise_if_cancelled(cancellation_marker, connection)
        connection.send(file_header)
        with apk_path.open("rb") as fh:
            while True:
                block = fh.read(1024 * 1024)
                if not block:
                    break
                raise_if_cancelled(cancellation_marker, connection)
                connection.send(block)
        raise_if_cancelled(cancellation_marker, connection)
        connection.send(ending)
        response = connection.getresponse()
        raw = response.read(1024 * 1024)
        raise_if_cancelled(cancellation_marker)
        text = raw.decode("utf-8", errors="replace")
        try:
            body = json.loads(text or "{}")
        except Exception:
            body = {"ok": False, "error": short(text, 500)}
        if response.status < 200 or response.status >= 300:
            return {
                "ok": False,
                "status": response.status,
                "error": short(body.get("error") if isinstance(body, dict) else text, 500),
            }
        return body if isinstance(body, dict) else {"ok": False, "error": "resposta inválida da VPS"}
    finally:
        with contextlib.suppress(Exception):
            connection.close()


def publish_latest(
    files: Path,
    payload: dict[str, Any],
    server_url: str,
    worker_id: str,
    token: str,
    worker_version: str,
    *,
    payload_cancellation_marker,
    raise_if_cancelled,
    safe_json_load,
    is_inside,
    validate_apk_fn,
    same_origin,
    multipart_publish_fn,
) -> dict[str, Any]:
    builder = files / "apk-self-builder"
    cancellation_marker = payload_cancellation_marker(builder, payload)
    raise_if_cancelled(cancellation_marker)
    metadata_path = builder / "artifacts/latest-artifact.json"
    meta = safe_json_load(metadata_path)
    apk = Path(str(meta.get("artifact_path") or ""))
    if not apk.is_file() or not is_inside(apk, builder):
        raise FileNotFoundError("nenhum APK autoconstrído persistido para republicar")
    validated = validate_apk_fn(apk)
    persisted_sha = str(meta.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", persisted_sha):
        raise ValueError("sha256 do último artifact ausente ou inválido")
    if persisted_sha != str(validated["sha256"]).lower():
        raise ValueError("sha256 do último artifact divergente")
    publish_url = str(payload.get("publish_url") or payload.get("publishUrl") or server_url.rstrip("/") + "/core-worker/app/publish")
    if not same_origin(publish_url, server_url):
        raise ValueError("publish_url precisa apontar para a mesma VPS")
    fields = {
        "worker_id": worker_id,
        "workerName": "Core Worker APK self-builder",
        "filename": f"CoreWorker-v{validated['versionName']}-debug.apk",
        "versionName": validated["versionName"],
        "versionCode": int(validated["versionCode"]),
        "sha256": validated["sha256"],
        "requiredAgentVersion": worker_version,
        "notifyUsers": "true",
        "notificationRequested": "true",
        "sourceSha256": meta.get("sourceSha256") or "",
        "sourceFingerprint": meta.get("sourceFingerprint") or meta.get("sourceSha256") or "",
        "notificationId": meta.get("notificationId") or "",
        "apkSigningMode": meta.get("apkSigningMode") or "compat-vps-debug-keystore",
        "apkSigningKeystoreSha256": str(meta.get("apkSigningKeystoreSha256") or "")[:64],
        "changelog": payload.get("changelog") or meta.get("changelog") or ["APK compilado pelo próprio Core Worker APK"],
    }
    published = multipart_publish_fn(
        apk, fields, publish_url, token, worker_id, worker_version,
        cancellation_marker=cancellation_marker,
    )
    return {
        "ok": bool(published.get("ok")),
        "summary": "APK republicado pelo próprio APK" if published.get("ok") else "falha publicando APK autoconstrído",
        "apk": {"filename": fields["filename"], **validated},
        "publish": published,
        "artifact": meta,
    }


def persist_artifact(
    built_apk: Path,
    artifacts: Path,
    filename: str,
    notification_id: str,
    validated: dict[str, Any],
    *,
    safe_filename,
    sha256_file,
    copy2=shutil.copy2,
    replace=os.replace,
    fsync=os.fsync,
    open_directory=os.open,
    close=os.close,
    getpid=os.getpid,
) -> Path:
    """Copy to private staging and expose the final APK only after full hash validation."""
    artifacts.mkdir(parents=True, exist_ok=True)
    clean_name = safe_filename(filename, "CoreWorker-debug.apk")
    if not clean_name.lower().endswith(".apk"):
        clean_name += ".apk"
    expected_sha = str(validated.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("sha256 validado do APK ausente ou inválido")

    candidate = artifacts / clean_name
    if candidate.exists() and sha256_file(candidate).lower() != expected_sha:
        suffix = safe_filename(str(notification_id or "")[:16], expected_sha[:12])
        candidate = artifacts / f"{candidate.stem}-{suffix}.apk"
        if candidate.exists() and sha256_file(candidate).lower() != expected_sha:
            candidate = artifacts / f"{candidate.stem}-{expected_sha[:12]}.apk"

    if candidate.is_file() and sha256_file(candidate).lower() == expected_sha:
        return candidate

    temp = candidate.with_name(f".{candidate.name}.{getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        temp.unlink()
    try:
        copy2(built_apk, temp)
        actual = sha256_file(temp).lower()
        if actual != expected_sha:
            raise ValueError(f"sha256 do artifact staged divergente: esperado={expected_sha}, recebido={actual}")
        replace(temp, candidate)
        try:
            directory_fd = open_directory(str(artifacts), os.O_RDONLY)
            try:
                fsync(directory_fd)
            finally:
                close(directory_fd)
        except OSError:
            pass
        return candidate
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def cleanup_private_builder_storage(
    builder: Path,
    current_apk: Path | None = None,
    *,
    safe_json_load,
    is_inside,
    tree_bytes,
    short,
    artifact_keep: int,
    log_keep: int,
    wall_time=time.time,
    rmtree=shutil.rmtree,
) -> dict[str, Any]:
    """Keep private builder storage bounded without touching the active toolchain/build."""
    result: dict[str, Any] = {"removed": 0, "removedBytes": 0, "keptApks": 0, "keptLogs": 0}
    try:
        artifacts = builder / "artifacts"
        latest = safe_json_load(artifacts / "latest-artifact.json")
        latest_path = Path(str(latest.get("artifact_path") or "")) if latest else None
        owner = safe_json_load(builder / ".apk-build-active/owner.json")
        active_work = Path(str(owner.get("work") or "")) if owner.get("work") else None
        active_log = Path(str(owner.get("log") or "")) if owner.get("log") else None
        apks = sorted(
            [path for path in artifacts.glob("*.apk") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if artifacts.is_dir() else []
        keep: set[Path] = set()
        for path in apks[:artifact_keep]:
            try:
                keep.add(path.resolve())
            except Exception:
                keep.add(path)
        if current_apk is not None and current_apk.is_file():
            try:
                keep.add(current_apk.resolve())
            except Exception:
                keep.add(current_apk)
        if latest_path is not None and latest_path.is_file() and is_inside(latest_path, builder):
            try:
                keep.add(latest_path.resolve())
            except Exception:
                keep.add(latest_path)
        for apk in apks:
            try:
                canonical = apk.resolve()
            except Exception:
                canonical = apk
            if canonical in keep:
                continue
            for item in (apk, apk.with_suffix(apk.suffix + ".json")):
                try:
                    if item.is_file():
                        size = item.stat().st_size
                        item.unlink()
                        result["removed"] += 1
                        result["removedBytes"] += size
                except Exception:
                    pass
        result["keptApks"] = len([path for path in apks if path.exists()])

        if artifacts.is_dir():
            for sidecar in artifacts.glob("*.apk.json"):
                apk = Path(str(sidecar)[:-5])
                if apk.is_file():
                    continue
                try:
                    size = sidecar.stat().st_size
                    sidecar.unlink()
                    result["removed"] += 1
                    result["removedBytes"] += size
                except Exception:
                    pass

        logs = builder / "logs"
        log_files = sorted(
            [path for path in logs.glob("*.log") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if logs.is_dir() else []
        for path in log_files[log_keep:]:
            if active_log is not None:
                try:
                    if path.resolve() == active_log.resolve():
                        continue
                except Exception:
                    pass
            try:
                size = path.stat().st_size
                path.unlink()
                result["removed"] += 1
                result["removedBytes"] += size
            except Exception:
                pass
        result["keptLogs"] = len([path for path in log_files if path.exists()])

        cutoff = wall_time() - 24 * 60 * 60
        work_root = builder / "work"
        if work_root.is_dir():
            for path in work_root.iterdir():
                if path.is_symlink() or not path.is_dir():
                    continue
                if active_work is not None:
                    try:
                        if path.resolve() == active_work.resolve():
                            continue
                    except Exception:
                        continue
                try:
                    if path.stat().st_mtime < cutoff:
                        size = tree_bytes(path)
                        rmtree(path)
                        result["removed"] += 1
                        result["removedBytes"] += size
                except Exception:
                    pass

        cancellations = builder / "cancellations"
        if cancellations.is_dir():
            for marker in cancellations.glob("*.request"):
                try:
                    if marker.is_file() and marker.stat().st_mtime < cutoff:
                        size = marker.stat().st_size
                        marker.unlink()
                        result["removed"] += 1
                        result["removedBytes"] += size
                except Exception:
                    pass
    except Exception as exc:
        result["warning"] = short(exc, 180)
    return result
