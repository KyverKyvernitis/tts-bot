from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 2
TERMINAL_STATUSES = {"success", "ok", "warn", "error", "done", "failed"}


class DeliveryError(RuntimeError):
    pass


def _utc_ts() -> float:
    return time.time()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DeliveryError("job não é um objeto JSON")
    return data


def _event_filename(event_key: str) -> str:
    digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:32]
    return f"{digest}.json"


def _layout(root: Path) -> dict[str, Path]:
    paths = {
        "pending": root / "pending",
        "sending": root / "sending",
        "delivered": root / "delivered",
        "dead": root / "dead-letter",
        "legacy": root / "legacy",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def migrate_legacy_jobs(root: Path, *, recover_recent_status: bool = False, recent_seconds: int = 1800) -> int:
    """Quarentena histórico antigo e converte apenas status finais muito recentes."""
    paths = _layout(root)
    moved = 0
    root_files = [path for path in root.iterdir() if path.is_file() and not path.name.startswith(".")]
    candidates = root_files + list(paths["pending"].glob("*.json"))
    for path in candidates:
        try:
            data = _read_json(path)
        except Exception:
            data = {}
        if data.get("schema_version") == SCHEMA_VERSION and data.get("event_key"):
            continue
        recovered = False
        if recover_recent_status:
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else None
            try:
                created_at = float(data.get("created_at") or 0) if data else 0
            except (TypeError, ValueError):
                created_at = 0
            age = _utc_ts() - created_at if created_at else recent_seconds + 1
            status = str(payload.get("status") or "").lower() if payload else ""
            if (
                payload
                and 0 <= age <= recent_seconds
                and payload.get("channel_id")
                and payload.get("message_id")
                and status in TERMINAL_STATUSES
            ):
                candidate = str(payload.get("candidate_id") or payload.get("display_id") or payload.get("message_id"))
                event_key = str(payload.get("event_key") or f"{candidate}:final-status:{status}")
                payload = dict(payload)
                payload["terminal"] = True
                payload["event_key"] = event_key
                filename = _event_filename(event_key)
                target = paths["pending"] / filename
                if not (paths["delivered"] / filename).exists() and not target.exists():
                    _atomic_write_json(target, {
                        "schema_version": SCHEMA_VERSION,
                        "kind": "status",
                        "event_key": event_key,
                        "created_at": created_at or _utc_ts(),
                        "updated_at": _utc_ts(),
                        "attempts": int(data.get("attempts") or 0),
                        "last_error": data.get("last_error"),
                        "data": payload,
                        "migrated_from_legacy": True,
                    })
                recovered = True
        destination = paths["legacy"] / f"{int(_utc_ts())}-{path.name}"
        try:
            os.replace(path, destination)
            moved += 1
        except FileNotFoundError:
            pass
    return moved


def enqueue_job(root: Path, *, kind: str, event_key: str, data: dict[str, Any]) -> str:
    event_key = str(event_key or "").strip()
    if not event_key:
        raise DeliveryError("event_key ausente")
    if kind not in {"status", "alert"}:
        raise DeliveryError("tipo de entrega inválido")
    paths = _layout(root)
    filename = _event_filename(event_key)
    delivered = paths["delivered"] / filename
    if delivered.exists():
        return "delivered"
    pending = paths["pending"] / filename
    sending = paths["sending"] / filename
    if sending.exists():
        return "sending"
    if kind == "alert":
        attachment = Path(str(data.get("attachment") or "").strip()) if data.get("attachment") else None
        if attachment and attachment.is_file():
            attachments = root / "attachments"
            attachments.mkdir(parents=True, exist_ok=True)
            suffix = attachment.suffix[:16]
            stored = attachments / f"{_event_filename(event_key).removesuffix('.json')}{suffix}"
            if not stored.exists():
                tmp_attachment = stored.with_name(f".{stored.name}.{os.getpid()}.tmp")
                shutil.copy2(attachment, tmp_attachment)
                os.replace(tmp_attachment, stored)
            data = dict(data)
            data["attachment"] = str(stored)
            if not data.get("attachment_name"):
                data["attachment_name"] = attachment.name

    now = _utc_ts()
    previous: dict[str, Any] = {}
    if pending.exists():
        try:
            previous = _read_json(pending)
        except Exception:
            previous = {}
    job = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "event_key": event_key,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "attempts": int(previous.get("attempts") or 0),
        "last_error": previous.get("last_error"),
        "data": data,
    }
    _atomic_write_json(pending, job)
    return "queued"


