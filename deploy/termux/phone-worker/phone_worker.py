#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import shlex
import stat
from collections import Counter
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from types import SimpleNamespace

START_TIME = time.time()
JOBS_STARTED = 0
JOBS_FAILED = 0
_PING_CACHE: dict[str, Any] = {}

DEFAULT_MAX_BODY_MB = 32
DEFAULT_MAX_OUTPUT_MB = 32
DEFAULT_TIMEOUT_SECONDS = 45
PHONE_WORKER_VERSION = "1.6.4"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_JOB_POLL_INTERVAL_SECONDS = 10
DEFAULT_CORE_JOB_RESULT_MAX_BYTES = 256 * 1024

SUPPORTED_DIRECT_TASKS = (
    "diagnostic_basic",
    "ffmpeg_check",
    "ffmpeg_convert",
    "ffprobe_check",
    "ffprobe_media",
    "health",
    "log_extract",
    "log_summary",
    "maintenance_plan",
    "network_probe",
    "ping",
    "service_restart",
    "service_start",
    "service_status",
    "service_stop",
    "sha256",
    "status",
    "tailscale_status",
    "text_stats",
    "worker_logs",
    "worker_self_check",
    "worker_update",
    "apk_build_debug",
    "boot_status",
    "boot_repair",
    "zip",
    "zip_validate",
)

SUPPORTED_CORE_WORKER_JOB_TYPES = (
    "diagnostic_basic",
    "ffmpeg_check",
    "ffprobe_check",
    "log_summary",
    "maintenance_plan",
    "network_probe",
    "ping",
    "service_restart",
    "service_start",
    "service_status",
    "service_stop",
    "status",
    "tailscale_status",
    "text_stats",
    "worker_logs",
    "worker_self_check",
    "worker_update",
    "apk_build_debug",
    "boot_status",
    "boot_repair",
    "zip_validate",
)

CORE_WORKER_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "leve": {
        "label": "Leve",
        "roles": ["phone-worker", "diagnostics", "log-summary"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "worker-logs", "network-probe", "tailscale-status"],
    },
    "midia": {
        "label": "Mídia",
        "roles": ["phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "worker-logs", "network-probe", "tailscale-status"],
    },
    "completo": {
        "label": "Completo",
        "roles": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "worker-logs", "network-probe", "tailscale-status", "service-control"],
    },
    "builder": {
        "label": "Builder",
        "roles": ["phone-worker", "diagnostics", "log-summary", "apk-builder", "zip-validate"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "apk-builder", "zip-validate", "worker-logs", "network-probe", "tailscale-status"],
    },
    "bedrock": {
        "label": "Bedrock",
        "roles": ["phone-worker", "diagnostics", "log-summary", "bedrock", "bedrock-logs", "bedrock-backup"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "bedrock", "bedrock-logs", "bedrock-backup", "worker-logs", "network-probe", "tailscale-status"],
    },
}



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
            if value[0] == '"':
                with contextlib.suppress(Exception):
                    value = json.loads(value)
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


def _normalize_core_worker_profile(value: Any) -> str:
    profile = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    if profile in CORE_WORKER_PROFILE_PRESETS:
        return profile
    return "midia"


def _core_worker_profile_label(profile: Any) -> str:
    normalized = _normalize_core_worker_profile(profile)
    return str(CORE_WORKER_PROFILE_PRESETS[normalized].get("label") or normalized.title())


def _core_worker_profile_roles(profile: Any) -> list[str]:
    normalized = _normalize_core_worker_profile(profile)
    return list(CORE_WORKER_PROFILE_PRESETS[normalized].get("roles") or CORE_WORKER_PROFILE_PRESETS["midia"]["roles"])


def _core_worker_profile_capabilities(profile: Any) -> list[str]:
    normalized = _normalize_core_worker_profile(profile)
    return list(CORE_WORKER_PROFILE_PRESETS[normalized].get("capabilities") or CORE_WORKER_PROFILE_PRESETS["midia"]["capabilities"])


def _current_core_worker_roles_and_capabilities() -> tuple[list[str], list[str]]:
    profile = _current_core_worker_profile()
    roles = _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))
    return roles, capabilities


def _supported_core_worker_job_types() -> list[str]:
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    allowed = list(SUPPORTED_CORE_WORKER_JOB_TYPES)
    # Build Android é pesado e só deve aparecer para celular escolhido como builder.
    if "apk-builder" not in set(roles + capabilities):
        allowed = [item for item in allowed if item != "apk_build_debug"]
    return allowed


def _current_core_worker_profile() -> str:
    return _normalize_core_worker_profile(os.getenv("CORE_WORKER_PROFILE") or os.getenv("PHONE_WORKER_PROFILE") or "midia")


def _safe_env_key(value: Any) -> str:
    key = str(value or "").strip()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise ValueError(f"chave de env inválida: {key or '<vazia>'}")
    return key


def _format_env_value(value: Any) -> str:
    text = str(value if value is not None else "")
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,;-]*", text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _update_env_file(path: str | None, updates: dict[str, Any]) -> Path:
    env_path = Path(path or os.getenv("PHONE_WORKER_ENV") or str(Path.home() / ".phone-worker.env")).expanduser()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    wanted = {_safe_env_key(k): str(v if v is not None else "") for k, v in updates.items()}
    existing = env_path.read_text(encoding="utf-8", errors="ignore").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    assign_re = re.compile(r"^(?:export\s+)?([A-Z_][A-Z0-9_]*)=")
    for line in existing:
        match = assign_re.match(line.strip())
        if match and match.group(1) in wanted:
            key = match.group(1)
            output.append(f"{key}={_format_env_value(wanted[key])}")
            seen.add(key)
        else:
            output.append(line)
    missing = [key for key in wanted if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Core Worker pareado automaticamente. Não envie estes valores ao GitHub.")
        for key in missing:
            output.append(f"{key}={_format_env_value(wanted[key])}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    with contextlib.suppress(Exception):
        env_path.chmod(0o600)
    for key, value in wanted.items():
        os.environ[key] = value
    return env_path


def _android_prop(name: str) -> str:
    if not shutil.which("getprop"):
        return ""
    try:
        proc = subprocess.run(["getprop", name], capture_output=True, text=True, timeout=1.2)
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _default_worker_name() -> str:
    configured = str(os.getenv("CORE_WORKER_NAME") or os.getenv("PHONE_WORKER_NAME") or "").strip()
    if configured and configured.lower() not in {"localhost", "localhost.localdomain", "termux"}:
        return configured
    manufacturer = _android_prop("ro.product.manufacturer")
    model = _android_prop("ro.product.model")
    device = _android_prop("ro.product.device")
    parts = []
    if manufacturer and manufacturer.lower() not in str(model).lower():
        parts.append(manufacturer)
    if model:
        parts.append(model)
    elif device:
        parts.append(device)
    label = " ".join(part.strip() for part in parts if part.strip())
    if label:
        return label[:64]
    node = str(platform.node() or "").strip()
    if node and node.lower() not in {"localhost", "localhost.localdomain"}:
        return node[:64]
    return "Core Phone Worker"


def _default_worker_id() -> str:
    raw = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    if raw:
        return raw
    name = _default_worker_name().strip().lower()
    name = re.sub(r"[^a-z0-9_.:-]+", "-", name).strip("-._:") or "phone-worker"
    seed = f"{platform.node()}|{Path.home()}".encode("utf-8", errors="ignore")
    suffix = hashlib.sha256(seed).hexdigest()[:8]
    return f"phone-{name[:28]}-{suffix}"


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


def _read_text_file(path: str | Path, *, limit: int = 4096) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:limit].strip()
    except Exception:
        return ""


def _empty_battery_snapshot(source: str = "unavailable", error: object = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "source": _short_text(source, limit=48, default="unavailable"),
        "level": None,
        "charging": None,
        "temperature_c": None,
    }
    if error:
        result["error"] = _short_text(error, limit=120)
    return result


def _safe_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except (PermissionError, OSError):
        return False
    except Exception:
        return False


def _sysfs_battery_snapshot() -> dict[str, Any]:
    # Fallback leve quando Termux:API não está instalado ou sem permissão.
    # Em alguns Androids/Termux, apenas chamar Path.exists() em /sys pode gerar
    # PermissionError. Telemetria é sempre best-effort e nunca pode derrubar
    # heartbeat/jobs.
    base_candidates: list[Path] = []
    primary = Path("/sys/class/power_supply/battery")
    if _safe_path_exists(primary):
        base_candidates.append(primary)
    try:
        for candidate in Path("/sys/class/power_supply").glob("BAT*"):
            if _safe_path_exists(candidate) and candidate not in base_candidates:
                base_candidates.append(candidate)
    except (PermissionError, OSError):
        return _empty_battery_snapshot("sysfs_permission_denied")
    except Exception as exc:
        return _empty_battery_snapshot("sysfs_error", exc)

    for base in base_candidates:
        try:
            result: dict[str, Any] = {"available": True, "source": "sysfs"}
            capacity = _read_text_file(base / "capacity", limit=32)
            status = _read_text_file(base / "status", limit=64).lower()
            plugged = _read_text_file(base / "type", limit=64).lower()
            temp = _read_text_file(base / "temp", limit=32)
            try:
                if capacity:
                    result["level"] = max(0, min(100, int(float(capacity))))
            except Exception:
                pass
            if status:
                result["status"] = status[:32]
                result["charging"] = status in {"charging", "full"}
            if plugged:
                result["plugged"] = plugged[:32]
            try:
                if temp:
                    raw_temp = float(temp)
                    # Android costuma expor décimos de °C.
                    if raw_temp > 1000:
                        raw_temp = raw_temp / 10.0
                    result["temperature_c"] = round(raw_temp, 1)
            except Exception:
                pass
            if any(key in result for key in ("level", "status", "charging", "temperature_c")):
                return result
        except (PermissionError, OSError):
            continue
        except Exception:
            continue
    return _empty_battery_snapshot("sysfs_unavailable")


def _battery_snapshot() -> dict[str, Any]:
    try:
        raw = _run_json_command(["termux-battery-status"], timeout=2.0)
    except Exception as exc:
        raw = {}
        termux_error = exc
    else:
        termux_error = None

    if not raw:
        try:
            return _sysfs_battery_snapshot()
        except (PermissionError, OSError) as exc:
            return _empty_battery_snapshot("battery_permission_denied", exc)
        except Exception as exc:
            return _empty_battery_snapshot("battery_error", exc or termux_error)

    level = raw.get("percentage")
    if level is None:
        level = raw.get("level")
    charging = None
    status = str(raw.get("status") or "").strip().lower()
    plugged = str(raw.get("plugged") or "").strip().lower()
    if status:
        charging = status in {"charging", "full"}
    elif plugged:
        charging = plugged not in {"unplugged", "none", "unknown"}
    result: dict[str, Any] = {"available": True, "source": "termux-api"}
    try:
        if level is not None:
            clean_level = max(0, min(100, int(float(level))))
            result["level"] = clean_level
            result["percentage"] = clean_level
            result["percent"] = clean_level
    except Exception:
        pass
    if charging is not None:
        result["charging"] = bool(charging)
    if status:
        result["status"] = status[:32]
    if plugged:
        result["plugged"] = plugged[:32]
    try:
        temp = raw.get("temperature")
        if temp is not None:
            result["temperature_c"] = round(float(temp), 1)
    except Exception:
        pass
    return result


def _safe_telemetry(name: str, callback, default: Any) -> Any:
    try:
        return callback()
    except Exception as exc:
        print(f"[phone-worker] telemetria {name} indisponível: {type(exc).__name__}: {_short_text(exc, limit=100)}", flush=True)
        if isinstance(default, dict):
            fallback = dict(default)
            fallback.setdefault("ok", False)
            fallback.setdefault("error", f"{type(exc).__name__}: {_short_text(exc, limit=100)}")
            return fallback
        return default

def _run_text_command(command: list[str], *, timeout: float = 3.0, max_bytes: int = 32768) -> tuple[int, str, str]:
    if not command or not shutil.which(command[0]):
        return 127, "", f"{command[0] if command else 'comando'} não encontrado"
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.5, timeout),
        )
        stdout = (proc.stdout or b"")[:max_bytes].decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"")[:max_bytes].decode("utf-8", errors="replace")
        return int(proc.returncode), stdout.strip(), stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:
        return 1, "", f"{type(exc).__name__}: {_short_text(exc, limit=120)}"


