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
from collections import Counter
import subprocess
import tempfile
import time
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
    parser = argparse.ArgumentParser(description="Worker auxiliar do celular para tarefas opcionais da VPS.")
    parser.add_argument("--host", default=os.getenv("PHONE_WORKER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_int("PHONE_WORKER_PORT", 8766))
    parser.add_argument("--token", default=os.getenv("PHONE_WORKER_TOKEN", ""))
    parser.add_argument("--max-body-mb", type=int, default=_env_int("PHONE_WORKER_MAX_BODY_MB", DEFAULT_MAX_BODY_MB))
    parser.add_argument("--max-output-mb", type=int, default=_env_int("PHONE_WORKER_MAX_OUTPUT_MB", DEFAULT_MAX_OUTPUT_MB))
    parser.add_argument("--job-timeout", type=int, default=_env_int("PHONE_WORKER_JOB_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), WorkerHandler)
    server.worker_token = args.token
    server.max_body_bytes = max(1, args.max_body_mb) * 1024 * 1024
    server.max_output_bytes = max(1, args.max_output_mb) * 1024 * 1024
    server.job_timeout = max(3, args.job_timeout)
    print(f"[phone-worker] ouvindo em {args.host}:{args.port}; token={'sim' if args.token else 'não'}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