def recover_sending(root: Path, *, stale_seconds: int = 120) -> int:
    paths = _layout(root)
    recovered = 0
    now = _utc_ts()
    for path in paths["sending"].glob("*.json"):
        try:
            data = _read_json(path)
            claimed_at = float(data.get("claimed_at") or path.stat().st_mtime)
        except Exception:
            claimed_at = path.stat().st_mtime
        if now - claimed_at < stale_seconds:
            continue
        target = paths["pending"] / path.name
        try:
            os.replace(path, target)
            recovered += 1
        except FileNotFoundError:
            pass
    return recovered


def _claim(path: Path, sending_dir: Path) -> Path | None:
    target = sending_dir / path.name
    try:
        os.replace(path, target)
    except FileNotFoundError:
        return None
    try:
        job = _read_json(target)
        job["claimed_at"] = _utc_ts()
        _atomic_write_json(target, job)
    except Exception:
        pass
    return target


def _finish_job(path: Path, target_dir: Path, job: dict[str, Any], *, error: str | None = None) -> None:
    job["updated_at"] = _utc_ts()
    job.pop("claimed_at", None)
    if error:
        job["last_error"] = error[:1000]
    else:
        job["delivered_at"] = _utc_ts()
        job["last_error"] = None
    _atomic_write_json(path, job)
    os.replace(path, target_dir / path.name)


def _retry_job(path: Path, pending_dir: Path, dead_dir: Path, job: dict[str, Any], error: str, max_attempts: int) -> None:
    attempts = int(job.get("attempts") or 0) + 1
    job["attempts"] = attempts
    job["last_error"] = error[:1000]
    job["updated_at"] = _utc_ts()
    job.pop("claimed_at", None)
    _atomic_write_json(path, job)
    target_dir = dead_dir if attempts >= max_attempts else pending_dir
    os.replace(path, target_dir / path.name)


def _token_from_env(repo_dir: Path) -> str:
    env_path = repo_dir / ".env"
    try:
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if raw.startswith("BOT_INTERNAL_UPDATE_TOKEN="):
                return raw.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def send_status_http(payload: dict[str, Any], *, url: str, token: str = "", timeout: float = 7.0) -> None:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Update-Token"] = token
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
    if not isinstance(result, dict) or not result.get("ok"):
        raise DeliveryError(str(result.get("error") if isinstance(result, dict) else "resposta inválida"))


def send_alert_subprocess(data: dict[str, Any], *, alert_script: Path) -> None:
    args = [
        str(alert_script),
        str(data.get("type") or "info"),
        str(data.get("title") or "Atualização"),
        str(data.get("body") or ""),
    ]
    attachment = str(data.get("attachment") or "").strip()
    attachment_name = str(data.get("attachment_name") or "").strip()
    if attachment:
        args.extend([attachment, attachment_name])
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or f"alert.sh retornou {completed.returncode}").strip()
        raise DeliveryError(message)


@dataclass(frozen=True)
class FlushResult:
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    legacy: int = 0


def flush_jobs(
    root: Path,
    *,
    kind: str,
    sender: Callable[[dict[str, Any]], None],
    limit: int = 20,
    max_attempts: int = 100,
    stale_seconds: int = 120,
) -> FlushResult:
    paths = _layout(root)
    legacy = migrate_legacy_jobs(root, recover_recent_status=(kind == "status"))
    recover_sending(root, stale_seconds=stale_seconds)
    delivered = retried = dead = 0
    for pending in sorted(paths["pending"].glob("*.json"))[: max(1, limit)]:
        claimed = _claim(pending, paths["sending"])
        if claimed is None:
            continue
        try:
            job = _read_json(claimed)
            if job.get("schema_version") != SCHEMA_VERSION or job.get("kind") != kind or not job.get("event_key"):
                raise DeliveryError("job incompatível ou corrompido")
            payload = job.get("data")
            if not isinstance(payload, dict):
                raise DeliveryError("dados do job ausentes")
            sender(payload)
            _finish_job(claimed, paths["delivered"], job)
            delivered += 1
        except Exception as exc:
            try:
                job = _read_json(claimed)
            except Exception:
                job = {"attempts": max_attempts - 1}
            before = int(job.get("attempts") or 0)
            _retry_job(claimed, paths["pending"], paths["dead"], job, f"{type(exc).__name__}: {exc}", max_attempts)
            if before + 1 >= max_attempts:
                dead += 1
            else:
                retried += 1
    return FlushResult(delivered=delivered, retried=retried, dead=dead, legacy=legacy)