def _mask_ipv4(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text):
        parts = text.split(".")
        return f"{parts[0]}.{parts[1]}.x.x"
    return _short_text(text, limit=64)


def _base_url_host() -> str:
    base_url, _token, _worker_id = _core_worker_auth_parts()
    if not base_url:
        return ""
    try:
        return urllib.parse.urlparse(base_url).hostname or ""
    except Exception:
        return ""


def _looks_like_tailscale_host(host: str) -> bool:
    text = str(host or "").strip()
    if re.fullmatch(r"100\.(?:\d{1,3}\.){2}\d{1,3}", text):
        return True
    # MagicDNS/headscale costumam usar nomes internos. Não marca como conectado,
    # só ajuda a explicar que a rota parece privada.
    return text.endswith(".ts.net") or text.endswith(".tailnet")


def _tailscale_snapshot(*, probe_vps: bool = False) -> dict[str, Any]:
    base_url, _token, _worker_id = _core_worker_auth_parts()
    base_host = _base_url_host()
    base_looks_tailscale = _looks_like_tailscale_host(base_host)
    result: dict[str, Any] = {
        "cli_available": bool(shutil.which("tailscale")),
        "connected": False,
        "state": "unknown",
        "via_vps_url": bool(base_looks_tailscale),
    }
    if base_host:
        result["vps_host_masked"] = _mask_ipv4(base_host)
    ip = ""
    if result["cli_available"]:
        code, stdout, stderr = _run_text_command(["tailscale", "ip", "-4"], timeout=2.5, max_bytes=4096)
        if code == 0 and stdout.strip():
            ip = stdout.strip().splitlines()[0].strip()
            result["connected"] = True
            result["ip_present"] = True
            result["ip_masked"] = _mask_ipv4(ip)
        elif stderr:
            result["ip_error"] = _short_text(stderr, limit=120)

        code, stdout, stderr = _run_text_command(["tailscale", "status", "--json"], timeout=3.5, max_bytes=65536)
        if code == 0 and stdout:
            try:
                parsed = json.loads(stdout)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                state = str(parsed.get("BackendState") or parsed.get("backendState") or "").strip()
                if state:
                    result["state"] = state[:48]
                    result["connected"] = result["connected"] or state.lower() == "running"
                self_info = parsed.get("Self") if isinstance(parsed.get("Self"), dict) else {}
                if self_info:
                    result["hostname"] = _short_text(self_info.get("HostName"), limit=64)
                    result["online"] = bool(self_info.get("Online", result.get("connected")))
                peers = parsed.get("Peer") if isinstance(parsed.get("Peer"), dict) else {}
                result["peers"] = len(peers) if isinstance(peers, dict) else 0
        elif code != 127 and stderr:
            result["status_error"] = _short_text(stderr, limit=160)
    else:
        # No Android é comum usar o app oficial do Tailscale como VPN, sem CLI no Termux.
        # Se a VPS configurada é 100.x.x.x ou MagicDNS, o heartbeat bem-sucedido já prova
        # que o Termux alcança a VPS por uma rota privada/VPN; não mostrar como "off".
        if base_looks_tailscale:
            result["connected"] = True
            result["state"] = "app/vpn"
            result["note"] = "CLI tailscale ausente; conexão inferida pelo endpoint privado da VPS"
        else:
            result["state"] = "no-cli"
            result["note"] = "CLI tailscale não encontrada no Termux; use o app oficial para a VPN"

    if probe_vps and base_url:
        health_url = base_url.rstrip("/") + "/health"
        started = time.time()
        try:
            req = urllib.request.Request(health_url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                raw = resp.read(4096)
                result["vps_reachable"] = True
                result["vps_status"] = int(getattr(resp, "status", 200) or 200)
                result["vps_latency_ms"] = round((time.time() - started) * 1000, 1)
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
                    if isinstance(data, dict):
                        result["vps_health_ok"] = bool(data.get("ok", True))
                except Exception:
                    pass
        except Exception as exc:
            result["vps_reachable"] = False
            result["vps_error"] = f"{type(exc).__name__}: {_short_text(exc, limit=120)}"
    return result



def _vps_tcp_ping_snapshot(*, timeout: float = 2.5, cache_ttl: float = 6.0) -> dict[str, Any]:
    """Mede RTT TCP do worker até a VPS/orquestrador.

    Não usa ICMP/root. Apenas abre uma conexão TCP curta para a URL já
    configurada em CORE_WORKER_VPS_URL. Resultado é cacheado por poucos
    segundos porque o payload também é usado no polling de jobs.
    """
    base_url, _token, _worker_id = _core_worker_auth_parts()
    if not base_url:
        return {"available": False, "reachable": False, "source": "not_configured"}
    try:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname or ""
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception as exc:
        return {"available": False, "reachable": False, "source": "invalid_url", "error": _short_text(exc, limit=100)}
    if not host:
        return {"available": False, "reachable": False, "source": "missing_host"}

    cache_key = f"{host}:{port}"
    now = time.monotonic()
    cached = _PING_CACHE.get(cache_key)
    if isinstance(cached, dict) and now - float(cached.get("monotonic_at") or 0.0) <= max(0.5, cache_ttl):
        result = dict(cached.get("result") or {})
        result["cached"] = True
        return result

    started = time.perf_counter()
    result: dict[str, Any] = {
        "available": True,
        "source": "tcp_connect",
        "host_masked": _mask_ipv4(host),
        "port": port,
    }
    try:
        with socket.create_connection((host, port), timeout=max(0.3, timeout)):
            pass
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        result.update({
            "reachable": True,
            "ping_ms": latency_ms,
            "latency_ms": latency_ms,
            "vps_ping_ms": latency_ms,
        })
    except Exception as exc:
        result.update({
            "reachable": False,
            "error": f"{type(exc).__name__}: {_short_text(exc, limit=100)}",
        })
    _PING_CACHE[cache_key] = {"monotonic_at": now, "result": dict(result)}
    return result

def _network_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {}
    wifi = _run_json_command(["termux-wifi-connectioninfo"], timeout=2.0)
    if wifi:
        result["type"] = "wifi"
        result["source"] = "termux-api"
        ssid = str(wifi.get("ssid") or "").strip()
        if ssid and ssid != "<unknown ssid>":
            result["name"] = _short_text(ssid, limit=48)
        try:
            result["rssi"] = int(wifi.get("rssi"))
        except Exception:
            pass
    else:
        # Sem Termux:API, ainda conseguimos dizer que há conectividade se o worker
        # está alcançando a VPS por heartbeat/poll.
        result["type"] = "connected" if _heartbeat_configured() else "unknown"
        result["source"] = "inferred"
    tailscale = _tailscale_snapshot(probe_vps=False)
    result["tailscale"] = bool(tailscale.get("connected"))
    result["tailscale_cli"] = bool(tailscale.get("cli_available"))
    result["tailscale_state"] = _short_text(tailscale.get("state"), limit=48, default="unknown")
    result["tailscale_via_vps_url"] = bool(tailscale.get("via_vps_url"))
    if tailscale.get("ip_masked"):
        result["tailscale_ip_masked"] = tailscale.get("ip_masked")
    elif tailscale.get("vps_host_masked") and tailscale.get("via_vps_url"):
        result["tailscale_ip_masked"] = tailscale.get("vps_host_masked")
    if tailscale.get("note"):
        result["tailscale_note"] = _short_text(tailscale.get("note"), limit=100)
    ping = _safe_telemetry("vps ping", _vps_tcp_ping_snapshot, {"available": False, "reachable": False, "source": "telemetry_failed"})
    if isinstance(ping, dict):
        result["vps_reachable"] = bool(ping.get("reachable"))
        result["vps_ping_available"] = bool(ping.get("available", True))
        if ping.get("ping_ms") is not None:
            result["vps_ping_ms"] = ping.get("ping_ms")
            result["ping_ms"] = ping.get("ping_ms")
        elif ping.get("latency_ms") is not None:
            result["vps_ping_ms"] = ping.get("latency_ms")
            result["ping_ms"] = ping.get("latency_ms")
        if ping.get("host_masked"):
            result["vps_host_masked"] = ping.get("host_masked")
        if ping.get("port"):
            result["vps_port"] = ping.get("port")
        if ping.get("error"):
            result["vps_ping_error"] = _short_text(ping.get("error"), limit=120)
    return result


def _heartbeat_configured() -> bool:
    if not _env_bool("CORE_WORKER_HEARTBEAT_ENABLED", True):
        return False
    return bool(
        str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip()
        and str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
        and str(os.getenv("CORE_WORKER_TOKEN") or "").strip()
    )


def _core_worker_jobs_configured() -> bool:
    if not _env_bool("CORE_WORKER_JOBS_ENABLED", True):
        return False
    return _heartbeat_configured()


def _core_worker_auth_parts() -> tuple[str, str, str]:
    base_url = str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip().rstrip("/")
    token = str(os.getenv("CORE_WORKER_TOKEN") or "").strip()
    worker_id = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    return base_url, token, worker_id


def _post_json_url(url: str, payload: dict[str, Any], *, token: str = "", timeout: float = 8.0) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(1024 * 1024)
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read(16 * 1024)
        status = int(exc.code)
    parsed: dict[str, Any]
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        parsed = data if isinstance(data, dict) else {"ok": False, "error": "resposta não é objeto"}
    except Exception as exc:
        parsed = {"ok": False, "error": f"JSON inválido da VPS: {type(exc).__name__}"}
    return status, parsed


def _get_json_url(url: str, *, timeout: float = 8.0, max_bytes: int = 1024 * 1024) -> tuple[int, dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(max_bytes + 1)
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read(16 * 1024)
        status = int(exc.code)
    if len(raw) > max_bytes:
        return status, {"ok": False, "error": "resposta JSON grande demais"}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        parsed = data if isinstance(data, dict) else {"ok": False, "error": "resposta não é objeto"}
    except Exception as exc:
        parsed = {"ok": False, "error": f"JSON inválido: {type(exc).__name__}"}
    return status, parsed


def _download_url_to_file(url: str, target: Path, *, timeout: float = 35.0, max_bytes: int = 150 * 1024 * 1024) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.android.package-archive,*/*",
        "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    total = 0
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp, tmp.open("wb") as fh:
            status = int(getattr(resp, "status", 200) or 200)
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("APK grande demais para este worker")
                digest.update(chunk)
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        body = exc.read(8 * 1024).decode("utf-8", errors="replace")
        with contextlib.suppress(Exception):
            tmp.unlink()
        return {"ok": False, "status": int(exc.code), "error": _short_text(body or exc, limit=180)}
    except Exception:
        with contextlib.suppress(Exception):
            tmp.unlink()
        raise
    tmp.replace(target)
    return {"ok": True, "status": status, "path": str(target), "bytes": total, "sha256": digest.hexdigest()}


def _post_core_worker_json(path: str, payload: dict[str, Any], *, timeout: float = 8.0) -> tuple[int, dict[str, Any]]:
    base_url, token, _worker_id = _core_worker_auth_parts()
    if not base_url or not token:
        return 0, {"ok": False, "error": "Core Worker não configurado"}
    return _post_json_url(f"{base_url}{path}", payload, token=token, timeout=timeout)


def _core_worker_payload(*, host: str, port: int) -> dict[str, Any]:
    status = _safe_telemetry("system", _system_status, {"ok": False})
    worker_id = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    name = _default_worker_name()
    endpoint = str(os.getenv("CORE_WORKER_ENDPOINT") or os.getenv("PHONE_WORKER_ENDPOINT") or "").strip()
    if not endpoint and host not in {"", "0.0.0.0", "::"}:
        endpoint = f"http://{host}:{port}"
    profile = _current_core_worker_profile()
    roles = _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))
    if status.get("ffmpeg") and "ffmpeg" not in capabilities:
        capabilities.append("ffmpeg")
    if status.get("ffprobe") and "ffprobe" not in capabilities:
        capabilities.append("ffprobe")
    return {
        "worker_id": worker_id,
        "name": _short_text(name, limit=64, default="Core Phone Worker"),
        "source": "termux-phone-worker",
        "version": PHONE_WORKER_VERSION,
        "profile": profile,
        "profile_label": _core_worker_profile_label(profile),
        "endpoint": endpoint,
        "roles": roles[:16],
        "capabilities": capabilities[:24],
        "supported_tasks": _supported_core_worker_job_types(),
        "battery": _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot()),
        "network": _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}),
        "health": {
            "ok": True,
            "pid": status.get("pid"),
            "uptime_seconds": status.get("uptime_seconds"),
            "jobs_started": status.get("jobs_started"),
            "jobs_failed": status.get("jobs_failed"),
            "ffmpeg": status.get("ffmpeg"),
            "ffprobe": status.get("ffprobe"),
            "scripts_ok": ((status.get("scripts") or {}).get("complete") if isinstance(status.get("scripts"), dict) else None),
            "boot_ok": ((status.get("boot") or {}).get("ok") if isinstance(status.get("boot"), dict) else None),
            "supervisor_ok": ((status.get("supervisor") or {}).get("supervisor_ok") if isinstance(status.get("supervisor"), dict) else None),
        },
        "status": {
            "profile": profile,
            "profile_label": _core_worker_profile_label(profile),
            "http_host": host,
            "http_port": port,
            "python": status.get("python"),
            "platform": status.get("platform"),
            "disk_home": status.get("disk_home"),
            "loadavg": status.get("loadavg"),
            "scripts": status.get("scripts"),
            "boot": status.get("boot"),
            "supervisor": status.get("supervisor"),
        },
    }


def _pair_core_worker(
    *,
    code: str,
    vps_url: str,
    host: str,
    port: int,
    worker_id: str = "",
    name: str = "",
    roles: str = "",
    capabilities: str = "",
    env_file: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    normalized_code = str(code or "").strip().upper()
    base_url = str(vps_url or "").strip().rstrip("/")
    if not normalized_code:
        message = "informe o código CORE-XXXX"
        print(f"[core-worker-pair] {message}", flush=True)
        return {"ok": False, "error": message}
    if not base_url:
        message = "informe a URL da VPS/Tailscale com --vps-url"
        print(f"[core-worker-pair] {message}", flush=True)
        return {"ok": False, "error": message}

    selected_worker_id = str(worker_id or _default_worker_id()).strip()
    selected_name = str(name or _default_worker_name()).strip()
    payload = _core_worker_payload(host=host, port=port)
    payload.update({
        "code": normalized_code,
        "worker_id": selected_worker_id,
        "name": _short_text(selected_name, limit=64, default="Core Phone Worker"),
        "source": "termux-phone-worker",
    })
    requested_roles = _env_list("CORE_WORKER_ROLES", []) if not roles else _env_list("CORE_WORKER_ROLES", [])
    if roles:
        os.environ["CORE_WORKER_ROLES"] = roles
        requested_roles = _env_list("CORE_WORKER_ROLES", [])
    requested_capabilities = _env_list("CORE_WORKER_CAPABILITIES", []) if not capabilities else _env_list("CORE_WORKER_CAPABILITIES", [])
    if capabilities:
        os.environ["CORE_WORKER_CAPABILITIES"] = capabilities
        requested_capabilities = _env_list("CORE_WORKER_CAPABILITIES", [])
    if requested_roles:
        payload["roles"] = requested_roles[:16]
    if requested_capabilities:
        payload["capabilities"] = requested_capabilities[:24]

    status, data = _post_json_url(f"{base_url}/core-worker/pair", payload, timeout=timeout)
    if not (200 <= status < 300) or not data.get("ok", False):
        message = _short_text(data.get("error") or data, limit=180)
        print(f"[core-worker-pair] HTTP {status}: {message}", flush=True)
        return {"ok": False, "status": status, "error": message}

    token = str(data.get("token") or "").strip()
    returned_worker_id = str(data.get("worker_id") or selected_worker_id).strip()
    if not token or not returned_worker_id:
        message = "resposta sem worker_id/token"
        print(f"[core-worker-pair] {message}", flush=True)
        return {"ok": False, "status": status, "error": message}

    env_path = _update_env_file(env_file, {
        "CORE_WORKER_HEARTBEAT_ENABLED": "true",
        "CORE_WORKER_JOBS_ENABLED": "true",
        "CORE_WORKER_VPS_URL": base_url,
        "CORE_WORKER_ID": returned_worker_id,
        "CORE_WORKER_TOKEN": token,
        "CORE_WORKER_NAME": payload.get("name") or selected_name,
        "CORE_WORKER_ROLES": ",".join(payload.get("roles") or _env_list("CORE_WORKER_ROLES", [])),
        "CORE_WORKER_CAPABILITIES": ",".join(payload.get("capabilities") or _env_list("CORE_WORKER_CAPABILITIES", [])),
    })
    print(f"[core-worker-pair] pareado como {returned_worker_id}; token salvo em {env_path}", flush=True)
    print("[core-worker-pair] heartbeat/jobs já podem usar o env atualizado; reiniciar ainda é recomendado se o supervisor estiver antigo.", flush=True)
    return {
        "ok": True,
        "status": status,
        "worker_id": returned_worker_id,
        "name": payload.get("name") or selected_name,
        "vps_url": base_url,
        "env_updated": True,
        "env_file": str(env_path),
        "message": "worker local pareado com a VPS",
    }

def _send_core_worker_heartbeat_once(*, host: str, port: int, timeout: float = 6.0) -> bool:
    _base_url, _token, worker_id = _core_worker_auth_parts()
    if not _base_url or not _token or not worker_id:
        return False
    try:
        payload = _core_worker_payload(host=host, port=port)
        status, data = _post_core_worker_json("/core-worker/heartbeat", payload, timeout=timeout)
        if 200 <= status < 300 and data.get("ok", True):
            return True
        print(f"[core-worker-heartbeat] HTTP {status}: {_short_text(data.get('error') or data, limit=180)}", flush=True)
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
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "name": _default_worker_name(),
        "version": PHONE_WORKER_VERSION,
        "profile": _current_core_worker_profile(),
        "profile_label": _core_worker_profile_label(_current_core_worker_profile()),
        "core_worker_heartbeat": _heartbeat_configured(),
        "core_worker_jobs": _core_worker_jobs_configured(),
        "scripts": _script_inventory(),
        "boot": _safe_telemetry("boot", _termux_boot_status_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "supervisor": _safe_telemetry("supervisor", _runtime_supervisor_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "supported_tasks": list(SUPPORTED_DIRECT_TASKS),
        "supported_core_worker_jobs": _supported_core_worker_job_types(),
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


def _local_agent_status_payload(*, host: str, port: int) -> dict[str, Any]:
    profile = _current_core_worker_profile()
    status = _safe_telemetry("system", _system_status, {"ok": False})
    return {
        "ok": True,
        "local_only": True,
        "worker": "phone-worker",
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "name": _default_worker_name(),
        "version": PHONE_WORKER_VERSION,
        "profile": profile,
        "profile_label": _core_worker_profile_label(profile),
        "roles": _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))[:16],
        "capabilities": _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))[:24],
        "supported_tasks": _supported_core_worker_job_types(),
        "vps_configured": _heartbeat_configured(),
        "jobs_configured": _core_worker_jobs_configured(),
        "vps_url": str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip(),
        "pid": os.getpid(),
        "uptime_seconds": status.get("uptime_seconds"),
        "endpoint": f"http://127.0.0.1:{port}",
        "bind_host": host,
        "bind_port": port,
        "ffmpeg": bool(status.get("ffmpeg")),
        "ffprobe": bool(status.get("ffprobe")),
        "boot_ok": ((status.get("boot") or {}).get("ok") if isinstance(status.get("boot"), dict) else None),
        "supervisor_ok": ((status.get("supervisor") or {}).get("supervisor_ok") if isinstance(status.get("supervisor"), dict) else None),
        "note": "Rota local para o APK Core Worker; não executa shell e não expõe token.",
    }


def _apply_local_core_worker_profile(profile: Any) -> dict[str, Any]:
    normalized = _normalize_core_worker_profile(profile)
    roles = _core_worker_profile_roles(normalized)
    capabilities = _core_worker_profile_capabilities(normalized)
    env_path = _update_env_file(None, {
        "CORE_WORKER_PROFILE": normalized,
        "CORE_WORKER_ROLES": ",".join(roles),
        "CORE_WORKER_CAPABILITIES": ",".join(capabilities),
    })
    return {
        "ok": True,
        "saved": True,
        "profile": normalized,
        "profile_label": _core_worker_profile_label(normalized),
        "roles": roles,
        "capabilities": capabilities,
        "env_updated": True,
        "env_file": str(env_path),
    }


class WorkerHandler(BaseHTTPRequestHandler):
    server_version = "PhoneWorker/1.2"

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

    def _is_local_client(self) -> bool:
        host = str((self.client_address or ("", 0))[0] or "").strip().lower()
        return host == "::1" or host == "localhost" or host.startswith("127.")

    def _require_local_client(self) -> bool:
        if self._is_local_client():
            return True
        _error(self, HTTPStatus.FORBIDDEN, "rota local disponível apenas em 127.0.0.1")
        return False

    def _bind_host_port(self) -> tuple[str, int]:
        host = str(getattr(self.server, "phone_worker_host", "127.0.0.1") or "127.0.0.1")
        port = int(getattr(self.server, "phone_worker_port", 8766) or 8766)
        return host, port

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
        path = urllib.parse.urlparse(self.path).path
        if path == "/local/status":
            if not self._require_local_client():
                return
            host, port = self._bind_host_port()
            _json_response(self, HTTPStatus.OK, _local_agent_status_payload(host=host, port=port))
            return
        if path not in {"/", "/health", "/status"}:
            _error(self, HTTPStatus.NOT_FOUND, "rota não encontrada")
            return
        if not self._require_auth():
            return
        _json_response(self, HTTPStatus.OK, _system_status())

    def do_POST(self) -> None:
        global JOBS_STARTED, JOBS_FAILED
        path = urllib.parse.urlparse(self.path).path
        if path == "/local/pair":
            if not self._require_local_client():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                host, port = self._bind_host_port()
                profile_result = _apply_local_core_worker_profile(body.get("profile"))
                name = _short_text(body.get("name") or body.get("device_name") or _default_worker_name(), limit=64, default="Core Phone Worker")
                pair_result = _pair_core_worker(
                    code=str(body.get("code") or ""),
                    vps_url=str(body.get("vps_url") or body.get("server_url") or ""),
                    host=host,
                    port=port,
                    worker_id=str(os.getenv("CORE_WORKER_ID") or _default_worker_id()),
                    name=name,
                    roles=",".join(profile_result.get("roles") or []),
                    capabilities=",".join(profile_result.get("capabilities") or []),
                    env_file=None,
                    timeout=10.0,
                )
                if not pair_result.get("ok"):
                    _json_response(self, HTTPStatus.BAD_REQUEST, pair_result)
                    return
                heartbeat_ok = _send_core_worker_heartbeat_once(host=host, port=port, timeout=6.0)
                result = _local_agent_status_payload(host=host, port=port)
                result.update(pair_result)
                result["profile"] = profile_result.get("profile")
                result["profile_label"] = profile_result.get("profile_label")
                result["roles"] = profile_result.get("roles") or result.get("roles")
                result["capabilities"] = profile_result.get("capabilities") or result.get("capabilities")
                result["synced_to_vps"] = bool(heartbeat_ok)
                result["message"] = "worker local pareado; o APK não criou registro separado"
                _json_response(self, HTTPStatus.OK, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
            return
        if path == "/local/heartbeat":
            if not self._require_local_client():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                host, port = self._bind_host_port()
                result = _local_agent_status_payload(host=host, port=port)
                result["synced_to_vps"] = _send_core_worker_heartbeat_once(host=host, port=port, timeout=6.0) if _heartbeat_configured() else False
                result["message"] = "heartbeat solicitado ao worker local"
                _json_response(self, HTTPStatus.OK, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
            return
        if path == "/local/profile":
            if not self._require_local_client():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                result = _apply_local_core_worker_profile(body.get("profile"))
                host, port = self._bind_host_port()
                result.update(_local_agent_status_payload(host=host, port=port))
                result["synced_to_vps"] = _send_core_worker_heartbeat_once(host=host, port=port, timeout=5.0) if _heartbeat_configured() else False
                result["message"] = "perfil atualizado no worker local"
                _json_response(self, HTTPStatus.OK, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
            return
        if path != "/task":
            _error(self, HTTPStatus.NOT_FOUND, "rota não encontrada")
            return
        if not self._require_auth():
            return
        body = self._read_json()
        if body is None:
            return

        task = str(body.get("task") or "").strip().lower().replace("-", "_")
        JOBS_STARTED += 1
        try:
            if task in {"ping", "health", "status"}:
                payload = _system_status()
                payload.setdefault("summary", "status direto coletado")
            elif task in {"diagnostic_basic", "worker_self_check"}:
                payload = _execute_core_worker_job({"type": "worker_self_check", "payload": body}, max_body_bytes=self.max_body_bytes, max_output_bytes=self.max_output_bytes, job_timeout=self.job_timeout)
            elif task in {"network_probe", "tailscale_status", "worker_logs", "worker_update", "apk_build_debug", "boot_status", "boot_repair", "service_status", "service_start", "service_stop", "service_restart", "ffmpeg_check", "ffprobe_check"}:
                payload = _execute_core_worker_job({"type": task, "payload": body}, max_body_bytes=self.max_body_bytes, max_output_bytes=self.max_output_bytes, job_timeout=self.job_timeout)
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
            _launch_deferred_phone_worker_action(payload)
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



def _command_version(command: str) -> dict[str, Any]:
    if not shutil.which(command):
        return {"ok": False, "available": False, "command": command}
    try:
        proc = subprocess.run([command, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
        output = (proc.stdout or proc.stderr).decode("utf-8", errors="replace").splitlines()
        return {
            "ok": proc.returncode == 0,
            "available": True,
            "command": command,
            "returncode": proc.returncode,
            "version_line": _short_text(output[0] if output else "", limit=180),
        }
    except Exception as exc:
        return {"ok": False, "available": True, "command": command, "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"}



def _sanitize_log_text(value: str, *, limit: int = 12000) -> str:
    text = str(value or "")
    for key in ("PHONE_WORKER_TOKEN", "CORE_WORKER_TOKEN"):
        raw = str(os.getenv(key) or "").strip()
        if raw:
            text = text.replace(raw, "[redacted]")
    text = re.sub(r"(Authorization:\s*Bearer\s+)[^\s]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(X-(?:Phone|Core)-Worker-Token:\s*)[^\s]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    if len(text) > limit:
        return text[-limit:]
    return text


def _phone_worker_dir() -> Path:
    return Path(os.getenv("PHONE_WORKER_DIR") or (Path.home() / "phone-worker")).expanduser()


def _phone_worker_log_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_LOG_FILE") or (_phone_worker_dir() / "phone-worker.log")).expanduser()


def _phone_worker_pid_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_PID_FILE") or (_phone_worker_dir() / "phone-worker.pid")).expanduser()


def _phone_worker_status_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_STATUS_FILE") or (_phone_worker_dir() / "phone-worker.status")).expanduser()


def _phone_worker_start_lock_dir() -> Path:
    return Path(os.getenv("PHONE_WORKER_LOCK_DIR") or (_phone_worker_dir() / ".phone-worker-start.lock")).expanduser()


def _phone_worker_watch_log_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_WATCH_LOG_FILE") or (_phone_worker_dir() / "phone-worker-watch.log")).expanduser()


def _phone_worker_watch_pid_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_WATCH_PID_FILE") or (_phone_worker_dir() / "phone-worker-watch.pid")).expanduser()


def _read_pid_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[0]
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool | None:
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _path_size(path: Path) -> int | None:
    try:
        return int(path.stat().st_size)
    except Exception:
        return None


def _runtime_supervisor_snapshot() -> dict[str, Any]:
    pid_file = _phone_worker_pid_file()
    status_file = _phone_worker_status_file()
    log_file = _phone_worker_log_file()
    watch_log = _phone_worker_watch_log_file()
    watch_pid_file = _phone_worker_watch_pid_file()
    pid = _read_pid_file(pid_file)
    watch_pid = _read_pid_file(watch_pid_file)
    processes = _pgrep_count("phone_worker.py") if "_pgrep_count" in globals() else None
    result: dict[str, Any] = {
        "ok": True,
        "current_pid": os.getpid(),
        "pid_file": str(pid_file),
        "pid_file_pid": pid,
        "pid_file_alive": _pid_alive(pid),
        "processes": processes,
        "duplicates": (max(0, int(processes) - 1) if isinstance(processes, int) else None),
        "lock_dir": str(_phone_worker_start_lock_dir()),
        "lock_active": _phone_worker_start_lock_dir().exists(),
        "log_file": str(log_file),
        "log_size_bytes": _path_size(log_file),
        "watch_log_file": str(watch_log),
        "watch_log_size_bytes": _path_size(watch_log),
        "watch_pid_file": str(watch_pid_file),
        "watch_pid": watch_pid,
        "watch_pid_alive": _pid_alive(watch_pid),
        "status_file": str(status_file),
        "status_text": _short_text(_read_text_file(status_file, limit=240), limit=160),
    }
    result["supervisor_ok"] = bool(
        (result.get("pid_file_alive") in {True, None})
        and (result.get("duplicates") in {0, None})
    )
    return result


def _termux_boot_script_path() -> Path:
    return (Path.home() / ".termux" / "boot" / "10-core-worker").expanduser()


def _termux_boot_script_content() -> str:
    return "\n".join([
        "#!/data/data/com.termux/files/usr/bin/sh",
        "# Auto-start do Core Worker pelo Termux:Boot.",
        "# Criado/reparado pelo phone-worker. Não coloque segredos aqui.",
        "termux-wake-lock 2>/dev/null || true",
        "sleep \"${PHONE_WORKER_BOOT_DELAY_SECONDS:-25}\"",
        "cd \"$HOME/phone-worker\" || exit 0",
        "if [ -x \"$HOME/phone-worker/start-phone-worker.sh\" ]; then",
        "  exec \"$HOME/phone-worker/start-phone-worker.sh\"",
        "fi",
        "echo '[core-worker-boot] start-phone-worker.sh não encontrado' >> \"$HOME/phone-worker.log\"",
        "",
    ])


def _termux_boot_package_status() -> dict[str, Any]:
    # Best-effort: Android 16 pode negar listagem completa de pacotes para o app,
    # então falha aqui não deve marcar o boot como quebrado.
    if not shutil.which("cmd"):
        return {"available": None, "source": "cmd_missing"}
    for command in (["cmd", "package", "path", "com.termux.boot"], ["pm", "path", "com.termux.boot"]):
        code, stdout, stderr = _run_text_command(command, timeout=2.0, max_bytes=2048)
        text = f"{stdout}\n{stderr}".strip()
        if code == 0 and "package:" in stdout:
            return {"available": True, "source": command[0]}
        if "com.termux.boot" in text.lower() and "permission denial" not in text.lower():
            return {"available": True, "source": command[0], "note": _short_text(text, limit=100)}
        if "permission denial" in text.lower():
            return {"available": None, "source": command[0], "note": "Android negou listagem de pacote"}
    return {"available": False, "source": "pm", "note": "Termux:Boot não detectado"}


def _termux_boot_status_snapshot() -> dict[str, Any]:
    path = _termux_boot_script_path()
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "executable": False,
        "content_ok": False,
        "ok": False,
        "source": "termux-boot",
    }
    try:
        exists = path.exists()
        result["exists"] = bool(exists)
        if exists:
            result["executable"] = os.access(path, os.X_OK)
            content = path.read_text(encoding="utf-8", errors="ignore")[:4096]
            result["content_ok"] = ("start-phone-worker.sh" in content or "phone_worker.py" in content) and "phone-worker" in content
            result["size"] = path.stat().st_size
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {_short_text(exc, limit=100)}"
    package = _termux_boot_package_status()
    result["package"] = package
    result["package_available"] = package.get("available")
    result["ok"] = bool(result.get("exists") and result.get("executable") and result.get("content_ok"))
    if result["ok"] and package.get("available") is False:
        result["warning"] = "script ok, mas app Termux:Boot não detectado"
    elif not result["ok"]:
        result["warning"] = "script de boot ausente/incompleto"
    return result


def _repair_termux_boot_script() -> dict[str, Any]:
    path = _termux_boot_script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _termux_boot_script_content()
    changed = True
    if path.exists():
        with contextlib.suppress(Exception):
            changed = path.read_text(encoding="utf-8", errors="ignore") != content
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o755)
    status = _termux_boot_status_snapshot()
    status.update({
        "ok": bool(status.get("ok")),
        "changed": bool(changed),
        "summary": "boot automático reparado" if status.get("ok") else "boot automático criado, verifique Termux:Boot",
    })
    return status


def _home_script(name: str) -> Path:
    return (Path.home() / name).expanduser()


def _script_candidates(name: str) -> list[Path]:
    # Preferir scripts dentro de ~/phone-worker, mas manter compatibilidade com ~/script.sh.
    worker_path = (_phone_worker_dir() / name).expanduser()
    home_path = _home_script(name)
    if worker_path == home_path:
        return [worker_path]
    return [worker_path, home_path]


def _best_script(name: str) -> Path:
    for path in _script_candidates(name):
        if path.exists():
            return path
    return _script_candidates(name)[0]


def _script_inventory() -> dict[str, Any]:
    scripts: dict[str, Any] = {}
    complete = True
    mirrored = True
    for name in ("start-phone-worker.sh", "watch-phone-worker.sh", "pair-phone-worker.sh"):
        worker_path = (_phone_worker_dir() / name).expanduser()
        home_path = _home_script(name)
        worker_exists = worker_path.exists()
        home_exists = home_path.exists()
        exists = worker_exists or home_exists
        complete = complete and exists
        mirrored = mirrored and worker_exists and home_exists
        scripts[name] = {
            "ok": exists,
            "worker_dir": worker_exists,
            "home": home_exists,
            "executable": any(path.exists() and os.access(path, os.X_OK) for path in (worker_path, home_path)),
            "preferred": str(_best_script(name)),
        }
    return {"complete": complete, "mirrored": mirrored, "scripts": scripts}


def _tmux_session_exists(session: str) -> bool:
    if not session or not shutil.which("tmux"):
        return False
    code, _stdout, _stderr = _run_text_command(["tmux", "has-session", "-t", session], timeout=2.0, max_bytes=1024)
    return code == 0


def _pgrep_count(pattern: str) -> int:
    if not shutil.which("pgrep"):
        return 0
    code, stdout, _stderr = _run_text_command(["pgrep", "-f", pattern], timeout=2.0, max_bytes=4096)
    if code != 0 or not stdout:
        return 0
    current = str(os.getpid())
    return sum(1 for line in stdout.splitlines() if line.strip() and line.strip() != current)


def _allowed_service_name(value: Any) -> str:
    service = str(value or "phone-worker").strip().lower().replace("_", "-")
    service = re.sub(r"[^a-z0-9.-]+", "-", service).strip("-.")
    aliases = {
        "worker": "phone-worker",
        "core-worker": "phone-worker",
        "phone": "phone-worker",
        "watch": "phone-worker-watch",
        "watchdog": "phone-worker-watch",
    }
    service = aliases.get(service, service)
    if service not in {"phone-worker", "phone-worker-watch", "tailscale"}:
        raise ValueError("serviço não permitido")
    return service


def _service_status(service: str) -> dict[str, Any]:
    service = _allowed_service_name(service)
    phone_session = str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker").strip() or "phone-worker"
    watch_session = str(os.getenv("PHONE_WORKER_WATCH_TMUX_SESSION") or "phone-worker-watch").strip() or "phone-worker-watch"
    if service == "phone-worker":
        status = _system_status()
        supervisor = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
        return {
            "ok": True,
            "service": service,
            "manageable": True,
            "running": True,
            "current_pid": os.getpid(),
            "tmux_session": phone_session,
            "tmux_running": _tmux_session_exists(phone_session),
            "processes": _pgrep_count("phone_worker.py"),
            "uptime_seconds": status.get("uptime_seconds"),
            "log_file": str(_phone_worker_log_file()),
            "scripts": _script_inventory(),
            "start_script": str(_best_script("start-phone-worker.sh")),
            "supervisor": supervisor,
            "pid_file": supervisor.get("pid_file"),
            "pid_file_pid": supervisor.get("pid_file_pid"),
            "duplicates": supervisor.get("duplicates"),
        }
    if service == "phone-worker-watch":
        return {
            "ok": True,
            "service": service,
            "manageable": True,
            "running": _tmux_session_exists(watch_session),
            "tmux_session": watch_session,
            "script": str(_best_script("watch-phone-worker.sh")),
            "scripts": _script_inventory(),
        }
    tailscale = _tailscale_snapshot(probe_vps=True)
    return {
        "ok": True,
        "service": service,
        "manageable": False,
        "running": bool(tailscale.get("connected")),
        "tailscale": tailscale,
        "note": "controle start/stop do Tailscale oficial deve ser feito no app Android; o worker só diagnostica",
    }


def _run_service_action(service: str, action: str) -> dict[str, Any]:
    service = _allowed_service_name(service)
    action = str(action or "status").strip().lower().replace("-", "_")
    if action == "status":
        return _service_status(service)
    if action not in {"start", "stop", "restart"}:
        raise ValueError("ação de serviço não permitida")

    phone_session = str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker").strip() or "phone-worker"
    watch_session = str(os.getenv("PHONE_WORKER_WATCH_TMUX_SESSION") or "phone-worker-watch").strip() or "phone-worker-watch"
    start_script = _best_script("start-phone-worker.sh")
    watch_script = _best_script("watch-phone-worker.sh")

    if service == "tailscale":
        raise ValueError("Tailscale oficial no Android não pode ser iniciado/parado pelo Termux com segurança; use o app Tailscale")

    if service == "phone-worker-watch":
        if action in {"stop", "restart"}:
            if shutil.which("tmux"):
                _run_text_command(["tmux", "kill-session", "-t", watch_session], timeout=3.0, max_bytes=4096)
        if action in {"start", "restart"}:
            if not watch_script.exists():
                raise FileNotFoundError(str(watch_script))
            if not shutil.which("tmux"):
                raise RuntimeError("tmux não encontrado")
            code, stdout, stderr = _run_text_command(["tmux", "new-session", "-d", "-s", watch_session, str(watch_script)], timeout=4.0, max_bytes=4096)
            if code != 0 and "duplicate session" not in stderr.lower():
                raise RuntimeError(stderr or stdout or f"tmux retornou {code}")
        return _service_status(service) | {"action": action}

    # phone-worker é o próprio processo atual. Parar/reiniciar precisa ser deferido
    # para o resultado do job conseguir voltar para a VPS antes do tmux/pkill.
    if action == "start":
        if not start_script.exists():
            raise FileNotFoundError(str(start_script))
        proc = subprocess.run([str(start_script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20.0)
        status = _service_status(service)
        status.update({
            "action": action,
            "returncode": int(proc.returncode),
            "stdout": _sanitize_log_text(proc.stdout.decode("utf-8", errors="replace"), limit=3000),
            "stderr": _sanitize_log_text(proc.stderr.decode("utf-8", errors="replace"), limit=3000),
        })
        if proc.returncode != 0:
            status["ok"] = False
        return status
    if action == "restart" and not start_script.exists():
        raise FileNotFoundError(str(start_script))
    status = _service_status(service)
    status.update({"action": action, "deferred": True, "message": f"{action} agendado após envio do resultado"})
    status["_deferred_phone_worker_action"] = action
    status["_deferred_phone_worker_session"] = phone_session
    status["_deferred_start_script"] = str(start_script)
    return status





def _safe_extract_zip_file(zip_path: Path, target_dir: Path, *, max_members: int = 6000) -> int:
    target_root = target_dir.resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if count >= max_members:
                raise ValueError("source zip com arquivos demais")
            name = str(member.filename or "").replace("\\", "/")
            if not name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"caminho inseguro no zip: {name[:80]}")
            destination = (target_root / name).resolve()
            if destination != target_root and not str(destination).startswith(str(target_root) + os.sep):
                raise ValueError(f"caminho fora da pasta do build: {name[:80]}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=128 * 1024)
            count += 1
    return count



def _is_termux_runtime() -> bool:
    prefix = os.getenv("PREFIX") or ""
    return "com.termux" in prefix or Path("/data/data/com.termux/files/usr").exists()


def _prepare_termux_android_build(project_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    """Ajustes seguros para build Android dentro do Termux.

    O Android Gradle Plugin tenta baixar um aapt2 Linux comum que não roda no
    Android/Termux. Quando existir aapt2 do Termux, forçamos o override global.
    Também permitimos fallback para SDK 34, que é o caminho que funcionou no
    builder do usuário.
    """
    info: dict[str, Any] = {"termux": _is_termux_runtime()}
    if not info["termux"] or not _env_bool("PHONE_WORKER_APK_BUILD_TERMUX_TWEAKS", True):
        return info

    default_sdk = Path.home() / "android-sdk"
    if not env.get("ANDROID_HOME") and default_sdk.exists():
        env["ANDROID_HOME"] = str(default_sdk)
        env.setdefault("ANDROID_SDK_ROOT", str(default_sdk))
        env["PATH"] = f"{default_sdk}/cmdline-tools/latest/bin:{default_sdk}/platform-tools:" + env.get("PATH", "")
        info["android_home"] = str(default_sdk)

    sdk_fallback = str(os.getenv("PHONE_WORKER_APK_BUILD_TERMUX_SDK") or "34").strip()
    build_gradle = project_dir / "app" / "build.gradle"
    if sdk_fallback and build_gradle.exists():
        text = build_gradle.read_text(encoding="utf-8", errors="ignore")
        changed = re.sub(r"(compileSdk\s*=?\s*)\d+", rf"\g<1>{sdk_fallback}", text)
        changed = re.sub(r"(targetSdk\s*=?\s*)\d+", rf"\g<1>{sdk_fallback}", changed)
        if changed != text:
            build_gradle.write_text(changed, encoding="utf-8")
            info["sdk_fallback"] = sdk_fallback

    aapt2_path = shutil.which("aapt2")
    if aapt2_path:
        gradle_dir = Path.home() / ".gradle"
        gradle_dir.mkdir(parents=True, exist_ok=True)
        props = gradle_dir / "gradle.properties"
        line = f"android.aapt2FromMavenOverride={aapt2_path}"
        lines = props.read_text(encoding="utf-8", errors="ignore").splitlines() if props.exists() else []
        replaced = False
        new_lines = []
        for existing in lines:
            if existing.startswith("android.aapt2FromMavenOverride="):
                new_lines.append(line)
                replaced = True
            else:
                new_lines.append(existing)
        if not replaced:
            new_lines.append(line)
        props.write_text("\n".join(new_lines).strip() + "\n", encoding="utf-8")
        info["aapt2_override"] = aapt2_path
    return info


def _read_android_version(project_dir: Path) -> tuple[str, int]:
    build_gradle = project_dir / "app" / "build.gradle"
    text = build_gradle.read_text(encoding="utf-8", errors="ignore") if build_gradle.exists() else ""
    name_match = re.search(r"versionName\s+[\"']([^\"']+)[\"']", text)
    code_match = re.search(r"versionCode\s+(\d+)", text)
    version_name = name_match.group(1) if name_match else "0.0.0"
    version_code = int(code_match.group(1)) if code_match else 0
    return version_name, version_code


def _upload_core_worker_apk(apk_path: Path, *, filename: str, version_name: str, version_code: int, sha256: str, publish_url: str, changelog: list[str] | None = None) -> dict[str, Any]:
    base_url, token, worker_id = _core_worker_auth_parts()
    if not token or not worker_id:
        return {"ok": False, "error": "worker não pareado; não posso publicar APK"}
    publish_url = str(publish_url or "").strip() or f"{base_url}/core-worker/app/publish"
    boundary = "----CoreWorkerApkBoundary" + hashlib.sha256(f"{time.time()}:{os.getpid()}".encode()).hexdigest()[:24]

    def field(name: str, value: Any) -> bytes:
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode("utf-8")

    apk_bytes = apk_path.read_bytes()
    parts = [
        field("worker_id", worker_id),
        field("filename", filename),
        field("versionName", version_name),
        field("versionCode", int(version_code or 0)),
        field("sha256", sha256),
        field("requiredAgentVersion", PHONE_WORKER_VERSION),
        field("changelog", json.dumps(changelog or ["APK compilado por worker builder"], ensure_ascii=False)),
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"apk\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/vnd.android.package-archive\r\n\r\n"
        ).encode("utf-8"),
        apk_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        publish_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(5.0, _env_float("PHONE_WORKER_APK_PUBLISH_TIMEOUT_SECONDS", 60.0))) as resp:
            raw = resp.read(256 * 1024)
            data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            return data if isinstance(data, dict) else {"ok": False, "error": "resposta inválida da VPS"}
    except urllib.error.HTTPError as exc:
        raw = exc.read(64 * 1024).decode("utf-8", errors="replace")
        return {"ok": False, "status": int(exc.code), "error": _short_text(raw or exc, limit=240)}


def _apply_apk_build_debug(payload: dict[str, Any]) -> dict[str, Any]:
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    if "apk-builder" not in set(roles + capabilities):
        raise PermissionError("este worker não tem função apk-builder")
    if not _env_bool("PHONE_WORKER_APK_BUILD_ENABLED", True):
        raise PermissionError("build de APK desativado neste worker")

    source_url = str(payload.get("source_zip_url") or os.getenv("PHONE_WORKER_APK_BUILD_SOURCE_URL") or "").strip()
    if not source_url:
        raise ValueError("source_zip_url ausente; publique source-core-worker-app.zip na VPS")
    expected_source_sha = str(payload.get("source_sha256") or "").strip().lower()
    project_subdir = str(payload.get("project_subdir") or "android/core-worker-app").strip().strip("/")
    if not project_subdir or project_subdir.startswith("/") or ".." in project_subdir.split("/"):
        raise ValueError("project_subdir inválido")

    build_root = Path(os.getenv("PHONE_WORKER_APK_BUILD_DIR") or (Path.home() / "core-worker-apk-builds")).expanduser()
    work_dir = build_root / f"build-{int(time.time())}-{os.getpid()}"
    source_zip = work_dir / "source.zip"
    timeout_seconds = max(60, _env_int("PHONE_WORKER_APK_BUILD_TIMEOUT_SECONDS", 3600))
    max_source_bytes = max(1024 * 1024, _env_int("PHONE_WORKER_APK_BUILD_SOURCE_MAX_BYTES", 220 * 1024 * 1024))
    keep_workdir = _env_bool("PHONE_WORKER_APK_BUILD_KEEP_WORKDIR", False)
    started = time.time()
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        download = _download_url_to_file(source_url, source_zip, timeout=60.0, max_bytes=max_source_bytes)
        if not download.get("ok"):
            return {"ok": False, "summary": "falha baixando fonte do APK", "download": download}
        if expected_source_sha and expected_source_sha != str(download.get("sha256") or "").lower():
            return {"ok": False, "summary": "sha256 do source zip divergente", "expected": expected_source_sha, "actual": download.get("sha256")}
        source_dir = work_dir / "src"
        members = _safe_extract_zip_file(source_zip, source_dir)
        project_dir = (source_dir / project_subdir).resolve()
        if not project_dir.is_dir():
            # Compatibilidade: zip pode ter a pasta android/core-worker-app como raiz direta.
            alt = source_dir / "core-worker-app"
            if alt.is_dir():
                project_dir = alt.resolve()
            else:
                raise FileNotFoundError(f"projeto Android não encontrado: {project_subdir}")
        env = os.environ.copy()
        builder_environment = _prepare_termux_android_build(project_dir, env)
        version_name, version_code = _read_android_version(project_dir)
        version_name = str(payload.get("versionName") or payload.get("version_name") or version_name)
        version_code = int(payload.get("versionCode") or payload.get("version_code") or version_code or 0)
        gradlew = project_dir / "gradlew"
        if gradlew.exists():
            gradlew.chmod(0o755)
            cmd = [str(gradlew), "assembleDebug", "--no-daemon", "--max-workers=1"]
        else:
            if not shutil.which("gradle"):
                raise FileNotFoundError("gradle não encontrado no worker builder")
            cmd = ["gradle", "assembleDebug", "--no-daemon", "--max-workers=1"]
        if not env.get("ANDROID_HOME"):
            default_sdk = Path.home() / "android-sdk"
            if default_sdk.exists():
                env["ANDROID_HOME"] = str(default_sdk)
                env.setdefault("ANDROID_SDK_ROOT", str(default_sdk))
                env["PATH"] = f"{default_sdk}/cmdline-tools/latest/bin:{default_sdk}/platform-tools:" + env.get("PATH", "")
        proc = subprocess.run(cmd, cwd=str(project_dir), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds)
        stdout = _sanitize_log_text(proc.stdout or "", limit=18000)
        stderr = _sanitize_log_text(proc.stderr or "", limit=18000)
        if proc.returncode != 0:
            return {"ok": False, "summary": "build do APK falhou", "returncode": proc.returncode, "stdout_tail": stdout[-9000:], "stderr_tail": stderr[-9000:], "work_dir": str(work_dir)}
        apk_candidates = sorted((project_dir / "app" / "build" / "outputs" / "apk" / "debug").glob("*.apk"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not apk_candidates:
            return {"ok": False, "summary": "build terminou mas APK não foi encontrado", "work_dir": str(work_dir)}
        apk_path = apk_candidates[0]
        raw = apk_path.read_bytes()
        apk_sha = hashlib.sha256(raw).hexdigest()
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(payload.get("filename") or f"CoreWorker-v{version_name}-debug.apk")).strip("-._")
        if not filename.lower().endswith(".apk"):
            filename += ".apk"
        result: dict[str, Any] = {
            "ok": True,
            "summary": f"APK {version_name} compilado pelo worker",
            "versionName": version_name,
            "versionCode": version_code,
            "apk": {"filename": filename, "bytes": len(raw), "sha256": apk_sha},
            "source": {"url": source_url, "bytes": download.get("bytes"), "sha256": download.get("sha256"), "files": members},
            "builder_environment": builder_environment,
            "duration_seconds": round(time.time() - started, 3),
        }
        if bool(payload.get("publish", True)):
            publish_url = str(payload.get("publish_url") or "").strip()
            base_url, _token, _worker_id = _core_worker_auth_parts()
            publish = _upload_core_worker_apk(
                apk_path,
                filename=filename,
                version_name=version_name,
                version_code=version_code,
                sha256=apk_sha,
                publish_url=publish_url or f"{base_url}/core-worker/app/publish",
                changelog=list(payload.get("changelog") or ["APK compilado por worker builder"]),
            )
            result["publish"] = publish
            if not bool(publish.get("ok", False)):
                result["ok"] = False
                result["summary"] = "APK compilado, mas publicação na VPS falhou"
        return result
    finally:
        if not keep_workdir:
            with contextlib.suppress(Exception):
                shutil.rmtree(work_dir)

_WORKER_UPDATE_TARGETS: dict[str, tuple[str, str, int]] = {
    "phone_worker.py": ("worker", "phone_worker.py", 0o755),
    "start-phone-worker.sh": ("worker", "start-phone-worker.sh", 0o755),
    "watch-phone-worker.sh": ("worker", "watch-phone-worker.sh", 0o755),
    "pair-phone-worker.sh": ("worker", "pair-phone-worker.sh", 0o755),
    "bootstrap-phone-worker.sh": ("worker", "bootstrap-phone-worker.sh", 0o755),
    "install.sh": ("worker", "install.sh", 0o755),
    "README.md": ("worker", "README.md", 0o644),
    "phone-worker.env.example": ("worker", "phone-worker.env.example", 0o600),
}


def _safe_update_target_path(target: str) -> tuple[Path, int]:
    clean = str(target or "").replace("\\", "/").split("/")[-1].strip()
    if clean not in _WORKER_UPDATE_TARGETS:
        raise ValueError(f"arquivo de update não permitido: {clean or '<vazio>'}")
    location, filename, mode = _WORKER_UPDATE_TARGETS[clean]
    base = _phone_worker_dir() if location == "worker" else Path.home()
    path = (base / filename).expanduser()
    return path, mode


def _apply_worker_update(payload: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_SELF_UPDATE_ENABLED", True):
        raise PermissionError("self-update do phone-worker desativado por configuração")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("payload de update sem arquivos")
    if len(files) > 8:
        raise ValueError("arquivos demais no update")

    updated: list[dict[str, Any]] = []
    errors: list[str] = []
    total = 0
    max_file_bytes = max(1024, _env_int("PHONE_WORKER_UPDATE_MAX_FILE_BYTES", 512 * 1024))
    max_total_bytes = max(max_file_bytes, _env_int("PHONE_WORKER_UPDATE_MAX_TOTAL_BYTES", 1024 * 1024))

    for item in files:
        if not isinstance(item, dict):
            errors.append("item inválido")
            continue
        target = str(item.get("target") or item.get("name") or "").strip()
        try:
            path, mode = _safe_update_target_path(target)
            raw = _b64decode(str(item.get("data_b64") or ""), max_bytes=max_file_bytes)
            total += len(raw)
            if total > max_total_bytes:
                raise ValueError("update grande demais")
            expected = str(item.get("sha256") or "").strip().lower()
            actual = hashlib.sha256(raw).hexdigest()
            if expected and expected != actual:
                raise ValueError(f"sha256 divergente em {target}")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                backup = path.with_suffix(path.suffix + ".bak")
                with contextlib.suppress(Exception):
                    shutil.copy2(path, backup)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(raw)
            os.chmod(tmp, int(item.get("mode") or mode))
            tmp.replace(path)
            applied_paths = [path]
            if path.name in {"start-phone-worker.sh", "watch-phone-worker.sh", "pair-phone-worker.sh", "bootstrap-phone-worker.sh"}:
                # Espelhar scripts em ~/ também para instalações antigas e atalhos existentes.
                home_copy = _home_script(path.name)
                try:
                    if home_copy != path:
                        shutil.copy2(path, home_copy)
                        os.chmod(home_copy, int(item.get("mode") or mode))
                        applied_paths.append(home_copy)
                except Exception as mirror_exc:
                    errors.append(f"{target} mirror: {type(mirror_exc).__name__}: {_short_text(mirror_exc, limit=80)}")
            updated.append({"target": path.name, "paths": [str(p) for p in applied_paths], "bytes": len(raw), "sha256": actual[:12]})
        except Exception as exc:
            errors.append(f"{target or '<sem alvo>'}: {type(exc).__name__}: {_short_text(exc, limit=100)}")

    if errors:
        return {"ok": False, "summary": "update parcial/falhou", "updated": updated, "errors": errors[:8], "total_bytes": total}

    result: dict[str, Any] = {
        "ok": True,
        "summary": f"update aplicado: {len(updated)} arquivo(s)",
        "updated": updated,
        "total_bytes": total,
        "current_version": PHONE_WORKER_VERSION,
        "target_version": _short_text(payload.get("version"), limit=48, default="desconhecida"),
    }
    if _env_bool("PHONE_WORKER_UPDATE_RESTART", bool(payload.get("restart", True))):
        result.update({
            "deferred_restart": True,
            "_deferred_phone_worker_action": "restart",
            "_deferred_phone_worker_session": str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker"),
            "_deferred_start_script": str(_best_script("start-phone-worker.sh")),
        })
    return result


def _launch_deferred_phone_worker_action(result: dict[str, Any]) -> None:
    action = str(result.pop("_deferred_phone_worker_action", "") or "").strip().lower()
    if action not in {"stop", "restart"}:
        return
    session = str(result.pop("_deferred_phone_worker_session", "") or os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker")
    start_script = Path(str(result.pop("_deferred_start_script", "") or _best_script("start-phone-worker.sh"))).expanduser()
    worker_dir = _phone_worker_dir()
    script = worker_dir / f".core-worker-deferred-{action}.sh"
    lines = [
        "#!/data/data/com.termux/files/usr/bin/bash",
        "set +e",
        "sleep 1",
        f"tmux kill-session -t {shlex.quote(session)} >/dev/null 2>&1 || true",
        "pkill -f 'phone_worker.py' >/dev/null 2>&1 || true",
    ]
    if action == "restart":
        lines.extend([
            "sleep 1",
            f"bash {shlex.quote(str(start_script))} >/dev/null 2>&1 &",
        ])
    try:
        worker_dir.mkdir(parents=True, exist_ok=True)
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o700)
        subprocess.Popen(["bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        print(f"[core-worker-service] falha ao agendar {action}: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)


def _worker_logs_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    lines = max(20, min(400, _env_int("CORE_WORKER_LOG_LINES", int(payload.get("lines") or 120))))
    path = Path(str(payload.get("path") or _phone_worker_log_file())).expanduser()
    if not path.exists() or not path.is_file():
        return {"ok": False, "path": str(path), "error": "log não encontrado"}
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except Exception as exc:
        return {"ok": False, "path": str(path), "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"}
    text = _sanitize_log_text("\n".join(data), limit=16000)
    return {
        "ok": True,
        "path": str(path),
        "lines": len(data),
        "tail": text,
        "error_lines": sum(1 for line in data if re.search(r"error|erro|exception|traceback|falha|failed", line, re.IGNORECASE)),
    }

def _task_runner(max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> WorkerHandler:
    runner = WorkerHandler.__new__(WorkerHandler)
    runner.server = SimpleNamespace(
        max_body_bytes=max_body_bytes,
        max_output_bytes=max_output_bytes,
        job_timeout=job_timeout,
        worker_token="",
    )
    return runner


def _sanitize_job_result(result: dict[str, Any]) -> dict[str, Any]:
    max_bytes = max(4096, _env_int("CORE_WORKER_JOB_RESULT_MAX_BYTES", DEFAULT_CORE_JOB_RESULT_MAX_BYTES))
    try:
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except Exception:
        return {"ok": False, "error": "resultado não serializável"}
    if len(raw) <= max_bytes:
        return result
    return {
        "ok": bool(result.get("ok", True)),
        "truncated": True,
        "original_json_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "keys": sorted(str(key)[:80] for key in result.keys())[:40],
        "summary": "resultado grande demais para salvar no registry; use rota direta do phone-worker para payload pesado",
    }


def _execute_core_worker_job(job: dict[str, Any], *, max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> dict[str, Any]:
    global JOBS_STARTED, JOBS_FAILED
    kind = str(job.get("type") or "").strip().lower().replace("-", "_")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    JOBS_STARTED += 1
    try:
        if kind in {"ping", "status"}:
            result = _system_status()
        elif kind in {"diagnostic_basic", "worker_self_check"}:
            result = {
                "ok": True,
                "summary": "saúde do worker coletada",
                "system": _safe_telemetry("system", _system_status, {"ok": False}),
                "battery": _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot()),
                "network": _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}),
                "ping": _safe_telemetry("vps ping", _vps_tcp_ping_snapshot, {"available": False, "reachable": False, "source": "telemetry_failed"}),
                "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"}),
                "services": {
                    "phone-worker": _safe_telemetry("service phone-worker", lambda: _service_status("phone-worker"), {"ok": False}),
                    "phone-worker-watch": _safe_telemetry("service phone-worker-watch", lambda: _service_status("phone-worker-watch"), {"ok": False}),
                    "tailscale": _safe_telemetry("service tailscale", lambda: _service_status("tailscale"), {"ok": False}),
                },
                "ffmpeg": _command_version("ffmpeg"),
                "ffprobe": _command_version("ffprobe"),
                "roles": _env_list("CORE_WORKER_ROLES", []),
                "capabilities": _env_list("CORE_WORKER_CAPABILITIES", []),
            }
        elif kind == "network_probe":
            result = {"ok": True, "summary": "rede testada", "network": _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}), "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"})}
        elif kind == "tailscale_status":
            result = {"ok": True, "summary": "status Tailscale coletado", "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"})}
        elif kind == "worker_logs":
            result = _worker_logs_snapshot(payload)
            result.setdefault("summary", "logs do phone-worker coletadas" if result.get("ok") else "falha ao coletar logs")
        elif kind == "worker_update":
            result = _apply_worker_update(payload)
            result.setdefault("summary", "arquivos do phone-worker atualizados")
        elif kind == "apk_build_debug":
            result = _apply_apk_build_debug(payload)
            result.setdefault("summary", "APK compilado/publicado pelo worker")
        elif kind == "boot_status":
            result = _termux_boot_status_snapshot()
            result.setdefault("summary", "status do boot automático coletado")
        elif kind == "boot_repair":
            result = _repair_termux_boot_script()
            result.setdefault("summary", "boot automático reparado")
        elif kind in {"service_status", "service_start", "service_stop", "service_restart"}:
            service = payload.get("service") or "phone-worker"
            action = kind.removeprefix("service_")
            result = _run_service_action(str(service), action)
            result.setdefault("summary", f"{action} {result.get('service') or service}")
        elif kind == "ffmpeg_check":
            result = _command_version("ffmpeg")
            result.setdefault("summary", "ffmpeg verificado")
        elif kind == "ffprobe_check":
            result = _command_version("ffprobe")
            result.setdefault("summary", "ffprobe verificado")
        elif kind in {"zip_validate", "log_summary", "text_stats", "maintenance_plan"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            if kind == "zip_validate":
                result = runner._task_zip_validate(payload)
            elif kind == "log_summary":
                result = runner._task_log_summary(payload)
            elif kind == "text_stats":
                result = runner._task_text_stats(payload)
            else:
                result = runner._task_maintenance_plan(payload)
            result.setdefault("summary", kind)
        else:
            raise ValueError("job não permitido pelo worker")
        result.setdefault("ok", True)
        deferred = {key: result.pop(key) for key in list(result.keys()) if key.startswith("_deferred_")}
        clean = _sanitize_job_result(result)
        clean.update(deferred)
        return clean
    except Exception as exc:
        JOBS_FAILED += 1
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


def _send_core_worker_job_result(*, job_id: str, status: str, result: dict[str, Any] | None = None, error: str = "", timeout: float = 8.0) -> bool:
    _base_url, _token, worker_id = _core_worker_auth_parts()
    if not worker_id:
        return False
    safe_result = dict(result or {})
    payload = {
        "worker_id": worker_id,
        "job_id": job_id,
        "status": status,
        "result": safe_result,
        "error": _short_text(error, limit=240),
        "summary": _short_text(safe_result.get("summary") or error or status, limit=160),
    }
    code, data = _post_core_worker_json("/core-worker/jobs/result", payload, timeout=timeout)
    if 200 <= code < 300 and data.get("ok", True):
        return True
    print(f"[core-worker-jobs] falha ao enviar resultado HTTP {code}: {_short_text(data.get('error') or data, limit=180)}", flush=True)
    return False


def _poll_core_worker_job_once(*, host: str, port: int, max_body_bytes: int, max_output_bytes: int, job_timeout: int, timeout: float = 8.0) -> bool:
    _base_url, _token, worker_id = _core_worker_auth_parts()
    if not worker_id:
        return False
    payload = _core_worker_payload(host=host, port=port)
    code, data = _post_core_worker_json("/core-worker/jobs/poll", payload, timeout=timeout)
    if not (200 <= code < 300):
        print(f"[core-worker-jobs] poll HTTP {code}: {_short_text(data.get('error') or data, limit=180)}", flush=True)
        return False
    job = data.get("job") if isinstance(data.get("job"), dict) else None
    if not job:
        return False
    job_id = str(job.get("job_id") or "").strip()
    kind = str(job.get("type") or "").strip()
    if not job_id:
        return False
    print(f"[core-worker-jobs] executando {job_id} ({kind})", flush=True)
    try:
        result = _execute_core_worker_job(job, max_body_bytes=max_body_bytes, max_output_bytes=max_output_bytes, job_timeout=job_timeout)
        result_ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        ok = _send_core_worker_job_result(job_id=job_id, status="succeeded" if result_ok else "failed", result=result, error="" if result_ok else str(result.get("summary") or "ação falhou"), timeout=timeout)
        if ok and result_ok:
            _launch_deferred_phone_worker_action(result)
    except Exception as exc:
        _send_core_worker_job_result(job_id=job_id, status="failed", result={}, error=f"{type(exc).__name__}: {exc}", timeout=timeout)
    return True


def _start_core_worker_jobs(*, host: str, port: int, max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> None:
    if not _core_worker_jobs_configured():
        print("[core-worker-jobs] desativado ou incompleto; habilite CORE_WORKER_HEARTBEAT_ENABLED/JOBS e configure URL, ID e TOKEN", flush=True)
        return
    interval = max(3.0, min(120.0, _env_float("CORE_WORKER_JOB_POLL_INTERVAL_SECONDS", DEFAULT_JOB_POLL_INTERVAL_SECONDS)))

    def loop() -> None:
        while True:
            try:
                ran_job = _poll_core_worker_job_once(
                    host=host,
                    port=port,
                    max_body_bytes=max_body_bytes,
                    max_output_bytes=max_output_bytes,
                    job_timeout=job_timeout,
                    timeout=8.0,
                )
                time.sleep(0.5 if ran_job else interval)
            except Exception as exc:
                print(f"[core-worker-jobs] loop falhou: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)
                time.sleep(interval)

    thread = threading.Thread(target=loop, name="core-worker-jobs", daemon=True)
    thread.start()
    print(f"[core-worker-jobs] polling ativo; intervalo={int(interval)}s", flush=True)

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
    parser.add_argument("--jobs-once", action="store_true", help="faz um poll de job na VPS, executa no máximo um job e encerra")
    parser.add_argument("--pair", "--pair-code", dest="pair_code", default="", help="pareia este phone-worker com o registry usando um código CORE-XXXX")
    parser.add_argument("--vps-url", default=os.getenv("CORE_WORKER_VPS_URL", ""), help="URL base da VPS/Tailscale para pareamento, ex: http://100.x.x.x:8766")
    parser.add_argument("--worker-id", default=os.getenv("CORE_WORKER_ID", ""), help="ID estável deste worker; opcional")
    parser.add_argument("--name", default=os.getenv("CORE_WORKER_NAME", os.getenv("PHONE_WORKER_NAME", "")), help="nome exibido no painel")
    parser.add_argument("--roles", default=os.getenv("CORE_WORKER_ROLES", ""), help="roles do worker separadas por vírgula")
    parser.add_argument("--capabilities", default=os.getenv("CORE_WORKER_CAPABILITIES", ""), help="capacidades do worker separadas por vírgula")
    parser.add_argument("--env-file", default=os.getenv("PHONE_WORKER_ENV", str(Path.home() / ".phone-worker.env")), help="arquivo .env local a atualizar no pareamento")
    args = parser.parse_args()

    max_body_bytes = max(1, args.max_body_mb) * 1024 * 1024
    max_output_bytes = max(1, args.max_output_mb) * 1024 * 1024
    job_timeout = max(3, args.job_timeout)

    if args.pair_code:
        result = _pair_core_worker(
            code=args.pair_code,
            vps_url=args.vps_url,
            host=args.host,
            port=args.port,
            worker_id=args.worker_id,
            name=args.name,
            roles=args.roles,
            capabilities=args.capabilities,
            env_file=args.env_file,
            timeout=10.0,
        )
        return 0 if result.get("ok") else 1
    if args.heartbeat_once:
        ok = _send_core_worker_heartbeat_once(host=args.host, port=args.port, timeout=8.0)
        return 0 if ok else 1
    if args.jobs_once:
        ok = _poll_core_worker_job_once(
            host=args.host,
            port=args.port,
            max_body_bytes=max_body_bytes,
            max_output_bytes=max_output_bytes,
            job_timeout=job_timeout,
            timeout=8.0,
        )
        return 0 if ok else 1

    server = ThreadingHTTPServer((args.host, args.port), WorkerHandler)
    server.worker_token = args.token
    server.phone_worker_host = args.host
    server.phone_worker_port = args.port
    server.max_body_bytes = max_body_bytes
    server.max_output_bytes = max_output_bytes
    server.job_timeout = job_timeout
    print(f"[phone-worker] ouvindo em {args.host}:{args.port}; token={'sim' if args.token else 'não'}; versão={PHONE_WORKER_VERSION}", flush=True)
    _start_core_worker_heartbeat(host=args.host, port=args.port)
    _start_core_worker_jobs(
        host=args.host,
        port=args.port,
        max_body_bytes=max_body_bytes,
        max_output_bytes=max_output_bytes,
        job_timeout=job_timeout,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
