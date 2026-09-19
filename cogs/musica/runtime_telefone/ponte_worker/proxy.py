"""Proxy HTTP do Phone Worker para o Music Agent local."""
from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

def proxy_music_agent(body: dict[str, Any], *, max_output_bytes: int, hooks: Any) -> dict[str, Any]:
    """Proxy authenticated /task requests to the local same-bot Music Agent."""
    hooks.load_runtime_env()
    token = str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip() or hooks.ensure_token(persist=True)
    host = str(os.getenv("MUSIC_AGENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(float(os.getenv("MUSIC_AGENT_PORT") or 8780))
    except Exception:
        port = 8780
    action = str(body.get("action") or body.get("command") or "status").strip().lower().replace("-", "_") or "status"
    try:
        timeout_seconds = float(body.get("timeout_seconds") or os.getenv("MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS") or 18.0)
    except Exception:
        timeout_seconds = 18.0
    timeout_seconds = max(1.0, min(90.0, timeout_seconds))
    base = f"http://{host}:{port}"
    agent_configured = bool(str(os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip())

    if action not in {"status", "get_state"} and not agent_configured:
        return {
            "ok": False,
            "available": False,
            "error": "Music Agent sem token do bot no worker",
            "message": "configure MUSIC_AGENT_BOT_TOKEN em ~/phone-worker/secrets/music-agent.env",
            "agent": {"host": host, "port": port, "configured": False},
        }

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _request_agent() -> dict[str, Any]:
        local_headers = dict(headers)
        if action in {"status", "get_state"}:
            health_url = f"{base}/health"
            try:
                guild_id = int(body.get("guild_id") or 0)
            except Exception:
                guild_id = 0
            compact = hooks.truthy(body.get("compact"), guild_id > 0)
            if guild_id > 0:
                params = {
                    "guild_id": guild_id,
                    "compact": "1" if compact else "0",
                }
                known_revision = str(body.get("known_revision") or "").strip()
                if known_revision:
                    params["known_revision"] = known_revision
                health_url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(health_url, headers=local_headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read(min(max_output_bytes, 1024 * 1024)).decode("utf-8", "replace")
            parsed = json.loads(raw or "{}")
        else:
            payload = {k: v for k, v in body.items() if k not in {"task", "timeout_seconds"}}
            payload.setdefault("action", action)
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            local_headers["Content-Type"] = "application/json"
            req = urllib.request.Request(f"{base}/command", data=encoded, headers=local_headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read(min(max_output_bytes, 1024 * 1024)).decode("utf-8", "replace")
            parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}

    attempted_prepare: dict[str, Any] | None = None
    try:
        if action not in {"status", "get_state"}:
            snapshot = hooks.safe_telemetry("music_agent", hooks.snapshot, {"ok": False, "available": False, "configured": agent_configured})
            runtime_version = str(snapshot.get("version") or snapshot.get("runtime_version") or "").strip()
            file_version = str(snapshot.get("file_version") or "").strip()
            needs_restart = bool(snapshot.get("needs_restart") or (runtime_version and file_version and hooks.version_lt(runtime_version, file_version)))
            if needs_restart:
                attempted_prepare = hooks.run_service("music-agent", "restart")
            elif not bool(snapshot.get("available")):
                attempted_prepare = hooks.run_service("music-agent", "start")
        data = _request_agent()
    except urllib.error.HTTPError as exc:
        raw = ""
        with contextlib.suppress(Exception):
            raw = exc.read(2048).decode("utf-8", "replace")
        return {
            "ok": False,
            "available": False,
            "error": f"Music Agent HTTP {exc.code}: {hooks.short_text(raw or exc.reason, limit=260)}",
            "agent": {"host": host, "port": port, "configured": agent_configured},
            "prepare": attempted_prepare,
        }
    except Exception as exc:
        # Uma queda de conexão no primeiro comando normalmente significa
        # agent parado/desatualizado. Tenta um start uma vez antes de falhar.
        if action not in {"status", "get_state"} and attempted_prepare is None and agent_configured:
            try:
                attempted_prepare = hooks.run_service("music-agent", "start")
                data = _request_agent()
            except Exception as retry_exc:
                return {
                    "ok": False,
                    "available": False,
                    "error": f"{type(retry_exc).__name__}: {hooks.short_text(retry_exc, limit=260)}",
                    "first_error": f"{type(exc).__name__}: {hooks.short_text(exc, limit=180)}",
                    "agent": {"host": host, "port": port, "configured": agent_configured},
                    "prepare": attempted_prepare,
                }
        else:
            return {
                "ok": False,
                "available": False,
                "error": f"{type(exc).__name__}: {hooks.short_text(exc, limit=260)}",
                "agent": {"host": host, "port": port, "configured": agent_configured},
                "prepare": attempted_prepare,
            }
    if isinstance(data, dict):
        data.setdefault("ok", True)
        data.setdefault("available", bool(data.get("discord_ready") or data.get("available") or data.get("ok")))
        data.setdefault("agent", {"host": host, "port": port, "configured": agent_configured})
        if attempted_prepare is not None:
            data.setdefault("prepare", attempted_prepare)
        return data
    return {"ok": False, "available": False, "error": "resposta inválida do Music Agent", "agent": {"host": host, "port": port, "configured": agent_configured}, "prepare": attempted_prepare}
