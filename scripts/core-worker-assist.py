#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utility.commands.workers_registry import CoreWorkerRegistryError, get_core_workers_registry  # noqa: E402


def _short(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def _local_log_summary(text: str, *, max_recent: int = 18, max_top: int = 12) -> dict[str, Any]:
    patterns = {
        "critical": r"\bcritical\b|\bfatal\b|crítico|critico",
        "error": r"\berror\b|\berro\b",
        "warning": r"\bwarning\b|\bwarn\b|aviso",
        "timeout": r"timeout|timed out|tempo esgotado",
        "traceback": r"traceback",
        "failed": r"failed|failure|falhou|falha",
        "restart": r"restart|restarting|started|stopped|iniciando|parando",
        "phone_worker": r"phone-worker|core-worker|worker",
    }
    compiled = {key: re.compile(expr, re.I) for key, expr in patterns.items()}
    lines = text.splitlines()
    counts = {key: 0 for key in compiled}
    important: list[str] = []
    grouped: Counter[str] = Counter()
    for line in lines:
        hit = False
        for key, regex in compiled.items():
            if regex.search(line):
                counts[key] += 1
                hit = True
        if hit:
            important.append(line.strip())
            normalized = re.sub(r"\b\d{15,22}\b", "<snowflake>", line.strip())
            normalized = re.sub(r"\s+", " ", normalized)[:220]
            grouped[normalized] += 1
    return {
        "ok": True,
        "fallback_local": True,
        "summary": "logs resumidos localmente na VPS",
        "bytes": len(text.encode("utf-8")),
        "lines": len(lines),
        "important_count": len(important),
        "counts": counts,
        "recent": important[-max_recent:],
        "top_messages": [{"message": msg, "count": count} for msg, count in grouped.most_common(max_top)],
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _read_tail(path: Path, max_bytes: int) -> str:
    raw = path.read_bytes()[-max_bytes:]
    return raw.decode("utf-8", errors="replace")


def _job_done(job: dict[str, Any] | None) -> bool:
    return isinstance(job, dict) and str(job.get("status") or "").lower() in {"succeeded", "failed", "expired"}


def _wait_job(job_id: str, timeout: float) -> dict[str, Any] | None:
    registry = get_core_workers_registry()
    deadline = time.monotonic() + max(0.5, timeout)
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            data = registry.get_job(job_id)
            job = data.get("job") if isinstance(data, dict) else None
            if isinstance(job, dict):
                last = job
                if _job_done(job):
                    return job
        except Exception:
            pass
        time.sleep(1.0)
    return last


def queue_and_wait(job_type: str, payload: dict[str, Any], *, required_capabilities: list[str], summary: str, timeout: float) -> dict[str, Any]:
    registry = get_core_workers_registry()
    try:
        created = registry.create_job(
            job_type=job_type,
            payload=payload,
            created_by_id=0,
            created_by_name="VPS assist",
            required_capabilities=required_capabilities,
            ttl_seconds=max(60, int(timeout) + 90),
            lease_seconds=max(30, int(timeout) + 30),
            max_attempts=1,
            summary=summary,
        )
    except CoreWorkerRegistryError as exc:
        return {"ok": False, "queued": False, "fallback_recommended": True, "error": str(exc), "status": exc.status}
    job = created.get("job") if isinstance(created, dict) else {}
    job_id = str((job or {}).get("job_id") or "")
    final = _wait_job(job_id, timeout=timeout) if job_id else None
    return {"ok": bool(final and final.get("status") == "succeeded"), "queued": True, "job_id": job_id, "job": final or job}


def cmd_probe(args: argparse.Namespace) -> dict[str, Any]:
    return queue_and_wait("vps_assist_probe", {}, required_capabilities=["vps-assist"], summary="probe auxiliar da VPS", timeout=args.timeout)


def cmd_log_summary(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.file).expanduser()
    text = _read_tail(path, args.max_bytes)
    payload = {"text": text, "max_recent": args.max_recent, "max_top": args.max_top, "source": str(path)}
    result = queue_and_wait("log_digest", payload, required_capabilities=["vps-assist"], summary=f"resumo remoto de {path.name}", timeout=args.timeout)
    if result.get("ok"):
        return result
    local = _local_log_summary(text, max_recent=args.max_recent, max_top=args.max_top)
    local["worker_result"] = result
    return local


def cmd_zip_validate(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.file).expanduser()
    raw = path.read_bytes()
    if len(raw) > args.max_bytes:
        return {"ok": False, "error": "ZIP grande demais para enviar ao worker", "bytes": len(raw)}
    payload = {"filename": path.name, "data_b64": base64.b64encode(raw).decode("ascii"), "max_entries": 1200, "max_preview": 60, "source": str(path)}
    result = queue_and_wait("zip_audit", payload, required_capabilities=["vps-assist"], summary=f"auditoria remota de {path.name}", timeout=args.timeout)
    if result.get("ok"):
        return result
    return {"ok": False, "worker_result": result, "fallback_recommended": True, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Usa Core Workers como aceleradores opcionais da VPS, com fallback local.")
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--timeout", type=float, default=10.0)
    log = sub.add_parser("log-summary")
    log.add_argument("file")
    log.add_argument("--timeout", type=float, default=14.0)
    log.add_argument("--max-bytes", type=int, default=220_000)
    log.add_argument("--max-recent", type=int, default=18)
    log.add_argument("--max-top", type=int, default=14)
    zval = sub.add_parser("zip-validate")
    zval.add_argument("file")
    zval.add_argument("--timeout", type=float, default=18.0)
    zval.add_argument("--max-bytes", type=int, default=24 * 1024 * 1024)
    args = parser.parse_args()
    if args.command == "probe":
        result = cmd_probe(args)
    elif args.command == "log-summary":
        result = cmd_log_summary(args)
    elif args.command == "zip-validate":
        result = cmd_zip_validate(args)
    else:
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
