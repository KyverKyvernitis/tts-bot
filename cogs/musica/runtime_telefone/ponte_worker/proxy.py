"""Proxy HTTP do Phone Worker para o Music Agent local.

Comandos usam diretamente o processo já autenticado; a auditoria completa de
imports e serviços fica fora do caminho normal de reprodução.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import uuid
from typing import Any

_READY_LOCK = threading.Lock()
_READY_RUNTIME_VERSION: dict[tuple[str, str], str] = {}
_VERSION_CACHE: tuple[tuple[Any, ...], str] | None = None
_HTTP_POOL_LOCK = threading.Lock()
_HTTP_POOL: dict[tuple[str, int], list[tuple[float, http.client.HTTPConnection]]] = {}


def _forget_runtime_version(key: tuple[str, str]) -> None:
    with _READY_LOCK:
        _READY_RUNTIME_VERSION.pop(key, None)


def _installed_version() -> str:
    """Cheap stat on every command, read/reparse only after an atomic update."""
    global _VERSION_CACHE
    path = Path(__file__).resolve().parents[1] / "agente" / "servidor.py"
    try:
        stat = path.stat()
        fingerprint = (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        with _READY_LOCK:
            if _VERSION_CACHE is not None and _VERSION_CACHE[0] == fingerprint:
                return _VERSION_CACHE[1]
        source = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
        version = match.group(1) if match else ""
        with _READY_LOCK:
            _VERSION_CACHE = (fingerprint, version)
        return version
    except (OSError, ValueError):
        return ""


def _pooled_request(
    host: str, port: int, path: str, *, method: str, headers: dict[str, str],
    body: bytes | None, timeout: float, max_bytes: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Reuse idle connections, without implicit replay of a mutable POST."""
    key = (host, port)
    now = time.monotonic()
    connection: http.client.HTTPConnection | None = None
    with _HTTP_POOL_LOCK:
        idle = _HTTP_POOL.pop(key, [])
        remaining = []
        while idle:
            expires, candidate = idle.pop()
            if expires <= now:
                candidate.close()
            elif connection is None:
                connection = candidate
            else:
                remaining.append((expires, candidate))
        if remaining:
            _HTTP_POOL[key] = remaining
        # Entries for other origins are bounded as well (configuration changes).
        for other_key, other_idle in list(_HTTP_POOL.items()):
            live = []
            for expires, candidate in other_idle:
                if expires > now:
                    live.append((expires, candidate))
                else:
                    candidate.close()
            if live:
                _HTTP_POOL[other_key] = live
            else:
                _HTTP_POOL.pop(other_key, None)
    if connection is None:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    else:
        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)
    reusable = False
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("resposta do Music Agent excede o limite")
        reusable = not response.will_close
        if response.status >= 400:
            error = urllib.error.HTTPError(
                f"http://{host}:{port}{path}", response.status, response.reason,
                response.headers, io.BytesIO(raw),
            )
            # Metadata stays attached to the error, including compatibility data.
            error.agent_headers = response_headers  # type: ignore[attr-defined]
            try:
                error.agent_payload = json.loads(raw or b"{}")  # type: ignore[attr-defined]
            except (ValueError, TypeError):
                error.agent_payload = {}  # type: ignore[attr-defined]
            raise error
        parsed = json.loads(raw or b"{}")
        if not isinstance(parsed, dict):
            raise ValueError("resposta inválida do Music Agent")
        return parsed, response_headers
    finally:
        if reusable:
            with _HTTP_POOL_LOCK:
                idle = _HTTP_POOL.setdefault(key, [])
                if len(idle) < 4 and len(_HTTP_POOL) <= 4:
                    idle.append((time.monotonic() + 30.0, connection))
                    connection = None
        if connection is not None:
            connection.close()


def _runtime_metadata(data: dict[str, Any], headers: dict[str, str]) -> tuple[str, bool | None]:
    nested = data.get("status") if isinstance(data.get("status"), dict) else {}
    version = str(headers.get("x-music-agent-version") or data.get("version") or nested.get("version") or "").strip()
    readiness = headers.get("x-music-agent-discord-ready")
    if readiness is not None:
        ready = str(readiness).strip().lower() in {"1", "true", "yes"}
    elif "discord_ready" in data:
        ready = bool(data["discord_ready"])
    elif "discord_ready" in nested:
        ready = bool(nested["discord_ready"])
    else:
        ready = None
    return version, ready


