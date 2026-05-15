#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
from collections import Counter
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

START_TIME = time.time()
JOBS_STARTED = 0
JOBS_FAILED = 0

DEFAULT_MAX_BODY_MB = 32
DEFAULT_MAX_OUTPUT_MB = 32
DEFAULT_TIMEOUT_SECONDS = 45
PHONE_WORKER_VERSION = "1.2.0"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30



def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def _load_env_file(path: str | None = None) -> None:
    """Carrega ~/.phone-worker.env sem sobrescrever variáveis já exportadas.

    Isso deixa o worker funcionar mesmo quando o script de start roda dentro do
    tmux sem exportar todas as variáveis CORE_WORKER_*.
    """
    raw_path = path or os.getenv("PHONE_WORKER_ENV") or str(Path.home() / ".phone-worker.env")
    env_path = Path(raw_path).expanduser()
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return
    for line in lines:
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        if clean.startswith("export "):
            clean = clean[len("export "):].strip()
        key, value = clean.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except Exception:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    default = list(default or [])
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    items: list[str] = []
    for item in raw.replace(";", ",").split(","):
        clean = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", item.strip().lower()).strip("-._:")
        if clean and clean not in items:
            items.append(clean[:40])
    return items or default


def _short_text(value: Any, *, limit: int = 120, default: str = "") -> str:
    text = str(value or default).replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _run_json_command(command: list[str], *, timeout: float = 2.0) -> dict[str, Any]:
    if not command or not shutil.which(command[0]):
        return {}
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        parsed = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _battery_snapshot() -> dict[str, Any]:
    raw = _run_json_command(["termux-battery-status"], timeout=2.0)
    if not raw:
        return {}
    level = raw.get("percentage")
    charging = None
    status = str(raw.get("status") or "").strip().lower()
    plugged = str(raw.get("plugged") or "").strip().lower()
    if status:
        charging = status in {"charging", "full"}
    elif plugged:
        charging = plugged not in {"unplugged", "none", "unknown"}
    result: dict[str, Any] = {}
    try:
        if level is not None:
            result["level"] = int(float(level))
    except Exception:
        pass
    if charging is not None:
        result["charging"] = bool(charging)
    if status:
        result["status"] = status[:32]
    if plugged:
        result["plugged"] = plugged[:32]
    return result


