"""Registro, preparo e entrega de streams PCM do domínio de música."""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

_MUSIC_STREAM_LOCK = threading.RLock()
_MUSIC_STREAMS: dict[str, dict[str, Any]] = {}
_MUSIC_PCM_PREPARATIONS: dict[str, dict[str, Any]] = {}
PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_FRAME_MS = 20
PCM_FRAME_BYTES = int(PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH_BYTES * (PCM_FRAME_MS / 1000.0))


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except Exception:
        return default


def short_text(value: Any, *, limit: int = 120, default: str = "") -> str:
    text = str(value or default).replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _music_stream_ttl_seconds() -> float:
    return max(300.0, min(21600.0, _env_float("PHONE_WORKER_MUSIC_STREAM_TTL_SECONDS", 7200.0)))


def _music_pcm_cache_dir() -> Path:
    raw = str(os.getenv("PHONE_WORKER_MUSIC_PCM_CACHE_DIR") or "").strip()
    path = Path(raw).expanduser() if raw else (Path.home() / "phone-worker" / "cache" / "music-pcm")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_music_prepared_file(item: dict[str, Any]) -> None:
    path = str(item.get("prepared_pcm_path") or "").strip()
    if not path:
        return
    with contextlib.suppress(Exception):
        p = Path(path)
        cache_dir = _music_pcm_cache_dir().resolve()
        resolved = p.resolve()
        if cache_dir in resolved.parents or resolved == cache_dir:
            p.unlink(missing_ok=True)


def _cleanup_music_streams_unlocked(now: float | None = None) -> None:
    current = time.time() if now is None else float(now)
    expired = [key for key, item in _MUSIC_STREAMS.items() if float(item.get("expires_at") or 0.0) <= current]
    for key in expired:
        item = _MUSIC_STREAMS.pop(key, None)
        if isinstance(item, dict):
            _cleanup_music_prepared_file(item)


def _register_music_stream(item: dict[str, Any]) -> str:
    stream_url = str(item.get("stream_url") or item.get("direct_url") or "").strip()
    if not stream_url:
        return ""
    seed = f"{time.time()}|{os.urandom(16).hex()}|{stream_url[:96]}".encode("utf-8", errors="ignore")
    stream_id = hashlib.sha256(seed).hexdigest()[:32]
    stored = dict(item)
    stored["id"] = stream_id
    stored["created_at"] = time.time()
    stored["expires_at"] = time.time() + _music_stream_ttl_seconds()
    with _MUSIC_STREAM_LOCK:
        _cleanup_music_streams_unlocked()
        _MUSIC_STREAMS[stream_id] = stored
    return stream_id


def _music_stream_lookup(stream_id: str) -> dict[str, Any] | None:
    stream_id = str(stream_id or "").strip()
    if not stream_id:
        return None
    with _MUSIC_STREAM_LOCK:
        _cleanup_music_streams_unlocked()
        item = _MUSIC_STREAMS.get(stream_id)
        return dict(item) if isinstance(item, dict) else None


def _safe_ffmpeg_header_lines(headers: Any) -> str:
    if not isinstance(headers, dict):
        return ""
    allowed = {"user-agent", "accept", "accept-language", "referer", "origin", "cookie", "range"}
    lines: list[str] = []
    for key, value in headers.items():
        name = str(key or "").strip()
        if not name or name.lower() not in allowed:
            continue
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        if text:
            lines.append(f"{name}: {text}\r\n")
    return "".join(lines)


def _music_prepared_mode_enabled() -> bool:
    raw = str(os.getenv("PHONE_WORKER_MUSIC_STREAM_MODE") or os.getenv("PHONE_WORKER_MUSIC_PREPARE_MODE") or "prepared").strip().lower()
    return raw not in {"live", "passthrough", "stream", "realtime", "0", "false", "off", "no", "não", "nao"}


def _music_prepare_timeout_seconds(item: dict[str, Any]) -> float:
    configured = _env_float("PHONE_WORKER_MUSIC_PREPARE_TIMEOUT_SECONDS", 0.0)
    if configured > 0:
        return max(20.0, min(1800.0, configured))
    duration = float(item.get("duration") or 0.0)
    return max(45.0, min(1800.0, duration * 2.5 + 45.0)) if duration > 0 else 240.0