def proxy_music_agent(body: dict[str, Any], *, max_output_bytes: int, hooks: Any) -> dict[str, Any]:
    """Proxy authenticated /task requests to the local same-bot Music Agent."""
    started = time.perf_counter()
    request_ms = repair_ms = 0.0
    request_count = 0
    hooks.load_runtime_env()
    token = str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip() or hooks.ensure_token(persist=True)
    host = str(os.getenv("MUSIC_AGENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(float(os.getenv("MUSIC_AGENT_PORT") or 8780))
    except Exception:
        port = 8780
    action = str(body.get("action") or body.get("command") or "status").strip().lower().replace("-", "_") or "status"
    is_status = action in {"status", "get_state"}
    try:
        timeout_seconds = float(body.get("timeout_seconds") or os.getenv("MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS") or 18.0)
    except Exception:
        timeout_seconds = 18.0
    if not math.isfinite(timeout_seconds):
        timeout_seconds = 18.0
    timeout_seconds = max(1.0, min(90.0, timeout_seconds))
    base = f"http://{host}:{port}"
    cache_key = (base, token)
    # Zero remains an explicit compatibility mode with full preflight audit.
    try:
        ready_ttl = float(os.getenv("MUSIC_AGENT_COMMAND_READY_TTL_SECONDS") or 8.0)
    except (TypeError, ValueError):
        ready_ttl = 8.0
    if not math.isfinite(ready_ttl):
        ready_ttl = 8.0
    ready_ttl = max(0.0, min(30.0, ready_ttl))
    agent_configured = bool(str(os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip())
    attempted_prepare: dict[str, Any] | None = None
    agent_info = {"host": host, "port": port, "configured": agent_configured}

    def _finish(data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("agent", agent_info)
        if attempted_prepare is not None:
            data.setdefault("prepare", attempted_prepare)
        data["proxy_timing_ms"] = {
            "precheck": round(precheck_ms, 3), "request": round(request_ms, 3),
            "repair": round(repair_ms, 3),
            "total": round((time.perf_counter() - started) * 1000, 3),
            "request_count": request_count,
        }
        return data

    precheck_ms = 0.0
    if not is_status and not agent_configured:
        return _finish({
            "ok": False, "available": False,
            "error": "Music Agent sem token do bot no worker",
            "message": "configure MUSIC_AGENT_BOT_TOKEN em ~/phone-worker/secrets/music-agent.env",
        })

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    installed_version = _installed_version() if not is_status else ""
    # Every retry uses precisely this payload, including a generated dedup ID
    # for legacy callers that did not yet provide their own ID.
    payload = {k: v for k, v in body.items() if k not in {"task", "timeout_seconds"}}
    payload.setdefault("action", action)
    if not is_status and not str(payload.get("command_id") or "").strip():
        payload["command_id"] = uuid.uuid4().hex
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _request_agent() -> tuple[dict[str, Any], dict[str, str]]:
        nonlocal request_ms, request_count
        local_headers = dict(headers)
        path = "/command"
        method = "POST"
        request_body: bytes | None = encoded
        if is_status:
            path = "/health"
            try:
                guild_id = int(body.get("guild_id") or 0)
            except Exception:
                guild_id = 0
            compact = hooks.truthy(body.get("compact"), guild_id > 0)
            if guild_id > 0:
                params = {"guild_id": guild_id, "compact": "1" if compact else "0"}
                known_revision = str(body.get("known_revision") or "").strip()
                if known_revision:
                    params["known_revision"] = known_revision
                path += "?" + urllib.parse.urlencode(params)
            method, request_body = "GET", None
        else:
            local_headers["Content-Type"] = "application/json"
        request_started = time.perf_counter()
        request_count += 1
        try:
            return _pooled_request(
                host, port, path, method=method, headers=local_headers,
                body=request_body, timeout=timeout_seconds,
                max_bytes=max(1, min(max_output_bytes, 1024 * 1024)),
            )
        finally:
            request_ms += (time.perf_counter() - request_started) * 1000

    def _repair(*, force_stale: bool = False) -> None:
        nonlocal attempted_prepare, repair_ms
        repair_started = time.perf_counter()
        try:
            snapshot = hooks.safe_telemetry("music_agent", hooks.snapshot, {"ok": False, "available": False, "configured": agent_configured})
            runtime_version = str(snapshot.get("version") or snapshot.get("runtime_version") or "").strip()
            file_version = str(snapshot.get("file_version") or installed_version or "").strip()
            needs_restart = bool(force_stale or snapshot.get("needs_restart") or (runtime_version and file_version and hooks.version_lt(runtime_version, file_version)))
            if needs_restart:
                _forget_runtime_version(cache_key)
                attempted_prepare = hooks.run_service("music-agent", "restart")
            elif not bool(snapshot.get("available")):
                _forget_runtime_version(cache_key)
                attempted_prepare = hooks.run_service("music-agent", "start")
            else:
                # A lost response from a running agent must not restart it and
                # erase its dedup cache. Retry the same ID on the same process.
                attempted_prepare = {"ok": True, "action": "reuse", "reason": "agent_available"}
            if runtime_version:
                with _READY_LOCK:
                    _READY_RUNTIME_VERSION[cache_key] = runtime_version
        finally:
            repair_ms += (time.perf_counter() - repair_started) * 1000

    try:
        if not is_status:
            with _READY_LOCK:
                known_runtime_version = _READY_RUNTIME_VERSION.get(cache_key, "")
            known_stale = bool(known_runtime_version and installed_version and hooks.version_lt(known_runtime_version, installed_version))
            if ready_ttl == 0 or known_stale:
                _repair(force_stale=known_stale)
        precheck_ms = max(0.0, (time.perf_counter() - started) * 1000 - repair_ms)
        for attempt in range(2):
            try:
                data, response_headers = _request_agent()
                runtime_version, discord_ready = _runtime_metadata(data, response_headers)
                if runtime_version:
                    with _READY_LOCK:
                        if len(_READY_RUNTIME_VERSION) >= 8:
                            _READY_RUNTIME_VERSION.clear()
                        _READY_RUNTIME_VERSION[cache_key] = runtime_version
                data.setdefault("ok", True)
                if discord_ready is not None:
                    data["available"] = discord_ready
                else:
                    data.setdefault("available", bool(data.get("available") or data.get("ok")))
                return _finish(data)
            except urllib.error.HTTPError as exc:
                error_payload = getattr(exc, "agent_payload", {})
                error_headers = getattr(exc, "agent_headers", {})
                if not isinstance(error_payload, dict):
                    error_payload = {}
                runtime_version, _ready = _runtime_metadata(error_payload, error_headers)
                stale = bool(runtime_version and installed_version and hooks.version_lt(runtime_version, installed_version))
                # 400 means the action did not execute. A stale agent may not
                # know prepare_voice yet; repair once without replaying success.
                unsupported = "não suportada" in str(error_payload.get("error") or "") or "unsupported" in str(error_payload.get("error") or "").lower()
                if exc.code == 400 and stale and unsupported and not is_status and attempt == 0 and attempted_prepare is None:
                    _repair(force_stale=True)
                    continue
                if exc.code != 400:
                    _forget_runtime_version(cache_key)
                raw = ""
                with contextlib.suppress(Exception):
                    raw = exc.read(2048).decode("utf-8", "replace")
                return _finish({
                    "ok": False, "available": False,
                    "error": f"Music Agent HTTP {exc.code}: {hooks.short_text(raw or exc.reason, limit=260)}",
                })
            except Exception as exc:
                _forget_runtime_version(cache_key)
                if is_status or attempt > 0 or attempted_prepare is not None:
                    return _finish({"ok": False, "available": False, "error": f"{type(exc).__name__}: {hooks.short_text(exc, limit=260)}"})
                _repair()
        return _finish({"ok": False, "available": False, "error": "resposta inválida do Music Agent"})
    except Exception as exc:
        return _finish({"ok": False, "available": False, "error": f"{type(exc).__name__}: {hooks.short_text(exc, limit=260)}"})