def _network_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {}
    wifi = _run_json_command(["termux-wifi-connectioninfo"], timeout=2.0)
    if wifi:
        result["type"] = "wifi"
        ssid = str(wifi.get("ssid") or "").strip()
        if ssid and ssid != "<unknown ssid>":
            result["name"] = _short_text(ssid, limit=48)
        try:
            result["rssi"] = int(wifi.get("rssi"))
        except Exception:
            pass
    else:
        result["type"] = "unknown"
    tailscale_ip = ""
    if shutil.which("tailscale"):
        try:
            proc = subprocess.run(["tailscale", "ip", "-4"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2.0)
            if proc.returncode == 0:
                tailscale_ip = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[0].strip()
        except Exception:
            tailscale_ip = ""
    result["tailscale"] = bool(tailscale_ip)
    if tailscale_ip:
        parts = tailscale_ip.split(".")
        if len(parts) == 4:
            result["tailscale_ip_masked"] = f"{parts[0]}.{parts[1]}.x.x"
    return result


def _heartbeat_configured() -> bool:
    if not _env_bool("CORE_WORKER_HEARTBEAT_ENABLED", True):
        return False
    return bool(
        str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip()
        and str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
        and str(os.getenv("CORE_WORKER_TOKEN") or "").strip()
    )


def _core_worker_payload(*, host: str, port: int) -> dict[str, Any]:
    status = _system_status()
    worker_id = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    name = str(os.getenv("CORE_WORKER_NAME") or os.getenv("PHONE_WORKER_NAME") or platform.node() or "Core Phone Worker").strip()
    endpoint = str(os.getenv("CORE_WORKER_ENDPOINT") or os.getenv("PHONE_WORKER_ENDPOINT") or "").strip()
    if not endpoint and host not in {"", "0.0.0.0", "::"}:
        endpoint = f"http://{host}:{port}"
    roles = _env_list("CORE_WORKER_ROLES", ["phone-worker", "diagnostics", "log-summary", "zip-validate", "tts-convert"])
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", roles + ["ffmpeg", "ffprobe"])
    if status.get("ffmpeg") and "ffmpeg" not in capabilities:
        capabilities.append("ffmpeg")
    if status.get("ffprobe") and "ffprobe" not in capabilities:
        capabilities.append("ffprobe")
    return {
        "worker_id": worker_id,
        "name": _short_text(name, limit=64, default="Core Phone Worker"),
        "source": "termux-phone-worker",
        "version": PHONE_WORKER_VERSION,
        "endpoint": endpoint,
        "roles": roles[:16],
        "capabilities": capabilities[:24],
        "battery": _battery_snapshot(),
        "network": _network_snapshot(),
        "health": {
            "ok": True,
            "pid": status.get("pid"),
            "uptime_seconds": status.get("uptime_seconds"),
            "jobs_started": status.get("jobs_started"),
            "jobs_failed": status.get("jobs_failed"),
            "ffmpeg": status.get("ffmpeg"),
            "ffprobe": status.get("ffprobe"),
        },
        "status": {
            "http_host": host,
            "http_port": port,
            "python": status.get("python"),
            "platform": status.get("platform"),
            "disk_home": status.get("disk_home"),
            "loadavg": status.get("loadavg"),
        },
    }


def _send_core_worker_heartbeat_once(*, host: str, port: int, timeout: float = 6.0) -> bool:
    base_url = str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip().rstrip("/")
    token = str(os.getenv("CORE_WORKER_TOKEN") or "").strip()
    worker_id = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    if not base_url or not token or not worker_id:
        return False
    payload = _core_worker_payload(host=host, port=port)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/core-worker/heartbeat",
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            resp.read(4096)
        return True
    except urllib.error.HTTPError as exc:
        # Nunca loga token. O corpo pode conter só erro do registry.
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:180]
        except Exception:
            detail = ""
        print(f"[core-worker-heartbeat] HTTP {exc.code}: {_short_text(detail, limit=180)}", flush=True)
    except Exception as exc:
        print(f"[core-worker-heartbeat] falhou: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)
    return False


def _start_core_worker_heartbeat(*, host: str, port: int) -> None:
    if not _heartbeat_configured():
        print("[core-worker-heartbeat] desativado ou incompleto; defina CORE_WORKER_VPS_URL, CORE_WORKER_ID e CORE_WORKER_TOKEN", flush=True)
        return
    interval = max(10.0, min(300.0, _env_float("CORE_WORKER_HEARTBEAT_INTERVAL_SECONDS", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)))

    def loop() -> None:
        while True:
            _send_core_worker_heartbeat_once(host=host, port=port, timeout=6.0)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="core-worker-heartbeat", daemon=True)
    thread.start()
    print(f"[core-worker-heartbeat] ativo; intervalo={int(interval)}s", flush=True)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _json_response(handler, status, {"ok": False, "error": message})


def _b64decode(value: str, *, max_bytes: int) -> bytes:
    if not value:
        return b""
    # Base64 inflates data by ~33%; fail early on clearly huge payloads.
    if len(value) > int(max_bytes * 1.45) + 64:
        raise ValueError("payload base64 grande demais")
    data = base64.b64decode(value.encode("ascii"), validate=True)
    if len(data) > max_bytes:
        raise ValueError("payload grande demais")
    return data


def _b64encode(data: bytes, *, max_bytes: int) -> str:
    if len(data) > max_bytes:
        raise ValueError("resultado grande demais")
    return base64.b64encode(data).decode("ascii")


def _safe_name(name: Any, fallback: str = "file.bin") -> str:
    text = str(name or fallback).replace("\\", "/").strip().lstrip("/")
    parts = []
    for part in text.split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        parts.append(part[:120])
    return "/".join(parts) or fallback