def prune_jobs(root: Path, *, delivered_days: int = 7, dead_days: int = 30, legacy_days: int = 30) -> None:
    paths = _layout(root)
    now = _utc_ts()
    policies = ((paths["delivered"], delivered_days), (paths["dead"], dead_days), (paths["legacy"], legacy_days))
    for directory, days in policies:
        threshold = now - max(1, days) * 86400
        for path in directory.glob("*.json"):
            try:
                if path.stat().st_mtime < threshold:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    attachments = root / "attachments"
    if attachments.is_dir():
        referenced: set[str] = set()
        for directory in (paths["pending"], paths["sending"]):
            for job_path in directory.glob("*.json"):
                try:
                    job = _read_json(job_path)
                    data = job.get("data") if isinstance(job.get("data"), dict) else {}
                    attachment = str(data.get("attachment") or "").strip()
                    if attachment:
                        referenced.add(str(Path(attachment).resolve(strict=False)))
                except Exception:
                    continue
        threshold = now - max(1, delivered_days) * 86400
        for attachment in attachments.iterdir():
            if not attachment.is_file():
                continue
            try:
                resolved = str(attachment.resolve(strict=False))
                if resolved not in referenced and attachment.stat().st_mtime < threshold:
                    attachment.unlink(missing_ok=True)
            except OSError:
                continue


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Entrega idempotente de status/logs do updater")
    sub = parser.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--root", required=True)
    enqueue.add_argument("--kind", choices=("status", "alert"), required=True)
    enqueue.add_argument("--event-key", required=True)
    enqueue.add_argument("--data-json", required=True)

    flush_status = sub.add_parser("flush-status")
    flush_status.add_argument("--root", required=True)
    flush_status.add_argument("--url", required=True)
    flush_status.add_argument("--repo-dir", required=True)
    flush_status.add_argument("--limit", type=int, default=20)

    flush_alert = sub.add_parser("flush-alert")
    flush_alert.add_argument("--root", required=True)
    flush_alert.add_argument("--alert-script", required=True)
    flush_alert.add_argument("--limit", type=int, default=20)

    migrate = sub.add_parser("migrate-legacy")
    migrate.add_argument("--root", required=True)

    prune = sub.add_parser("prune")
    prune.add_argument("--root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "enqueue":
        data = json.loads(args.data_json)
        if not isinstance(data, dict):
            raise DeliveryError("data-json deve ser objeto")
        print(enqueue_job(Path(args.root), kind=args.kind, event_key=args.event_key, data=data))
        return 0
    if args.command == "flush-status":
        repo_dir = Path(args.repo_dir)
        token = _token_from_env(repo_dir)
        result = flush_jobs(
            Path(args.root),
            kind="status",
            limit=args.limit,
            sender=lambda payload: send_status_http(payload, url=args.url, token=token),
        )
        print(json.dumps(result.__dict__, sort_keys=True))
        return 0
    if args.command == "flush-alert":
        result = flush_jobs(
            Path(args.root),
            kind="alert",
            limit=args.limit,
            sender=lambda payload: send_alert_subprocess(payload, alert_script=Path(args.alert_script)),
        )
        print(json.dumps(result.__dict__, sort_keys=True))
        return 0
    if args.command == "migrate-legacy":
        print(migrate_legacy_jobs(Path(args.root)))
        return 0
    if args.command == "prune":
        prune_jobs(Path(args.root))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
