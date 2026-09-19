"""Telemetria e health do Music Agent expostos pelo Phone Worker."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..agente.utilitarios import short_text


def inactive_music_node_snapshot() -> dict[str, Any]:
    """Wire compatibility for the retired Lavalink playback node."""
    return {
        "kind": "lavalink",
        "mode": "disabled",
        "ok": False,
        "online": False,
        "state": "disabled",
        "music_available": False,
        "playback_modes": [],
        "deprecated": True,
        "reason": "playback_owned_by_music_agent",
    }


def read_music_agent_version(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    match = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else ""


def music_voice_dependency_specs() -> dict[str, dict[str, Any]]:
    return {
        "discord.py": {"module": "discord", "pip": "discord.py"},
        "PyNaCl": {"module": "nacl", "pip": "PyNaCl"},
        "davey": {"module": "davey", "pip": "davey"},
        "yt-dlp": {"module": "yt_dlp", "pip": "yt-dlp"},
        "aiohttp": {"module": "aiohttp", "pip": "aiohttp"},
        "gTTS": {"module": "gtts", "pip": "gTTS==2.5.4", "optional": True},
        "edge-tts": {"module": "edge_tts", "pip": "edge-tts==7.2.8", "optional": True},
    }


def music_voice_dependencies_snapshot(hooks: Any) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    specs = music_voice_dependency_specs()
    for label, spec in specs.items():
        ok, error = hooks.module_import_ok(str(spec.get("module") or ""))
        checks[label] = {"ok": ok, "optional": bool(spec.get("optional"))}
        if error:
            checks[label]["error"] = error
    for binary in ("ffmpeg", "ffprobe"):
        path = hooks.which(binary)
        checks[binary] = {"ok": bool(path), "path": path or ""}
    missing = [name for name, info in checks.items() if not bool(info.get("ok"))]
    missing_critical = [name for name in missing if not bool(checks.get(name, {}).get("optional"))]
    optional_missing = [name for name in missing if bool(checks.get(name, {}).get("optional"))]
    auto_install = (
        hooks.start_dependency_autoinstall(missing, checks)
        if missing
        else {"enabled": hooks.env_bool("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS", True), "started": False, "reason": "ok"}
    )
    return {
        "ok": not missing_critical,
        "missing": missing_critical,
        "optional_missing": optional_missing,
        "all_missing": missing,
        "checks": checks,
        "auto_install": auto_install,
    }


def music_agent_snapshot(hooks: Any) -> dict[str, Any]:
    hooks.load_runtime_env()
    token = str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip() or hooks.ensure_token(persist=True)
    host = str(os.getenv("MUSIC_AGENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(float(os.getenv("MUSIC_AGENT_PORT") or 8780))
    except Exception:
        port = 8780
    configured = bool(str(os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip())
    safe_mode = hooks.safe_mode_enabled()
    deps = music_voice_dependencies_snapshot(hooks)
    file_version = read_music_agent_version(hooks.phone_worker_dir() / "music_agent.py")
    url = f"http://{host}:{port}/health"
    headers = {"Accept": "application/json", "User-Agent": f"CorePhoneWorker/{hooks.phone_worker_version}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        timeout = max(0.5, min(5.0, hooks.env_float("MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 2.5)))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(512 * 1024).decode("utf-8", "replace")
            status = int(getattr(resp, "status", 200) or 200)
        data = json.loads(raw or "{}") if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
        runtime_version = str(data.get("version") or "").strip()
        discord_ready = bool(data.get("discord_ready"))
        available = bool(data.get("available") or discord_ready)
        needs_restart = bool(runtime_version and file_version and hooks.version_lt(runtime_version, file_version))
        data.update({
            "ok": available and bool(deps.get("ok", True)),
            "available": available and bool(deps.get("ok", True)),
            "configured": configured,
            "safe_mode": safe_mode,
            "auto_start_allowed": hooks.env_autostart_enabled("MUSIC_AGENT_ENABLED", "auto"),
            "file_version": file_version,
            "runtime_version": runtime_version,
            "needs_restart": needs_restart,
            "host": host,
            "port": port,
            "http_status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "voice_dependencies": deps,
            "dependency_missing": list(deps.get("missing") or []),
            "optional_dependency_missing": list(deps.get("optional_missing") or []),
        })
        return data
    except urllib.error.HTTPError as exc:
        raw = ""
        with contextlib.suppress(Exception):
            raw = exc.read(1024).decode("utf-8", "replace")
        return {
            "ok": False, "available": False, "configured": configured, "safe_mode": safe_mode,
            "auto_start_allowed": hooks.env_autostart_enabled("MUSIC_AGENT_ENABLED", "auto"),
            "file_version": file_version, "host": host, "port": port, "http_status": int(exc.code),
            "error": short_text(raw or exc.reason, limit=180), "voice_dependencies": deps,
            "dependency_missing": list(deps.get("missing") or []),
            "optional_dependency_missing": list(deps.get("optional_missing") or []),
        }
    except Exception as exc:
        return {
            "ok": False, "available": False, "configured": configured, "safe_mode": safe_mode,
            "auto_start_allowed": hooks.env_autostart_enabled("MUSIC_AGENT_ENABLED", "auto"),
            "file_version": file_version, "host": host, "port": port,
            "error": f"{type(exc).__name__}: {short_text(exc, limit=160)}", "voice_dependencies": deps,
            "dependency_missing": list(deps.get("missing") or []),
            "optional_dependency_missing": list(deps.get("optional_missing") or []),
        }