def _system_status() -> dict[str, Any]:
    disk = shutil.disk_usage(Path.home())
    load = None
    try:
        load = os.getloadavg()
    except Exception:
        load = None
    return {
        "ok": True,
        "worker": "phone-worker",
        "version": PHONE_WORKER_VERSION,
        "core_worker_heartbeat": _heartbeat_configured(),
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - START_TIME, 3),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "jobs_started": JOBS_STARTED,
        "jobs_failed": JOBS_FAILED,
        "loadavg": list(load) if load else None,
        "disk_home": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
        },
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
    }


class WorkerHandler(BaseHTTPRequestHandler):
    server_version = "PhoneWorker/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet default HTTP noise
        if _env_bool("PHONE_WORKER_HTTP_LOGS", False):
            super().log_message(fmt, *args)

    @property
    def token(self) -> str:
        return str(getattr(self.server, "worker_token", "") or "")

    @property
    def max_body_bytes(self) -> int:
        return int(getattr(self.server, "max_body_bytes", DEFAULT_MAX_BODY_MB * 1024 * 1024))

    @property
    def max_output_bytes(self) -> int:
        return int(getattr(self.server, "max_output_bytes", DEFAULT_MAX_OUTPUT_MB * 1024 * 1024))

    @property
    def job_timeout(self) -> int:
        return int(getattr(self.server, "job_timeout", DEFAULT_TIMEOUT_SECONDS))

    def _authorized(self) -> bool:
        expected = self.token
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        custom = self.headers.get("X-Phone-Worker-Token", "")
        return auth == f"Bearer {expected}" or custom == expected

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        _error(self, HTTPStatus.FORBIDDEN, "token inválido")
        return False

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except Exception:
            _error(self, HTTPStatus.LENGTH_REQUIRED, "Content-Length inválido")
            return None
        if length <= 0:
            return {}
        if length > self.max_body_bytes:
            _error(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "requisição grande demais")
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            _error(self, HTTPStatus.BAD_REQUEST, f"JSON inválido: {type(exc).__name__}")
            return None
        if not isinstance(parsed, dict):
            _error(self, HTTPStatus.BAD_REQUEST, "JSON precisa ser objeto")
            return None
        return parsed

    def do_GET(self) -> None:
        if self.path not in {"/", "/health", "/status"}:
            _error(self, HTTPStatus.NOT_FOUND, "rota não encontrada")
            return
        if not self._require_auth():
            return
        _json_response(self, HTTPStatus.OK, _system_status())

    def do_POST(self) -> None:
        global JOBS_STARTED, JOBS_FAILED
        if self.path != "/task":
            _error(self, HTTPStatus.NOT_FOUND, "rota não encontrada")
            return
        if not self._require_auth():
            return
        body = self._read_json()
        if body is None:
            return

        task = str(body.get("task") or "").strip().lower()
        JOBS_STARTED += 1
        try:
            if task in {"ping", "health"}:
                payload = _system_status()
            elif task == "sha256":
                payload = self._task_sha256(body)
            elif task == "zip":
                payload = self._task_zip(body)
            elif task == "zip_validate":
                payload = self._task_zip_validate(body)
            elif task == "maintenance_plan":
                payload = self._task_maintenance_plan(body)
            elif task == "text_stats":
                payload = self._task_text_stats(body)
            elif task == "log_extract":
                payload = self._task_log_extract(body)
            elif task == "log_summary":
                payload = self._task_log_summary(body)
            elif task == "ffprobe_media":
                payload = self._task_ffprobe_media(body)
            elif task == "ffmpeg_convert":
                payload = self._task_ffmpeg_convert(body)
            else:
                raise ValueError("task não suportada")
            payload.setdefault("ok", True)
            _json_response(self, HTTPStatus.OK, payload)
        except Exception as exc:
            JOBS_FAILED += 1
            _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")

    def _task_sha256(self, body: dict[str, Any]) -> dict[str, Any]:
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        return {"ok": True, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def _task_zip(self, body: dict[str, Any]) -> dict[str, Any]:
        files = body.get("files") or []
        if not isinstance(files, list) or not files:
            raise ValueError("files vazio")
        if len(files) > 80:
            raise ValueError("arquivos demais")
        compression = zipfile.ZIP_DEFLATED
        level = max(1, min(9, int(body.get("compresslevel") or 6)))
        total_in = 0
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=compression, compresslevel=level) as zf:
            for index, item in enumerate(files, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"files[{index}] inválido")
                name = _safe_name(item.get("name"), fallback=f"file-{index}.bin")
                data = _b64decode(str(item.get("data_b64") or ""), max_bytes=self.max_body_bytes)
                total_in += len(data)
                if total_in > self.max_body_bytes:
                    raise ValueError("entrada total grande demais")
                zf.writestr(name, data)
        data_out = output.getvalue()
        return {
            "ok": True,
            "filename": _safe_name(body.get("filename"), fallback="phone-worker.zip"),
            "input_size": total_in,
            "size": len(data_out),
            "data_b64": _b64encode(data_out, max_bytes=self.max_output_bytes),
        }

    def _task_zip_validate(self, body: dict[str, Any]) -> dict[str, Any]:
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        filename = _safe_name(body.get("filename"), fallback="update.zip")
        max_entries = max(1, min(2000, int(body.get("max_entries") or 600)))
        max_preview = max(1, min(80, int(body.get("max_preview") or 30)))
        warnings: list[str] = []
        errors: list[str] = []
        extensions: Counter[str] = Counter()
        total_uncompressed = 0
        file_count = 0
        dir_count = 0
        py_files: list[str] = []
        shell_files: list[str] = []
        large_files: list[dict[str, Any]] = []
        assets = 0
        manifests = 0
        service_files = 0
        script_files = 0
        top_level: Counter[str] = Counter()
        preview: list[str] = []

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                bad = zf.testzip()
                if bad:
                    errors.append(f"arquivo corrompido no ZIP: {bad}")
                infos = zf.infolist()
                if len(infos) > max_entries:
                    warnings.append(f"muitos itens no ZIP: {len(infos)}")
                for info in infos:
                    raw_name = str(info.filename or "")
                    normalized = raw_name.replace("\\", "/").lstrip("/")
                    parts = [part for part in normalized.split("/") if part]
                    if not parts:
                        continue
                    top_level[parts[0][:80]] += 1
                    if len(preview) < max_preview:
                        preview.append(normalized[:240])
                    if normalized.startswith("/") or any(part == ".." for part in parts):
                        errors.append(f"caminho inseguro: {raw_name}")
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        errors.append(f"symlink não permitido: {raw_name}")
                    if info.is_dir():
                        dir_count += 1
                        continue
                    file_count += 1
                    total_uncompressed += int(info.file_size or 0)
                    suffix = Path(parts[-1]).suffix.lower() or "<sem_ext>"
                    extensions[suffix] += 1
                    path_lc = normalized.lower()
                    if suffix == ".py":
                        py_files.append(normalized)
                    elif suffix in {".sh", ".bash", ".zsh"}:
                        shell_files.append(normalized)
                    if "/assets/" in f"/{path_lc}" or path_lc.startswith("assets/") or "/public/" in f"/{path_lc}":
                        assets += 1
                    if "manifest" in path_lc or path_lc.endswith(("package-lock.json", "pnpm-lock.yaml", "yarn.lock")):
                        manifests += 1
                    if path_lc.startswith("deploy/systemd/") or path_lc.endswith(".service") or path_lc.endswith(".timer"):
                        service_files += 1
                    if suffix in {".sh", ".py"} or path_lc.startswith("scripts/"):
                        script_files += 1
                    if int(info.file_size or 0) >= 1024 * 1024:
                        large_files.append({"path": normalized[:240], "size": int(info.file_size or 0)})
                    if total_uncompressed > self.max_output_bytes * 6:
                        warnings.append("tamanho descompactado muito alto para validação leve")
                        break
        except zipfile.BadZipFile:
            raise ValueError("ZIP inválido")

        large_files.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        risk = "ok"
        if errors:
            risk = "blocked"
        elif warnings or large_files or service_files:
            risk = "review"
        return {
            "ok": not errors,
            "filename": filename,
            "risk": risk,
            "size": len(data),
            "files": file_count,
            "dirs": dir_count,
            "total_uncompressed": total_uncompressed,
            "extensions": dict(extensions.most_common(20)),
            "top_level": dict(top_level.most_common(12)),
            "python_files": len(py_files),
            "shell_files": len(shell_files),
            "assets": assets,
            "manifests": manifests,
            "service_files": service_files,
            "script_files": script_files,
            "large_files": large_files[:12],
            "preview": preview,
            "warnings": warnings[:20],
            "errors": errors[:20],
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def _task_maintenance_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        entries = body.get("entries") or []
        if not isinstance(entries, list):
            raise ValueError("entries precisa ser lista")
        max_entries = max(1, min(5000, int(body.get("max_entries") or 1000)))
        now = float(body.get("now") or time.time())
        scanned = 0
        total_size = 0
        by_kind: dict[str, dict[str, Any]] = {}
        old_temp: list[dict[str, Any]] = []
        old_logs: list[dict[str, Any]] = []
        largest: list[dict[str, Any]] = []
        for item in entries[:max_entries]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")[:260]
            kind = str(item.get("kind") or "other")[:40]
            try:
                size = int(item.get("size") or 0)
            except Exception:
                size = 0
            try:
                raw_mtime = item.get("mtime")
                mtime = float(now if raw_mtime is None else raw_mtime)
            except Exception:
                mtime = now
            age_seconds = max(0, int(now - mtime))
            scanned += 1
            total_size += max(0, size)
            bucket = by_kind.setdefault(kind, {"count": 0, "size": 0})
            bucket["count"] += 1
            bucket["size"] += max(0, size)
            record = {"path": path, "size": size, "age_seconds": age_seconds, "kind": kind}
            largest.append(record)
            path_lc = path.lower()
            if kind in {"tmp_audio", "cache", "temp"} or "tmp_audio" in path_lc or "/cache/" in path_lc:
                if age_seconds >= 3600:
                    old_temp.append(record)
            if kind == "log" or path_lc.endswith((".log", ".txt")):
                if age_seconds >= 7 * 86400:
                    old_logs.append(record)
        largest.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        old_temp.sort(key=lambda item: (int(item.get("age_seconds") or 0), int(item.get("size") or 0)), reverse=True)
        old_logs.sort(key=lambda item: (int(item.get("age_seconds") or 0), int(item.get("size") or 0)), reverse=True)
        reclaimable_temp = sum(int(item.get("size") or 0) for item in old_temp)
        reclaimable_logs = sum(int(item.get("size") or 0) for item in old_logs)
        return {
            "ok": True,
            "scanned": scanned,
            "total_size": total_size,
            "by_kind": by_kind,
            "largest": largest[:30],
            "old_temp_candidates": old_temp[:80],
            "old_log_candidates": old_logs[:80],
            "estimated_reclaimable": reclaimable_temp + reclaimable_logs,
            "estimated_reclaimable_temp": reclaimable_temp,
            "estimated_reclaimable_logs": reclaimable_logs,
        }

    def _task_text_stats(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "")
        if len(text.encode("utf-8")) > self.max_body_bytes:
            raise ValueError("texto grande demais")
        lines = text.splitlines()
        words = text.split()
        return {
            "ok": True,
            "bytes": len(text.encode("utf-8")),
            "chars": len(text),
            "lines": len(lines),
            "words": len(words),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def _task_log_extract(self, body: dict[str, Any]) -> dict[str, Any]:
        import re
        text = str(body.get("text") or "")
        pattern = str(body.get("pattern") or r"error|exception|traceback|falhou|failed|fatal|timeout")
        max_lines = max(1, min(500, int(body.get("max_lines") or 120)))
        flags = re.IGNORECASE
        regex = re.compile(pattern, flags)
        matches = [line for line in text.splitlines() if regex.search(line)]
        trimmed = matches[-max_lines:]
        return {"ok": True, "matches": trimmed, "count": len(matches), "returned": len(trimmed)}


    @staticmethod
    def _normalize_log_message(line: str) -> str:
        text = str(line or "")
        # Remove prefixos comuns de journal/systemd e dados muito voláteis para agrupar melhor.
        text = re.sub(r"^\d{4}-\d{2}-\d{2}[T\s][^\s]+\s+", "", text)
        text = re.sub(r"^[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+", "", text)
        text = re.sub(r"^[\w.\-]+\s+", "", text, count=1)
        text = re.sub(r"^[\w@./+\-]+(?:\[\d+\])?:\s*", "", text)
        text = re.sub(r"\bguild=\d+\b", "guild=<id>", text)
        text = re.sub(r"\bchannel=\d+\b", "channel=<id>", text)
        text = re.sub(r"\buser=\d+\b", "user=<id>", text)
        text = re.sub(r"\b\d{15,22}\b", "<snowflake>", text)
        text = re.sub(r"\bpid=\d+\b|\[\d+\]", "[pid]", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:220] or "linha vazia"

    def _task_log_summary(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "")
        if len(text.encode("utf-8")) > self.max_body_bytes:
            raise ValueError("texto grande demais")
        max_recent = max(1, min(80, int(body.get("max_recent") or 12)))
        max_top = max(1, min(40, int(body.get("max_top") or 12)))
        lines = text.splitlines()
        patterns = {
            "critical": r"\bcritical\b|\bcritico\b|\bcrítico\b|\bfatal\b",
            "error": r"\berror\b|\berro\b",
            "warning": r"\bwarning\b|\bwarn\b|\baviso\b",
            "timeout": r"timeout|timed out|tempo esgotado",
            "traceback": r"traceback",
            "exception": r"exception|exce[cç][aã]o",
            "failed": r"failed|falhou|failure|falha",
            "restart": r"restart|restarting|started|stopped|iniciando|parando",
            "syntax": r"syntaxerror|indentationerror|taberror",
            "import": r"importerror|modulenotfounderror|extensionfailed|extensionnotfound",
            "lavalink": r"lavalink|lavasrc|trackexception|loadexception",
            "yt_dlp": r"yt[-_ ]?dlp|youtube|googlevideo",
            "rate_limit": r"rate.?limit|too many requests|429",
            "phone_worker": r"phone-worker|phone_lavalink|phone-lavalink",
        }
        compiled = {key: re.compile(pattern, re.IGNORECASE) for key, pattern in patterns.items()}
        counts = {key: 0 for key in compiled}
        important: list[str] = []
        grouped: Counter[str] = Counter()
        for line in lines:
            hit = False
            for key, regex in compiled.items():
                if regex.search(line or ""):
                    counts[key] += 1
                    hit = True
            if hit:
                important.append(line.strip())
                grouped[self._normalize_log_message(line)] += 1
        top_messages = [
            {"message": message, "count": count}
            for message, count in grouped.most_common(max_top)
        ]
        return {
            "ok": True,
            "bytes": len(text.encode("utf-8")),
            "lines": len(lines),
            "important_count": len(important),
            "counts": counts,
            "recent": important[-max_recent:],
            "top_messages": top_messages,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def _task_ffprobe_media(self, body: dict[str, Any]) -> dict[str, Any]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe não instalado no celular")
        input_ext = str(body.get("input_ext") or "bin").strip(". /\\")[:12] or "bin"
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        timeout = max(3, min(self.job_timeout, int(body.get("timeout_seconds") or min(self.job_timeout, 20))))
        with tempfile.TemporaryDirectory(prefix="phone-worker-ffprobe-") as tmp:
            src = Path(tmp) / f"input.{input_ext}"
            src.write_bytes(data)
            cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="ignore")[-800:]
                raise RuntimeError(f"ffprobe falhou: {err}")
            parsed = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
        streams = []
        for stream in parsed.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            streams.append({
                "index": stream.get("index"),
                "type": stream.get("codec_type"),
                "codec": stream.get("codec_name"),
                "duration": stream.get("duration"),
                "channels": stream.get("channels"),
                "sample_rate": stream.get("sample_rate"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "bit_rate": stream.get("bit_rate"),
            })
        fmt = parsed.get("format") if isinstance(parsed.get("format"), dict) else {}
        return {
            "ok": True,
            "input_size": len(data),
            "format": {
                "name": fmt.get("format_name"),
                "duration": fmt.get("duration"),
                "size": fmt.get("size"),
                "bit_rate": fmt.get("bit_rate"),
            },
            "streams": streams,
        }

    def _task_ffmpeg_convert(self, body: dict[str, Any]) -> dict[str, Any]:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg não instalado no celular")
        input_ext = str(body.get("input_ext") or "bin").strip(". /\\")[:12] or "bin"
        output_ext = str(body.get("output_ext") or "ogg").strip(". /\\")[:12] or "ogg"
        args = body.get("ffmpeg_args")
        if not isinstance(args, list) or not args:
            if output_ext in {"ogg", "opus"}:
                args = ["-vn", "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1"]
                output_ext = "ogg"
            elif output_ext == "mp3":
                args = ["-vn", "-c:a", "libmp3lame", "-b:a", "96k"]
            else:
                args = ["-vn"]
        safe_args = [str(part) for part in args if str(part) not in {";", "&&", "||"}]
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        timeout = max(3, min(self.job_timeout, int(body.get("timeout_seconds") or self.job_timeout)))
        with tempfile.TemporaryDirectory(prefix="phone-worker-ffmpeg-") as tmp:
            src = Path(tmp) / f"input.{input_ext}"
            dst = Path(tmp) / f"output.{output_ext}"
            src.write_bytes(data)
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *safe_args, str(dst)]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="ignore")[-800:]
                raise RuntimeError(f"ffmpeg falhou: {err}")
            out = dst.read_bytes()
        return {
            "ok": True,
            "output_ext": output_ext,
            "input_size": len(data),
            "size": len(out),
            "data_b64": _b64encode(out, max_bytes=self.max_output_bytes),
        }


def main() -> int:
    _load_env_file()
    parser = argparse.ArgumentParser(description="Worker auxiliar do celular para tarefas opcionais da VPS.")
    parser.add_argument("--host", default=os.getenv("PHONE_WORKER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_int("PHONE_WORKER_PORT", 8766))
    parser.add_argument("--token", default=os.getenv("PHONE_WORKER_TOKEN", ""))
    parser.add_argument("--max-body-mb", type=int, default=_env_int("PHONE_WORKER_MAX_BODY_MB", DEFAULT_MAX_BODY_MB))
    parser.add_argument("--max-output-mb", type=int, default=_env_int("PHONE_WORKER_MAX_OUTPUT_MB", DEFAULT_MAX_OUTPUT_MB))
    parser.add_argument("--job-timeout", type=int, default=_env_int("PHONE_WORKER_JOB_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    parser.add_argument("--heartbeat-once", action="store_true", help="envia um heartbeat para a VPS e encerra")
    args = parser.parse_args()

    if args.heartbeat_once:
        ok = _send_core_worker_heartbeat_once(host=args.host, port=args.port, timeout=8.0)
        return 0 if ok else 1

    server = ThreadingHTTPServer((args.host, args.port), WorkerHandler)
    server.worker_token = args.token
    server.max_body_bytes = max(1, args.max_body_mb) * 1024 * 1024
    server.max_output_bytes = max(1, args.max_output_mb) * 1024 * 1024
    server.job_timeout = max(3, args.job_timeout)
    print(f"[phone-worker] ouvindo em {args.host}:{args.port}; token={'sim' if args.token else 'não'}; versão={PHONE_WORKER_VERSION}", flush=True)
    _start_core_worker_heartbeat(host=args.host, port=args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