def _music_prepare_max_duration_seconds() -> float:
    return max(0.0, _env_float("PHONE_WORKER_MUSIC_PREPARE_MAX_DURATION_SECONDS", 1800.0))


def _music_pcm_cache_max_bytes() -> int:
    mb = max(64.0, min(16384.0, _env_float("PHONE_WORKER_MUSIC_PCM_CACHE_MAX_MB", 2048.0)))
    return int(mb * 1024 * 1024)


def _cleanup_music_pcm_cache() -> None:
    try:
        cache_dir = _music_pcm_cache_dir()
        files = [p for p in cache_dir.glob("*.pcm") if p.is_file()]
    except Exception:
        return
    now = time.time()
    max_age = _music_stream_ttl_seconds() + 600.0
    for p in files:
        with contextlib.suppress(Exception):
            if now - p.stat().st_mtime > max_age:
                p.unlink(missing_ok=True)
    try:
        files = [p for p in cache_dir.glob("*.pcm") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
    except Exception:
        return
    max_bytes = _music_pcm_cache_max_bytes()
    if total <= max_bytes:
        return
    for p in sorted(files, key=lambda item: item.stat().st_mtime):
        with contextlib.suppress(Exception):
            size = p.stat().st_size
            p.unlink(missing_ok=True)
            total -= size
        if total <= max_bytes:
            break


_PCM_IO_MODULE = None


def _pcm_io():
    global _PCM_IO_MODULE
    if _PCM_IO_MODULE is not None:
        return _PCM_IO_MODULE
    try:
        from phone_worker_runtime import pcm_io as module
    except ModuleNotFoundError:
        # Na release Termux o pacote genérico fica no mesmo sys.path. No
        # repositório, porém, a fonte canônica da música mora em cogs/musica e
        # precisa localizar a dependência genérica sem importar phone_worker.py.
        import importlib.util

        path = Path(__file__).resolve().parents[4] / "deploy/termux/phone-worker/phone_worker_runtime/pcm_io.py"
        spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_pcm_io", path)
        if not path.is_file() or spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    _PCM_IO_MODULE = module
    return module


def _music_stream_build_ffmpeg_input_cmd(item: dict[str, Any], *, output: str) -> list[str]:
    return _pcm_io().build_ffmpeg_input_cmd(item, output=output, which=shutil.which, header_lines=_safe_ffmpeg_header_lines)


def _assert_music_preparation_owner_unlocked(stream_id: str, owner: dict[str, Any] | None) -> None:
    if owner is not None and (_MUSIC_STREAMS.get(stream_id) is not owner or float(owner.get("expires_at") or 0.0) <= time.time()):
        raise RuntimeError("stream expirou ou foi substituído durante preparo")


def _prepare_music_pcm_file(stream_id: str, item: dict[str, Any]) -> dict[str, Any]:
    with _MUSIC_STREAM_LOCK:
        owner = _MUSIC_STREAMS.get(stream_id)
        preparation = _MUSIC_PCM_PREPARATIONS.get(stream_id)
        if preparation is None:
            preparation = {"lock": threading.Lock(), "users": 0}
            _MUSIC_PCM_PREPARATIONS[stream_id] = preparation
        preparation["users"] += 1
    try:
        with preparation["lock"]:
            with _MUSIC_STREAM_LOCK:
                _assert_music_preparation_owner_unlocked(stream_id, owner)
                if isinstance(owner, dict) and owner.get("prepared_pcm_path") and not item.get("prepared_pcm_path"):
                    item = dict(owner)
            return _prepare_music_pcm_file_owned(stream_id, item, owner)
    finally:
        with _MUSIC_STREAM_LOCK:
            preparation["users"] -= 1
            if preparation["users"] == 0 and _MUSIC_PCM_PREPARATIONS.get(stream_id) is preparation:
                _MUSIC_PCM_PREPARATIONS.pop(stream_id, None)


def _prepare_music_pcm_file_owned(stream_id: str, item: dict[str, Any], owner: dict[str, Any] | None) -> dict[str, Any]:
    def publish(tmp_path, out_path, updated):
        with _MUSIC_STREAM_LOCK:
            _assert_music_preparation_owner_unlocked(stream_id, owner)
            tmp_path.replace(out_path)
            current = _MUSIC_STREAMS.get(stream_id)
            if isinstance(current, dict):
                current.update(updated)
    return _pcm_io().prepare_file(
        stream_id, item, cleanup_cache=_cleanup_music_pcm_cache, cache_dir=_music_pcm_cache_dir,
        max_duration_seconds=_music_prepare_max_duration_seconds, timeout_seconds=_music_prepare_timeout_seconds,
        build_command=_music_stream_build_ffmpeg_input_cmd, wall_time=time.time,
        temporary_directory=tempfile.TemporaryDirectory, path_type=Path, subprocess_api=subprocess,
        publish=publish, short_text=short_text,
    )


def _serve_prepared_music_pcm(handler, stream_id: str, item: dict[str, Any]) -> None:
    _pcm_io().serve_prepared(handler, stream_id, item, path_type=Path, fstat=os.fstat,
                             frame_bytes=PCM_FRAME_BYTES, short_text=short_text)


def _stream_music_pcm(handler, stream_id: str, *, send_error=None) -> None:
    if send_error is None:
        def send_error(target, status, message):
            target.send_response(int(status))
            target.send_header("Content-Type", "application/json; charset=utf-8")
            target.end_headers()

    item = _music_stream_lookup(stream_id)
    if not item:
        send_error(handler, HTTPStatus.NOT_FOUND, "stream não encontrado ou expirado")
        return
    stream_url = str(item.get("stream_url") or item.get("direct_url") or "").strip()
    if not stream_url.startswith(("http://", "https://")):
        send_error(handler, HTTPStatus.BAD_REQUEST, "stream inválido")
        return
    if _music_prepared_mode_enabled():
        try:
            prepared = _prepare_music_pcm_file(stream_id, item)
            _serve_prepared_music_pcm(handler, stream_id, prepared)
            return
        except Exception as exc:
            fallback_live = str(os.getenv("PHONE_WORKER_MUSIC_PREPARE_LIVE_FALLBACK") or "false").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}
            print(f"[music-stream] cache_failed id={stream_id} erro={type(exc).__name__}: {short_text(exc, limit=180)}", flush=True)
            if not fallback_live:
                send_error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, f"preparo de áudio no worker falhou: {type(exc).__name__}")
                return
    try:
        pcm_io = _pcm_io()
    except Exception as exc:
        send_error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, f"stream falhou: {type(exc).__name__}")
        return
    pcm_io.stream_live(handler, stream_id, item, build_command=_music_stream_build_ffmpeg_input_cmd,
                       subprocess_api=subprocess, path_type=Path, frame_bytes=PCM_FRAME_BYTES,
                       send_error=send_error, short_text=short_text)

# Public bridge names: implementation stays here; phone_worker.py only delegates.
stream_ttl_seconds = _music_stream_ttl_seconds
cleanup_streams_unlocked = _cleanup_music_streams_unlocked
register_stream = _register_music_stream
stream_lookup = _music_stream_lookup
safe_ffmpeg_header_lines = _safe_ffmpeg_header_lines
pcm_cache_dir = _music_pcm_cache_dir
prepared_mode_enabled = _music_prepared_mode_enabled
prepare_timeout_seconds = _music_prepare_timeout_seconds
prepare_max_duration_seconds = _music_prepare_max_duration_seconds
pcm_cache_max_bytes = _music_pcm_cache_max_bytes
cleanup_prepared_file = _cleanup_music_prepared_file
cleanup_pcm_cache = _cleanup_music_pcm_cache
build_ffmpeg_input_cmd = _music_stream_build_ffmpeg_input_cmd
assert_preparation_owner_unlocked = _assert_music_preparation_owner_unlocked
prepare_pcm_file = _prepare_music_pcm_file
serve_prepared_pcm = _serve_prepared_music_pcm
stream_pcm = _stream_music_pcm
