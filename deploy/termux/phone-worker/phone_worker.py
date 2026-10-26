#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import colorsys
import hashlib
import http.client
import io
import importlib
import json
import os
import platform
import re
import shutil
import socket
import shlex
import stat
import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import subprocess
import tempfile
import sys
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

_PHONE_WORKER_CONFIG_MODULE: Any = None
_PHONE_WORKER_CONFIG_LOCK = threading.Lock()
_PHONE_WORKER_TELEMETRY_MODULE: Any = None
_PHONE_WORKER_TELEMETRY_LOCK = threading.Lock()
_PHONE_WORKER_SSH_MODULE: Any = None
_PHONE_WORKER_SSH_LOCK = threading.Lock()
_PHONE_WORKER_CONTROL_PLANE_MODULE: Any = None
_PHONE_WORKER_CONTROL_PLANE_LOCK = threading.Lock()
_PHONE_WORKER_VOICE_STATE_MODULE: Any = None
_PHONE_WORKER_VOICE_STATE_LOCK = threading.Lock()
_PHONE_WORKER_TTS_POLICY_MODULE: Any = None
_PHONE_WORKER_TTS_POLICY_LOCK = threading.Lock()
_PHONE_WORKER_TTS_CACHE_MODULE: Any = None
_PHONE_WORKER_TTS_CACHE_LOCK = threading.Lock()
_PHONE_WORKER_TTS_ANDROID_MODULE: Any = None
_PHONE_WORKER_TTS_ANDROID_LOCK = threading.Lock()
_PHONE_WORKER_TTS_PROVIDERS_MODULE: Any = None
_PHONE_WORKER_TTS_PROVIDERS_LOCK = threading.Lock()
_PHONE_WORKER_PCM_IO_MODULE: Any = None
_PHONE_WORKER_PCM_IO_LOCK = threading.Lock()


def _phone_worker_music_bridge_module(name: str) -> Any:
    """Load one music-domain bridge lazily so single-file recovery can boot."""
    clean = str(name or "").strip().replace("-", "_")
    if not clean or not re.fullmatch(r"[a-z_][a-z0-9_]*", clean):
        raise ValueError("módulo musical inválido")
    cached = _PHONE_WORKER_MUSIC_BRIDGE_MODULES.get(clean)
    if cached is not None:
        return cached
    with _PHONE_WORKER_MUSIC_BRIDGE_LOCK:
        cached = _PHONE_WORKER_MUSIC_BRIDGE_MODULES.get(clean)
        if cached is not None:
            return cached
        here = Path(__file__).resolve()
        worker_root = str(here.parent)
        if worker_root not in sys.path:
            sys.path.insert(0, worker_root)
        for candidate in (here.parent, *here.parents):
            if (candidate / "cogs" / "musica" / "runtime_telefone" / "ponte_worker").is_dir():
                value = str(candidate)
                if value not in sys.path:
                    sys.path.insert(0, value)
                break
        try:
            module = importlib.import_module(f"cogs.musica.runtime_telefone.ponte_worker.{clean}")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"ponte musical {clean}.py ainda não foi instalada; aguarde o segundo estágio do auto-update"
            ) from exc
        _PHONE_WORKER_MUSIC_BRIDGE_MODULES[clean] = module
        return module

try:
    from PIL import Image, ImageSequence  # type: ignore
except Exception:
    Image = None
    ImageSequence = None

START_TIME = time.time()
JOBS_STARTED = 0
JOBS_FAILED = 0
_PING_CACHE: dict[str, Any] = {}
_ANDROID_TTS_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "data": {}}
_CORE_WORKER_NETWORK_STATE: dict[str, Any] = {
    "last_ok_at": 0.0, "last_error_at": 0.0, "last_error": "", "last_error_kind": "",
    "failure_streak": 0, "backoff_until": 0.0,
}
_CORE_WORKER_NETWORK_LOCK = threading.RLock()
_CORE_WORKER_HTTP_STATE: dict[str, Any] = {
    "last_ok_at": 0.0, "last_error_at": 0.0, "last_status": 0,
    "last_source": "", "last_error": "", "failure_streak": 0,
}
_CORE_WORKER_NETWORK_AUDIT: dict[str, dict[str, Any]] = {}
_CONTROL_PLANE_SNAPSHOT_CACHE: dict[str, dict[str, Any]] = {}
_CONTROL_PLANE_SNAPSHOT_CACHE_LOCK = threading.RLock()
_CORE_JOB_LOCK = threading.RLock()
_CORE_JOB_ACTIVE: dict[str, Any] = {}
_CORE_JOB_LAST_RESULT: dict[str, Any] = {}
_PENDING_CORE_JOB_RESULTS: dict[str, dict[str, Any]] = {}
_HEAVY_RESOURCE_LOCK = threading.Lock()
_TETO_BACKGROUND_STATE = {"lock": threading.Lock(), "cancelled": None, "foreground_waiters": 0}
_TETO_RENDERER_LOCK = threading.RLock()
_TETO_RENDERER: Any = None
_TETO_RENDERER_ERROR = ""
_TETO_RENDERER_CONFIG = ""
_MIKU_RENDERER: Any = None
_MIKU_RENDERER_CONFIG = ""
_TETO_BATTERY_CACHE_LOCK = threading.RLock()
_TETO_BATTERY_CACHE: dict[str, Any] = {"at": 0.0, "data": {}}
_TETO_BATTERY_MONITOR_STARTED = False
_PHONE_WORKER_MUSIC_BRIDGE_MODULES: dict[str, Any] = {}
_PHONE_WORKER_MUSIC_BRIDGE_LOCK = threading.Lock()

DEFAULT_MAX_BODY_MB = 32
DEFAULT_MAX_OUTPUT_MB = 32
DEFAULT_TIMEOUT_SECONDS = 45
PHONE_WORKER_VERSION = "1.11.43"
CORE_WORKER_RUNTIME_MODE = "termux"
CORE_WORKER_INTERNAL_RUNTIME_STATE = "termux"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_JOB_POLL_INTERVAL_SECONDS = 10
DEFAULT_CORE_JOB_RESULT_MAX_BYTES = 256 * 1024
TERMUX_PRIMARY_HTTP_PORT = 8766
TERMUX_RECOVERY_HTTP_PORT = 8768
_DIRECT_HTTP_STATE = "starting"
_EFFECTIVE_HTTP_PORT: int | None = None
_LAST_HEARTBEAT_OK_AT = 0.0
_RUNTIME_STATUS_LOCK = threading.RLock()


def _early_env_truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    if text in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    return default


def _load_env_file_once(path: Path, *, override: bool = False) -> dict[str, str]:
    loaded: dict[str, str] = {}
    try:
        lines = path.expanduser().read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return loaded
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = _decode_env_value(value)
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def _music_agent_env_file_early() -> Path:
    try:
        return _phone_worker_music_bridge_module("configuracao").music_agent_env_file()
    except RuntimeError:
        worker_dir = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
        return Path(os.getenv("MUSIC_AGENT_ENV") or worker_dir / "secrets" / "music-agent.env").expanduser()


def _ensure_music_agent_token_env(*, persist: bool = True) -> str:
    return _phone_worker_music_bridge_module("configuracao").ensure_music_agent_token(persist=persist)


def _load_phone_worker_runtime_env() -> None:
    phone_env = Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env")
    _load_env_file_once(phone_env, override=False)
    try:
        module = _phone_worker_music_bridge_module("configuracao")
    except RuntimeError:
        # O bootstrap antigo pode iniciar primeiro só com phone_worker.py.
        return
    module.load_runtime_env(phone_worker_env=phone_env)


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
    "music_ytdlp_resolve",
    "music_agent_command",
    "music_agent_status",
    "network_probe",
    "ssh_connection_diagnostic",
    "ping",
    "service_restart",
    "service_start",
    "service_status",
    "service_stop",
    "sha256",
    "status",
    "tailscale_status",
    "text_stats",
    "tts_cache_lookup",
    "tts_cache_store",
    "tts_synthesize_benchmark",
    "tts_agent_status",
    "tts_agent_synthesize",
    "voice_agent_status",
    "voice_agent_register_session",
    "voice_agent_clear_session",
    "voice_agent_guild_status",
    "voice_agent_register_handoff",
    "voice_agent_clear_handoff",
    "voice_agent_handoff_status",
    "voice_agent_prepare_transfer",
    "voice_agent_begin_transfer",
    "voice_agent_release_transfer",
    "voice_agent_transfer_status",
    "voice_agent_play_tts",
    "voice_agent_probe_connection",
    "voice_agent_connection_status",
    "voice_agent_clear_connection",
    "worker_logs",
    "worker_self_check",
    "worker_update",
    "vps_assist_probe",
    "hash_batch",
    "endpoint_probe",
    "emoji_recolor",
    "media_probe",
    "audio_convert",
    "log_digest",
    "zip_audit",
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
    "music_ytdlp_resolve",
    "music_agent_command",
    "music_agent_status",
    "network_probe",
    "ssh_connection_diagnostic",
    "ping",
    "service_restart",
    "service_start",
    "service_status",
    "service_stop",
    "status",
    "tailscale_status",
    "text_stats",
    "tts_cache_lookup",
    "tts_cache_store",
    "tts_synthesize_benchmark",
    "tts_agent_status",
    "tts_agent_synthesize",
    "voice_agent_status",
    "voice_agent_register_session",
    "voice_agent_clear_session",
    "voice_agent_guild_status",
    "voice_agent_register_handoff",
    "voice_agent_clear_handoff",
    "voice_agent_handoff_status",
    "voice_agent_prepare_transfer",
    "voice_agent_begin_transfer",
    "voice_agent_release_transfer",
    "voice_agent_transfer_status",
    "voice_agent_play_tts",
    "voice_agent_probe_connection",
    "voice_agent_connection_status",
    "voice_agent_clear_connection",
    "worker_logs",
    "worker_self_check",
    "worker_update",
    "vps_assist_probe",
    "hash_batch",
    "endpoint_probe",
    "emoji_recolor",
    "media_probe",
    "audio_convert",
    "log_digest",
    "zip_audit",
    "boot_status",
    "boot_repair",
    "zip_validate",
)

CORE_WORKER_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "leve": {
        "label": "Leve",
        "roles": ["phone-worker", "diagnostics", "log-summary", "vps-assist"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "vps-assist", "hash-worker", "endpoint-probe", "worker-logs", "network-probe", "tailscale-status"],
    },
    "midia": {
        "label": "Mídia",
        "roles": ["phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist", "hash-worker", "endpoint-probe", "media-probe", "audio-convert", "worker-logs", "network-probe", "tailscale-status"],
    },
    "completo": {
        "label": "Completo",
        "roles": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist", "cache-worker"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist", "cache-worker", "hash-worker", "endpoint-probe", "media-probe", "audio-convert", "worker-logs", "network-probe", "tailscale-status", "service-control"],
    },
    "builder": {
        "label": "Builder",
        "roles": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "vps-assist", "cache-worker"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "vps-assist", "cache-worker", "hash-worker", "endpoint-probe", "media-probe", "worker-logs", "network-probe", "tailscale-status", "boot-repair", "service-control"],
    },
    "turbo": {
        "label": "Turbo",
        "roles": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "tts-synth", "tts-benchmark", "tts-agent", "voice-agent", "vps-assist", "cache-worker"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "tts-synth", "tts-benchmark", "tts-agent", "tts-gtts", "tts-edge", "tts-android-native", "tts-teto", "voice-agent", "worker-voice", "shared-voice-session", "vps-assist", "cache-worker", "hash-worker", "endpoint-probe", "media-probe", "audio-convert", "emoji-recolor", "worker-logs", "network-probe", "tailscale-status", "service-control"],
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


def _decode_env_value(value: str) -> str:
    """Read legacy JSON/plain values and the shell-safe escapes written now.

    Kept in the facade so a lone entrypoint can still load its startup config.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1]:
        if value[0] == '"':
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                # In JSON an escaped $/backtick is invalid; in a shell it is
                # required to prevent interpolation. Decode only after trying
                # legacy JSON so existing literal backslashes remain intact.
                with contextlib.suppress(ValueError, TypeError):
                    return json.loads(value.replace("\\$", "$").replace("\\`", "`"))
            return value[1:-1]
        if value[0] == "'":
            return value[1:-1]
    return value


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
        os.environ.setdefault(key, _decode_env_value(value))


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except Exception:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    return _parse_env_list(os.getenv(name, ""), default)


def _parse_env_list(value: Any, default: list[str] | None = None) -> list[str]:
    default = list(default or [])
    raw = str(value or "").strip()
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


def _merge_profile_contract(configured: list[str], required: list[str]) -> list[str]:
    """Preserva extensões locais sem deixar o perfil perder capacidades-base.

    Instalações antigas persistiram CORE_WORKER_ROLES/CAPABILITIES no env. Quando
    um preset ganhou uma capacidade nova, esse snapshot antigo continuava
    substituindo o preset inteiro e o painel mostrava Turbo enquanto o registry
    não recebia apk-builder. O perfil escolhido passa a ser o contrato mínimo;
    valores do env continuam aceitos como extensões.
    """
    merged: list[str] = []
    for item in list(required or []) + list(configured or []):
        clean = str(item or "").strip().lower()
        if clean and clean not in merged:
            merged.append(clean)
    return merged


def _current_core_worker_roles_and_capabilities() -> tuple[list[str], list[str]]:
    profile = _current_core_worker_profile()
    preset_roles = _core_worker_profile_roles(profile)
    preset_capabilities = _core_worker_profile_capabilities(profile)
    roles = _merge_profile_contract(_env_list("CORE_WORKER_ROLES", []), preset_roles)
    capabilities = _merge_profile_contract(_env_list("CORE_WORKER_CAPABILITIES", []), preset_capabilities)
    if _ssh_transport_enabled():
        capabilities = [item for item in capabilities if item != "tailscale-status"]
        if "ssh-transport" not in capabilities:
            capabilities.append("ssh-transport")
    return roles, capabilities


def _supported_core_worker_job_types() -> list[str]:
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    caps = set(roles + capabilities)
    allowed = list(SUPPORTED_CORE_WORKER_JOB_TYPES)
    # Funções de assistência só aparecem quando o perfil permite ajudar a VPS.
    if "vps-assist" not in caps:
        allowed = [item for item in allowed if item not in {"vps_assist_probe", "hash_batch", "endpoint_probe", "log_digest", "zip_audit"}]
    if "maintenance-plan" not in caps and "cache-worker" not in caps:
        allowed = [item for item in allowed if item != "maintenance_plan"]
    if "service-control" not in caps:
        allowed = [item for item in allowed if item not in {"service_status", "service_start", "service_stop", "service_restart"}]
    if "boot-repair" not in caps:
        allowed = [item for item in allowed if item not in {"boot_status", "boot_repair"}]
    if "ffprobe" not in caps and "media-probe" not in caps:
        allowed = [item for item in allowed if item != "media_probe"]
    if "ffmpeg" not in caps and "audio-convert" not in caps and "tts-convert" not in caps:
        allowed = [item for item in allowed if item != "audio_convert"]
    return allowed


def _current_core_worker_profile() -> str:
    return _normalize_core_worker_profile(os.getenv("CORE_WORKER_PROFILE") or os.getenv("PHONE_WORKER_PROFILE") or "midia")


def _phone_worker_config_module() -> Any:
    global _PHONE_WORKER_CONFIG_MODULE
    with _PHONE_WORKER_CONFIG_LOCK:
        if _PHONE_WORKER_CONFIG_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/config.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_config", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("config.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _PHONE_WORKER_CONFIG_MODULE = module
        return _PHONE_WORKER_CONFIG_MODULE


def _safe_env_key(value: Any) -> str:
    return _phone_worker_config_module().safe_env_key(value)


def _format_env_value(value: Any) -> str:
    return _phone_worker_config_module().format_env_value(value)


def _update_env_file(path: str | None, updates: dict[str, Any]) -> Path:
    env_path = Path(path or os.getenv("PHONE_WORKER_ENV") or str(Path.home() / ".phone-worker.env")).expanduser()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    wanted = {_safe_env_key(k): str(v if v is not None else "") for k, v in updates.items()}
    existing = env_path.read_text(encoding="utf-8", errors="ignore").splitlines() if env_path.exists() else []
    content = _phone_worker_config_module().render_env_lines(existing, wanted, _format_env_value)
    env_path.write_text(content, encoding="utf-8")
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


def _music_stream_ttl_seconds() -> float:
    return _phone_worker_music_bridge_module("streams").stream_ttl_seconds()


def _cleanup_music_streams_unlocked(now: float | None = None) -> None:
    _phone_worker_music_bridge_module("streams").cleanup_streams_unlocked(now)


def _register_music_stream(item: dict[str, Any]) -> str:
    return _phone_worker_music_bridge_module("streams").register_stream(item)


def _music_stream_lookup(stream_id: str) -> dict[str, Any] | None:
    return _phone_worker_music_bridge_module("streams").stream_lookup(stream_id)


def _safe_ffmpeg_header_lines(headers: Any) -> str:
    return _phone_worker_music_bridge_module("streams").safe_ffmpeg_header_lines(headers)


def _music_pcm_cache_dir() -> Path:
    return _phone_worker_music_bridge_module("streams").pcm_cache_dir()


def _music_prepared_mode_enabled() -> bool:
    return _phone_worker_music_bridge_module("streams").prepared_mode_enabled()


def _music_prepare_timeout_seconds(item: dict[str, Any]) -> float:
    return _phone_worker_music_bridge_module("streams").prepare_timeout_seconds(item)


def _music_prepare_max_duration_seconds() -> float:
    return _phone_worker_music_bridge_module("streams").prepare_max_duration_seconds()


def _music_pcm_cache_max_bytes() -> int:
    return _phone_worker_music_bridge_module("streams").pcm_cache_max_bytes()


def _cleanup_music_prepared_file(item: dict[str, Any]) -> None:
    _phone_worker_music_bridge_module("streams").cleanup_prepared_file(item)


def _cleanup_music_pcm_cache() -> None:
    _phone_worker_music_bridge_module("streams").cleanup_pcm_cache()


def _phone_worker_pcm_io_module() -> Any:
    global _PHONE_WORKER_PCM_IO_MODULE
    if _PHONE_WORKER_PCM_IO_MODULE is not None:
        return _PHONE_WORKER_PCM_IO_MODULE
    with _PHONE_WORKER_PCM_IO_LOCK:
        if _PHONE_WORKER_PCM_IO_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/pcm_io.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_pcm_io", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("pcm_io.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = ("build_ffmpeg_input_cmd", "prepare_file", "serve_prepared", "stream_live")
            if not all(callable(getattr(module, name, None)) for name in required):
                raise RuntimeError("pcm_io.py incompleto")
            _PHONE_WORKER_PCM_IO_MODULE = module
        return _PHONE_WORKER_PCM_IO_MODULE


def _music_stream_build_ffmpeg_input_cmd(item: dict[str, Any], *, output: str) -> list[str]:
    return _phone_worker_music_bridge_module("streams").build_ffmpeg_input_cmd(item, output=output)


def _assert_music_preparation_owner_unlocked(stream_id: str, owner: dict[str, Any] | None) -> None:
    _phone_worker_music_bridge_module("streams").assert_preparation_owner_unlocked(stream_id, owner)


def _prepare_music_pcm_file(stream_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return _phone_worker_music_bridge_module("streams").prepare_pcm_file(stream_id, item)


def _prepare_music_pcm_file_owned(stream_id: str, item: dict[str, Any], owner: dict[str, Any] | None) -> dict[str, Any]:
    return _phone_worker_music_bridge_module("streams")._prepare_pcm_file_owned(stream_id, item, owner)


def _serve_prepared_music_pcm(handler: BaseHTTPRequestHandler, stream_id: str, item: dict[str, Any]) -> None:
    _phone_worker_music_bridge_module("streams").serve_prepared_pcm(handler, stream_id, item)

def _stream_music_pcm(handler: BaseHTTPRequestHandler, stream_id: str) -> None:
    _phone_worker_music_bridge_module("streams").stream_pcm(handler, stream_id, send_error=_error)

def _format_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while abs(size) >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


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


def _phone_worker_telemetry_module() -> Any:
    global _PHONE_WORKER_TELEMETRY_MODULE
    if _PHONE_WORKER_TELEMETRY_MODULE is not None:
        return _PHONE_WORKER_TELEMETRY_MODULE
    with _PHONE_WORKER_TELEMETRY_LOCK:
        if _PHONE_WORKER_TELEMETRY_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/telemetry.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_telemetry", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("telemetry.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _PHONE_WORKER_TELEMETRY_MODULE = module
        return _PHONE_WORKER_TELEMETRY_MODULE


def _sysfs_battery_snapshot() -> dict[str, Any]:
    try:
        return _phone_worker_telemetry_module().sysfs_battery_snapshot(
            Path("/sys/class/power_supply"), list_candidates=lambda root: root.glob("BAT*"),
            path_exists=_safe_path_exists,
            read_text=_read_text_file, empty_snapshot=_empty_battery_snapshot)
    except (PermissionError, OSError):
        return _empty_battery_snapshot("sysfs_permission_denied")
    except Exception as exc:
        return _empty_battery_snapshot("sysfs_error", exc)


def _battery_snapshot() -> dict[str, Any]:
    try:
        return _phone_worker_telemetry_module().battery_snapshot(
            run_json_command=_run_json_command, sysfs_snapshot=_sysfs_battery_snapshot,
            empty_snapshot=_empty_battery_snapshot)
    except (PermissionError, OSError) as exc:
        return _empty_battery_snapshot("battery_permission_denied", exc)
    except Exception as exc:
        return _empty_battery_snapshot("battery_error", exc)


def _refresh_teto_battery_cache() -> dict[str, Any]:
    """Refresh the expensive Termux battery probe outside the Teto hot path."""
    snapshot = _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot())
    with _TETO_BATTERY_CACHE_LOCK:
        _TETO_BATTERY_CACHE["at"] = time.monotonic()
        _TETO_BATTERY_CACHE["data"] = dict(snapshot) if isinstance(snapshot, dict) else {}
        return dict(_TETO_BATTERY_CACHE["data"])


def _teto_cached_battery_snapshot() -> tuple[dict[str, Any], float | None]:
    """Return battery telemetry without ever spawning termux-battery-status.

    The process-wide monitor keeps the authoritative Termux API snapshot warm.
    Before the first refresh (or in isolated validation/tests), sysfs is the
    bounded, local fallback. A stale monitor value is preferable to blocking a
    synthesis request on an Android IPC subprocess; its age is exposed so the
    caller can observe monitor health.
    """
    now = time.monotonic()
    with _TETO_BATTERY_CACHE_LOCK:
        cached = _TETO_BATTERY_CACHE.get("data")
        cached_at = float(_TETO_BATTERY_CACHE.get("at") or 0.0)
        if isinstance(cached, dict) and cached:
            age_ms = max(0.0, (now - cached_at) * 1000.0) if cached_at else None
            return dict(cached), age_ms
    snapshot = _safe_telemetry("battery-sysfs", _sysfs_battery_snapshot, _empty_battery_snapshot())
    return (dict(snapshot) if isinstance(snapshot, dict) else {}), None


def _start_teto_battery_monitor() -> bool:
    """Start one daemon refresher so Teto admission never blocks on Termux API."""
    global _TETO_BATTERY_MONITOR_STARTED
    if not (_env_bool("PHONE_WORKER_TETO_ENABLED", False) or _env_bool("PHONE_WORKER_MIKU_ENABLED", False)):
        return False
    with _TETO_BATTERY_CACHE_LOCK:
        if _TETO_BATTERY_MONITOR_STARTED:
            return True
        _TETO_BATTERY_MONITOR_STARTED = True

    def run() -> None:
        interval = max(5.0, min(60.0, _env_float("PHONE_WORKER_TETO_RESOURCE_REFRESH_SECONDS", 15.0)))
        while True:
            try:
                _refresh_teto_battery_cache()
            except Exception as exc:
                print(
                    f"[phone-worker] monitor de bateria Teto falhou: {type(exc).__name__}: {_short_text(exc, limit=100)}",
                    flush=True,
                )
            time.sleep(interval)

    threading.Thread(target=run, name="teto-battery-monitor", daemon=True).start()
    return True


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


def _ssh_transport_enabled() -> bool:
    return str(os.getenv("PHONE_WORKER_TRANSPORT") or "legacy").strip().lower() == "ssh"


def _phone_worker_ssh_module() -> Any:
    """Load the optional transport from the release or its stable recovery copy."""
    global _PHONE_WORKER_SSH_MODULE
    if _PHONE_WORKER_SSH_MODULE is not None:
        return _PHONE_WORKER_SSH_MODULE
    with _PHONE_WORKER_SSH_LOCK:
        if _PHONE_WORKER_SSH_MODULE is None:
            relative = Path("phone_worker_runtime/ssh_transport.py")
            candidates = [Path(__file__).resolve().parent / relative,
                          Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser() / relative]
            path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if path is None:
                raise RuntimeError("módulo de transporte SSH ainda não instalado")
            name = "core_phone_worker_runtime_ssh_transport"
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError("módulo de transporte SSH inválido")
            module = importlib.util.module_from_spec(spec)
            # Dataclasses and introspection require the dynamically loaded module
            # to be registered while its definitions execute.
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(name, None)
                raise
            if not callable(getattr(module, "connection_snapshot", None)):
                raise RuntimeError("módulo SSH não contém connection_snapshot")
            _PHONE_WORKER_SSH_MODULE = module
        return _PHONE_WORKER_SSH_MODULE


def _ssh_connection_snapshot() -> dict[str, Any]:
    """Read only compact transport observations; never connect or run a shell."""
    if not _ssh_transport_enabled():
        return {"mode": "legacy"}
    try:
        raw = _phone_worker_ssh_module().connection_snapshot()
        if not isinstance(raw, dict):
            raise TypeError("snapshot SSH não é objeto")
        allowed = {"mode", "state", "interactive_state", "bulk_state", "last_ok_at", "rtt_ms",
                   "reconnect_count", "generation", "last_error", "worker_ready"}
        result = {key: value for key, value in raw.items() if key in allowed and
                  (value is None or isinstance(value, (str, int, float, bool)))}
        for key, value in list(result.items()):
            if isinstance(value, str):
                result[key] = _sanitize_log_text(value, limit=180 if key == "last_error" else 64)
            elif isinstance(value, float) and (value != value or abs(value) == float("inf")):
                result[key] = None
        result["mode"] = "ssh"
        result.setdefault("state", "unknown")
        rtt = result.get("rtt_ms")
        if isinstance(rtt, bool) or not isinstance(rtt, (int, float)) or rtt <= 0:
            result["rtt_ms"] = None
        return result
    except Exception as exc:
        return {"mode": "ssh", "state": "not_configured", "interactive_state": "unknown",
                "bulk_state": "unknown", "last_error": type(exc).__name__ + ": observação SSH indisponível"}


def _tailscale_snapshot(*, probe_vps: bool = False) -> dict[str, Any]:
    if _ssh_transport_enabled():
        result = {"connected": False, "cli_available": False, "state": "not_required",
                  "via_vps_url": False, "note": "Comunicação configurada por SSH"}
        if probe_vps:
            ping = _vps_tcp_ping_snapshot()
            result["vps_reachable"] = bool(ping.get("reachable"))
            if ping.get("latency_ms") is not None:
                result["vps_latency_ms"] = ping["latency_ms"]
        return result
    try:
        return _phone_worker_telemetry_module().tailscale_snapshot(
            probe_vps=probe_vps, auth_parts=_core_worker_auth_parts, base_url_host=_base_url_host,
            looks_like_tailscale_host=_looks_like_tailscale_host, find_command=shutil.which,
            run_text_command=_run_text_command, mask_ipv4=_mask_ipv4, short_text=_short_text,
            request_factory=urllib.request.Request, urlopen=urllib.request.urlopen, wall_clock=time.time)
    except Exception as exc:
        return {"connected": False, "state": "telemetry_failed",
                "error": f"{type(exc).__name__}: {_short_text(exc, limit=100)}"}


def _vps_tcp_ping_snapshot(*, timeout: float = 2.5, cache_ttl: float = 6.0) -> dict[str, Any]:
    try:
        if _ssh_transport_enabled():
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            return _phone_worker_telemetry_module().vps_http_ping_snapshot(
                timeout=timeout, cache_ttl=cache_ttl, auth_parts=_core_worker_auth_parts,
                ping_cache=_PING_CACHE, monotonic=time.monotonic, perf_counter=time.perf_counter,
                request_factory=urllib.request.Request, urlopen=opener.open, wall_clock=time.time,
                mask_ipv4=_mask_ipv4, short_text=_short_text)
        return _phone_worker_telemetry_module().vps_tcp_ping_snapshot(
            timeout=timeout, cache_ttl=cache_ttl, auth_parts=_core_worker_auth_parts,
            ping_cache=_PING_CACHE, monotonic=time.monotonic, perf_counter=time.perf_counter,
            create_connection=socket.create_connection, mask_ipv4=_mask_ipv4, short_text=_short_text)
    except Exception as exc:
        return {"available": False, "reachable": False, "source": "telemetry_failed",
                "error": f"{type(exc).__name__}: {_short_text(exc, limit=100)}"}

def _network_snapshot() -> dict[str, Any]:
    try:
        return _phone_worker_telemetry_module().network_snapshot(
            run_json_command=_run_json_command, heartbeat_configured=_heartbeat_configured,
            tailscale_snapshot=_tailscale_snapshot, safe_telemetry=_safe_telemetry,
            ping_snapshot=_vps_tcp_ping_snapshot, short_text=_short_text)
    except Exception as exc:
        return {"type": "unknown", "source": "telemetry_failed",
                "error": f"{type(exc).__name__}: {_short_text(exc, limit=100)}"}


def _heartbeat_configured() -> bool:
    if not _env_bool("CORE_WORKER_HEARTBEAT_ENABLED", True):
        return False
    return all(_core_worker_auth_parts())


def _core_worker_jobs_configured() -> bool:
    if not _env_bool("CORE_WORKER_JOBS_ENABLED", True):
        return False
    return _heartbeat_configured()


def _core_worker_auth_parts() -> tuple[str, str, str]:
    base_url = str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip().rstrip("/")
    token = str(os.getenv("CORE_WORKER_TOKEN") or "").strip()
    worker_id = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    return base_url, token, worker_id


def _classify_core_worker_network_error(exc: BaseException | str) -> str:
    text = str(exc or "").lower()
    if "no route to host" in text or "errno 113" in text:
        return "no_route_to_vps"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "network is unreachable" in text or "errno 101" in text:
        return "network_unreachable"
    if "name or service not known" in text or "temporary failure in name resolution" in text:
        return "dns_failed"
    if "connection refused" in text:
        return "connection_refused"
    if "connection reset" in text or "errno 104" in text:
        return "connection_reset"
    if "connection abort" in text or "connection aborted" in text or "errno 103" in text:
        return "connection_aborted"
    if "remote end closed" in text or "remote disconnected" in text:
        return "remote_closed"
    if "ssl" in text or "tls" in text or "certificate verify failed" in text:
        return "tls_error"
    return "request_failed"


def _core_worker_network_backoff_max_seconds() -> float:
    return max(5.0, min(300.0, _env_float("CORE_WORKER_NETWORK_BACKOFF_MAX_SECONDS", 60.0)))


def _remember_core_worker_network_ok() -> None:
    now = time.time()
    with _CORE_WORKER_NETWORK_LOCK:
        _CORE_WORKER_NETWORK_STATE.update({
            "last_ok_at": now,
            "last_error": "",
            "last_error_kind": "",
            "failure_streak": 0,
            "backoff_until": 0.0,
        })


def _remember_core_worker_network_error(exc: BaseException | str) -> None:
    now = time.time()
    kind = _classify_core_worker_network_error(exc)
    with _CORE_WORKER_NETWORK_LOCK:
        previous_error_at = float(_CORE_WORKER_NETWORK_STATE.get("last_error_at") or 0.0)
        previous_kind = str(_CORE_WORKER_NETWORK_STATE.get("last_error_kind") or "")
        # Uma janela longa sem falha reinicia a exponencial; trocar de classe de
        # erro também reinicia para não herdar penalidade de um incidente antigo.
        if previous_kind != kind or not previous_error_at or now - previous_error_at > 180.0:
            streak = 1
        else:
            streak = max(1, int(_CORE_WORKER_NETWORK_STATE.get("failure_streak") or 0) + 1)
        backoff = min(_core_worker_network_backoff_max_seconds(), 2.0 ** min(6, max(0, streak - 1)))
        _CORE_WORKER_NETWORK_STATE.update({
            "last_error_at": now,
            "last_error": _short_text(exc, limit=160),
            "last_error_kind": kind,
            "failure_streak": streak,
            "backoff_until": now + backoff,
        })


def _core_worker_network_retry_delay() -> float:
    with _CORE_WORKER_NETWORK_LOCK:
        until = float(_CORE_WORKER_NETWORK_STATE.get("backoff_until") or 0.0)
    return max(0.0, until - time.time())


def _audit_core_worker_network_failure(source: str, exc: BaseException | str, *, elapsed_ms: float = 0.0) -> None:
    kind = _classify_core_worker_network_error(exc)
    now = time.time()
    interval = max(15.0, min(600.0, _env_float("CORE_WORKER_NETWORK_ERROR_LOG_INTERVAL_SECONDS", 120.0)))
    with _CORE_WORKER_NETWORK_LOCK:
        item = _CORE_WORKER_NETWORK_AUDIT.setdefault(source, {"kind": "", "logged_at": 0.0, "suppressed": 0})
        should_log = item.get("kind") != kind or now - float(item.get("logged_at") or 0.0) >= interval
        if not should_log:
            item["suppressed"] = int(item.get("suppressed") or 0) + 1
            return
        suppressed = int(item.get("suppressed") or 0)
        item.update({"kind": kind, "logged_at": now, "suppressed": 0})
        streak = int(_CORE_WORKER_NETWORK_STATE.get("failure_streak") or 0)
        backoff = max(0.0, float(_CORE_WORKER_NETWORK_STATE.get("backoff_until") or 0.0) - now)
    suffix = f" suppressed={suppressed}" if suppressed else ""
    print(
        f"[core-worker-network] falha source={source} kind={kind} elapsed_ms={round(elapsed_ms, 1)} "
        f"streak={streak} backoff_seconds={round(backoff, 1)}{suffix}: "
        f"{type(exc).__name__ if isinstance(exc, BaseException) else 'Error'}: {_short_text(exc, limit=120)}",
        flush=True,
    )


def _audit_core_worker_network_success(source: str, *, elapsed_ms: float = 0.0, transport_recorded: bool = False) -> None:
    # Uma resposta HTTP (inclusive 4xx/5xx) prova que a rota até a VPS está
    # funcional. Limpe de verdade o breaker; antes este helper só limpava a
    # supressão de logs e deixava jobs bloqueados por um incidente já recuperado.
    # HTTP helpers record the response before returning. Do not record it twice:
    # another concurrent request may have failed between transport and audit.
    if not transport_recorded:
        _remember_core_worker_network_ok()
    with _CORE_WORKER_NETWORK_LOCK:
        item = _CORE_WORKER_NETWORK_AUDIT.get(source)
        if not item or not item.get("kind"):
            return
        previous = str(item.get("kind") or "")
        suppressed = int(item.get("suppressed") or 0)
        item.update({"kind": "", "logged_at": time.time(), "suppressed": 0})
    suffix = f" suppressed={suppressed}" if suppressed else ""
    print(
        f"[core-worker-network] recuperado source={source} previous={previous} elapsed_ms={round(elapsed_ms, 1)}{suffix}",
        flush=True,
    )


def _remember_core_worker_http_success(source: str) -> None:
    now = time.time()
    with _CORE_WORKER_NETWORK_LOCK:
        _CORE_WORKER_HTTP_STATE["last_ok_at"] = now
        if not source or str(_CORE_WORKER_HTTP_STATE.get("last_source") or "") == str(source):
            _CORE_WORKER_HTTP_STATE["failure_streak"] = 0


def _audit_core_worker_http_failure(source: str, status: int, detail: object, *, elapsed_ms: float = 0.0) -> None:
    """Audita falha HTTP sem confundi-la com queda de rede/Tailscale."""
    now = time.time()
    status = int(status or 0)
    detail_text = _short_text(detail, limit=160)
    key = f"http:{source}:{status}"
    interval = max(15.0, min(600.0, _env_float("CORE_WORKER_HTTP_ERROR_LOG_INTERVAL_SECONDS", 120.0)))
    with _CORE_WORKER_NETWORK_LOCK:
        previous_source = str(_CORE_WORKER_HTTP_STATE.get("last_source") or "")
        previous_status = int(_CORE_WORKER_HTTP_STATE.get("last_status") or 0)
        streak = int(_CORE_WORKER_HTTP_STATE.get("failure_streak") or 0)
        if previous_source == str(source) and previous_status == status:
            streak += 1
        else:
            streak = 1
        _CORE_WORKER_HTTP_STATE.update({
            "last_error_at": now,
            "last_status": status,
            "last_source": str(source or ""),
            "last_error": detail_text,
            "failure_streak": streak,
        })
        item = _CORE_WORKER_NETWORK_AUDIT.setdefault(key, {"logged_at": 0.0, "suppressed": 0})
        if now - float(item.get("logged_at") or 0.0) < interval:
            item["suppressed"] = int(item.get("suppressed") or 0) + 1
            return
        suppressed = int(item.get("suppressed") or 0)
        item.update({"logged_at": now, "suppressed": 0})
    suffix = f" suppressed={suppressed}" if suppressed else ""
    print(
        f"[core-worker-http] falha source={source} status={status} elapsed_ms={round(elapsed_ms, 1)} "
        f"streak={streak}{suffix}: {detail_text}",
        flush=True,
    )


def _audit_core_worker_slow_payload(*, prepare_ms: float, request_ms: float, total_ms: float) -> None:
    now = time.time()
    interval = max(30.0, min(3600.0, _env_float("CORE_WORKER_SLOW_PAYLOAD_LOG_INTERVAL_SECONDS", 300.0)))
    with _CORE_WORKER_NETWORK_LOCK:
        item = _CORE_WORKER_NETWORK_AUDIT.setdefault("payload", {"logged_at": 0.0, "suppressed": 0})
        if now - float(item.get("logged_at") or 0.0) < interval:
            item["suppressed"] = int(item.get("suppressed") or 0) + 1
            return
        suppressed = int(item.get("suppressed") or 0)
        item.update({"logged_at": now, "suppressed": 0})
    suffix = f" suppressed={suppressed}" if suppressed else ""
    print(
        f"[core-worker-heartbeat] payload_lento prepare_ms={round(prepare_ms, 1)} "
        f"request_ms={round(request_ms, 1)} total_ms={round(total_ms, 1)}{suffix}",
        flush=True,
    )


def _core_worker_network_runtime_snapshot() -> dict[str, Any]:
    now = time.time()
    with _CORE_WORKER_NETWORK_LOCK:
        state = dict(_CORE_WORKER_NETWORK_STATE)
    last_ok = float(state.get("last_ok_at") or 0.0)
    last_error = float(state.get("last_error_at") or 0.0)
    result = {
        "last_ok_age_seconds": round(now - last_ok, 3) if last_ok else None,
        "last_error_age_seconds": round(now - last_error, 3) if last_error else None,
        "last_error_kind": state.get("last_error_kind") or "",
        "last_error": state.get("last_error") or "",
    }
    streak = int(state.get("failure_streak") or 0)
    backoff = max(0.0, float(state.get("backoff_until") or 0.0) - now)
    if streak:
        result["failure_streak"] = streak
    if backoff > 0:
        result["backoff_seconds"] = round(backoff, 3)
    with _CORE_WORKER_NETWORK_LOCK:
        http_state = dict(_CORE_WORKER_HTTP_STATE)
    http_error_at = float(http_state.get("last_error_at") or 0.0)
    http_ok_at = float(http_state.get("last_ok_at") or 0.0)
    if http_error_at:
        result["last_http_error_age_seconds"] = round(now - http_error_at, 3)
        result["last_http_status"] = int(http_state.get("last_status") or 0)
        result["last_http_source"] = str(http_state.get("last_source") or "")
        result["last_http_error"] = str(http_state.get("last_error") or "")
    if http_ok_at:
        result["last_http_ok_age_seconds"] = round(now - http_ok_at, 3)
    http_streak = int(http_state.get("failure_streak") or 0)
    if http_streak:
        result["http_failure_streak"] = http_streak
    return result


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
    response_limit = max(1024 * 1024, min(8 * 1024 * 1024, _env_int("CORE_WORKER_JSON_RESPONSE_MAX_BYTES", 4 * 1024 * 1024)))
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(response_limit + 1)
            status = int(getattr(resp, "status", 200) or 200)
            _remember_core_worker_network_ok()
    except urllib.error.HTTPError as exc:
        try:
            with exc:
                raw = exc.read(16 * 1024)
        except Exception as read_exc:
            _remember_core_worker_network_error(read_exc)
            raise
        status = int(exc.code)
        _remember_core_worker_network_ok()
    except Exception as exc:
        _remember_core_worker_network_error(exc)
        raise
    parsed: dict[str, Any]
    if len(raw) > response_limit:
        return status, {"ok": False, "error": "resposta JSON da VPS grande demais"}
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
            _remember_core_worker_network_ok()
    except urllib.error.HTTPError as exc:
        try:
            with exc:
                raw = exc.read(16 * 1024)
        except Exception as read_exc:
            _remember_core_worker_network_error(read_exc)
            raise
        status = int(exc.code)
        _remember_core_worker_network_ok()
    except Exception as exc:
        _remember_core_worker_network_error(exc)
        raise
    if len(raw) > max_bytes:
        return status, {"ok": False, "error": "resposta JSON grande demais"}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        parsed = data if isinstance(data, dict) else {"ok": False, "error": "resposta não é objeto"}
    except Exception as exc:
        parsed = {"ok": False, "error": f"JSON inválido: {type(exc).__name__}"}
    return status, parsed


def _get_local_json_url(url: str, *, timeout: float = 4.0, max_bytes: int = 128 * 1024) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"CorePhoneWorkerLocal/{PHONE_WORKER_VERSION}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(max_bytes + 1)
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        with exc:
            raw = exc.read(min(max_bytes, 16 * 1024))
        status = int(exc.code)
    if len(raw) > max_bytes:
        return status, {"ok": False, "error": "resposta local grande demais"}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        return status, data if isinstance(data, dict) else {"ok": False, "error": "resposta local não é objeto"}
    except Exception as exc:
        return status, {"ok": False, "error": f"JSON local inválido: {type(exc).__name__}"}


def _post_local_json_url(url: str, payload: dict[str, Any], *, timeout: float = 4.0, max_bytes: int = 128 * 1024) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", "User-Agent": f"CorePhoneWorkerLocal/{PHONE_WORKER_VERSION}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(max_bytes + 1)
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        with exc:
            raw = exc.read(min(max_bytes, 16 * 1024))
        status = int(exc.code)
    if len(raw) > max_bytes:
        return status, {"ok": False, "error": "resposta local grande demais"}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        return status, data if isinstance(data, dict) else {"ok": False, "error": "resposta local não é objeto"}
    except Exception as exc:
        return status, {"ok": False, "error": f"JSON local inválido: {type(exc).__name__}"}


def _download_url_to_file(
    url: str,
    target: Path,
    *,
    timeout: float = 35.0,
    max_bytes: int = 150 * 1024 * 1024,
    accept: str = "application/vnd.android.package-archive,*/*",
    size_label: str = "APK",
) -> dict[str, Any]:
    headers = {
        "Accept": accept,
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
            _remember_core_worker_network_ok()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"{size_label} grande demais para este worker")
                digest.update(chunk)
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        try:
            with exc:
                body = exc.read(8 * 1024).decode("utf-8", errors="replace")
            _remember_core_worker_network_ok()
        except Exception as read_exc:
            _remember_core_worker_network_error(read_exc)
            raise
        finally:
            with contextlib.suppress(Exception):
                tmp.unlink()
        return {"ok": False, "status": int(exc.code), "error": _short_text(body or exc, limit=180)}
    except Exception as exc:
        _remember_core_worker_network_error(exc)
        with contextlib.suppress(Exception):
            tmp.unlink()
        raise
    tmp.replace(target)
    return {"ok": True, "status": status, "path": str(target), "bytes": total, "sha256": digest.hexdigest()}


def _post_core_worker_json(
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float = 8.0,
    respect_network_backoff: bool = False,
) -> tuple[int, dict[str, Any]]:
    base_url, token, _worker_id = _core_worker_auth_parts()
    if not base_url or not token:
        return 0, {"ok": False, "error": "Core Worker não configurado"}
    if respect_network_backoff:
        retry_delay = _core_worker_network_retry_delay()
        if retry_delay > 0:
            return 0, {
                "ok": False,
                "error": "network_backoff",
                "retry_after_seconds": round(retry_delay, 3),
            }
    # Each urllib call owns its connection. A stalled poll/result must not hold
    # heartbeat behind a process-wide lock for another full socket timeout.
    return _post_json_url(f"{base_url}{path}", payload, token=token, timeout=timeout)


def _core_job_runtime_snapshot() -> dict[str, Any]:
    with _CORE_JOB_LOCK:
        active = dict(_CORE_JOB_ACTIVE)
        last = dict(_CORE_JOB_LAST_RESULT)
        pending = len(_PENDING_CORE_JOB_RESULTS)
    return {
        "configured": _core_worker_jobs_configured(),
        "active": bool(active),
        "active_job_id": active.get("job_id") or "",
        "active_type": active.get("type") or "",
        "active_since": active.get("started_at") or 0,
        "last_result_job_id": last.get("job_id") or "",
        "last_result_type": last.get("type") or "",
        "last_result_status": last.get("status") or "",
        "last_result_summary": last.get("summary") or "",
        "last_result_at": last.get("finished_at") or 0,
        "last_result_sent": bool(last.get("sent_ok")),
        "pending_results": pending,
    }


def _set_core_job_active(job: dict[str, Any]) -> None:
    with _CORE_JOB_LOCK:
        _CORE_JOB_ACTIVE.clear()
        _CORE_JOB_ACTIVE.update({
            "job_id": str(job.get("job_id") or ""),
            "type": str(job.get("type") or ""),
            "started_at": time.time(),
        })


def _finish_core_job(job_id: str, kind: str, status: str, *, summary: str = "", sent_ok: bool = False) -> None:
    with _CORE_JOB_LOCK:
        _CORE_JOB_ACTIVE.clear()
        _CORE_JOB_LAST_RESULT.clear()
        _CORE_JOB_LAST_RESULT.update({
            "job_id": str(job_id or ""),
            "type": str(kind or ""),
            "status": str(status or ""),
            "summary": _short_text(summary or status, limit=160),
            "finished_at": time.time(),
            "sent_ok": bool(sent_ok),
        })


def _store_pending_core_job_result(payload: dict[str, Any]) -> None:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        return
    safe_payload = dict(payload)
    safe_payload.setdefault("stored_at", time.time())
    with _CORE_JOB_LOCK:
        _PENDING_CORE_JOB_RESULTS[job_id] = safe_payload
    _persist_pending_core_job_results()


def _post_core_worker_job_result_payload_status(payload: dict[str, Any], *, timeout: float = 8.0) -> tuple[bool, int, dict[str, Any]]:
    started = time.perf_counter()
    try:
        code, data = _post_core_worker_json(
            "/core-worker/jobs/result", payload, timeout=timeout, respect_network_backoff=True
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # Não deixe uma queda entre "job terminou" e "resultado enviado"
        # escapar como exceção. O chamador precisa receber False para persistir
        # o resultado e reenviá-lo quando o plano de controle voltar.
        _audit_core_worker_network_failure("job_result", exc, elapsed_ms=elapsed_ms)
        return False, 0, {"ok": False, "error": f"{type(exc).__name__}: {_short_text(exc, limit=140)}"}
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if code == 0 and str(data.get("error") or "") == "network_backoff":
        return False, 0, data
    if code:
        _audit_core_worker_network_success("job_result", elapsed_ms=elapsed_ms, transport_recorded=True)
    ok = bool(200 <= code < 300 and data.get("ok", True))
    if ok:
        _remember_core_worker_http_success("job_result")
        return True, int(code), data
    _audit_core_worker_http_failure(
        "job_result", int(code), data.get("error") or data, elapsed_ms=elapsed_ms
    )
    return False, int(code), data


def _post_core_worker_job_result_payload(payload: dict[str, Any], *, timeout: float = 8.0) -> bool:
    ok, _code, _data = _post_core_worker_job_result_payload_status(payload, timeout=timeout)
    return ok


def _pending_core_job_result_count() -> int:
    _load_persisted_pending_core_job_results()
    with _CORE_JOB_LOCK:
        return len(_PENDING_CORE_JOB_RESULTS)


def _flush_pending_core_worker_job_results(*, timeout: float = 8.0) -> int:
    _load_persisted_pending_core_job_results()
    with _CORE_JOB_LOCK:
        pending = list(_PENDING_CORE_JOB_RESULTS.items())[:5]
    sent = 0
    changed = False
    for job_id, payload in pending:
        ok, code, data = _post_core_worker_job_result_payload_status(payload, timeout=timeout)
        if ok:
            with _CORE_JOB_LOCK:
                _PENDING_CORE_JOB_RESULTS.pop(job_id, None)
                if _CORE_JOB_LAST_RESULT.get("job_id") == job_id:
                    _CORE_JOB_LAST_RESULT["sent_ok"] = True
            changed = True
            sent += 1
            continue
        if _job_result_rejection_is_permanent(code, data):
            _archive_pending_core_job_result(job_id, payload, reason=f"VPS recusou resultado antigo HTTP {code}", response=data)
            with _CORE_JOB_LOCK:
                _PENDING_CORE_JOB_RESULTS.pop(job_id, None)
            changed = True
            print(f"[core-worker-jobs] resultado pendente antigo arquivado e removido: {job_id}", flush=True)
    if changed:
        _persist_pending_core_job_results()
    return sent


def _runtime_state_dir() -> Path:
    configured = str(os.getenv("PHONE_WORKER_STATE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "core-worker-phone-worker"


def _runtime_status_path() -> Path:
    return _runtime_state_dir() / "runtime-status.json"


def _write_runtime_status(*, control_plane_alive: bool, heartbeat_ok: bool | None = None, reason: str = "") -> None:
    global _LAST_HEARTBEAT_OK_AT
    now = time.time()
    if heartbeat_ok is True:
        _LAST_HEARTBEAT_OK_AT = now
    payload = {
        "schema": 2,
        "runtime_kind": "termux",
        "runtime_mode": "termux",
        "source": "termux-phone-worker",
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "pid": os.getpid(),
        "version": PHONE_WORKER_VERSION,
        "source_hash": _phone_worker_source_hash(),
        "started_at": START_TIME,
        "updated_at": now,
        "control_plane_alive": bool(control_plane_alive),
        "last_heartbeat_ok_at": _LAST_HEARTBEAT_OK_AT or None,
        "direct_http_state": _DIRECT_HTTP_STATE,
        "http_port": _EFFECTIVE_HTTP_PORT,
        "preferred_http_port": TERMUX_PRIMARY_HTTP_PORT,
        "recovery_http_port": TERMUX_RECOVERY_HTTP_PORT,
        "reason": _short_text(reason, limit=160),
    }
    try:
        path = _runtime_status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[phone-worker-state] falha ao persistir status: {type(exc).__name__}", flush=True)


def _probe_direct_http_owner(host: str, port: int, *, timeout: float = 0.8) -> str:
    probe_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    headers = {"Accept": "application/json", "User-Agent": f"CorePhoneWorkerSupervisorProbe/{PHONE_WORKER_VERSION}"}
    tokens = []
    for name in ("PHONE_WORKER_TOKEN", "CORE_WORKER_DIRECT_HTTP_TOKEN"):
        value = str(os.getenv(name) or "").strip()
        if value and value not in tokens:
            tokens.append(value)
    attempts = [""] + tokens
    for token in attempts:
        current = dict(headers)
        if token:
            current["Authorization"] = f"Bearer {token}"
        try:
            req = urllib.request.Request(f"http://{probe_host}:{int(port)}/health", headers=current, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read(128 * 1024).decode("utf-8", errors="replace"))
            if not isinstance(data, dict):
                continue
            kind = str(data.get("runtime_kind") or "").strip().lower()
            source = str(data.get("source") or "").strip().lower()
            if kind == "apk" or source.startswith("core-worker-apk"):
                return "apk"
            if kind == "termux" and source == "termux-phone-worker":
                return "termux"
            return "unknown_http"
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                continue
        except Exception:
            continue
    try:
        with socket.create_connection((probe_host, int(port)), timeout=timeout):
            return "unknown_listener"
    except Exception:
        return "none"


def _bind_phone_worker_http_server(host: str, preferred_port: int, *, token: str, max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> ThreadingHTTPServer | None:
    global _DIRECT_HTTP_STATE, _EFFECTIVE_HTTP_PORT
    candidates = [int(preferred_port)]
    if int(preferred_port) == TERMUX_PRIMARY_HTTP_PORT:
        candidates.append(TERMUX_RECOVERY_HTTP_PORT)
    for index, selected_port in enumerate(candidates):
        try:
            server = ThreadingHTTPServer((host, selected_port), WorkerHandler)
        except OSError as exc:
            if getattr(exc, "errno", None) not in {98, 48, 10048}:  # Linux/macOS/Windows EADDRINUSE
                print(f"[phone-worker-http] bind {host}:{selected_port} falhou: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)
                if index == len(candidates) - 1:
                    _DIRECT_HTTP_STATE = "bind_failed_control_plane_alive"
                continue
            owner = _probe_direct_http_owner(host, selected_port)
            if selected_port == TERMUX_PRIMARY_HTTP_PORT and owner == "apk":
                _DIRECT_HTTP_STATE = "port_owned_by_apk"
            elif owner == "termux":
                _DIRECT_HTTP_STATE = "port_owned_by_old_termux"
            else:
                _DIRECT_HTTP_STATE = "port_owned_by_unknown_listener"
            print(f"[phone-worker-http] porta {selected_port} ocupada owner={owner}; agent continua com control plane ativo", flush=True)
            continue
        server.worker_token = token
        server.phone_worker_host = host
        server.phone_worker_port = selected_port
        server.max_body_bytes = max_body_bytes
        server.max_output_bytes = max_output_bytes
        server.job_timeout = job_timeout
        _EFFECTIVE_HTTP_PORT = selected_port
        if selected_port == TERMUX_PRIMARY_HTTP_PORT:
            _DIRECT_HTTP_STATE = "listening_primary"
        else:
            _DIRECT_HTTP_STATE = "listening_recovery_after_conflict"
        print(f"[phone-worker-http] ouvindo em {host}:{selected_port}; state={_DIRECT_HTTP_STATE}", flush=True)
        return server
    _EFFECTIVE_HTTP_PORT = None
    if _DIRECT_HTTP_STATE in {"starting", "port_owned_by_apk", "port_owned_by_old_termux", "port_owned_by_unknown_listener"}:
        _DIRECT_HTTP_STATE = "port_conflict_control_plane_alive"
    print(f"[phone-worker-http] sem endpoint direto; state={_DIRECT_HTTP_STATE}; heartbeat/jobs continuam ativos", flush=True)
    return None


def _bootstrap_updater_snapshot() -> dict[str, Any]:
    path = _runtime_state_dir() / "updater-status.json"
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            return {"state": "unknown"}
        allowed = {
            "state", "updated_at", "target_version", "target_source_hash", "current_version",
            "current_source_hash", "reason", "rolled_back", "bootstrap_version", "blocked_key",
        }
        return {str(k): v for k, v in data.items() if str(k) in allowed}
    except FileNotFoundError:
        return {"state": "legacy_or_not_initialized"}
    except Exception:
        return {"state": "state_unreadable"}


def _control_plane_snapshot(name: str, callback, default: dict[str, Any]) -> dict[str, Any]:
    def read():
        value = callback()
        if not isinstance(value, dict):
            raise TypeError("snapshot não é objeto")
        return value
    return _safe_telemetry(name, read, default)


def _cached_control_plane_snapshot(name: str, callback, default: dict[str, Any], *, ttl: float) -> dict[str, Any]:
    """Refresh optional probes once in the background; serve the last snapshot.

    Termux API, boot inventories and dependency/agent checks may take seconds.
    None of them should delay heartbeat/jobs or accumulate parallel collectors.
    A cold cache allows 50 ms for fast probes before using the recovery default.
    """
    now = time.monotonic()
    ttl = max(0.0, float(ttl or 0.0))
    with _CONTROL_PLANE_SNAPSHOT_CACHE_LOCK:
        cached = _CONTROL_PLANE_SNAPSHOT_CACHE.get(name)
        if cached and now - float(cached.get("at") or 0.0) <= ttl:
            data = cached.get("data")
            if isinstance(data, dict):
                return dict(data)
        if cached is None:
            cached = {"at": 0.0, "data": None}
            _CONTROL_PLANE_SNAPSHOT_CACHE[name] = cached
        event = cached.get("refresh_event")
        start_refresh = event is None
        if start_refresh:
            event = threading.Event()
            cached["refresh_event"] = event
        previous = cached.get("data")

    if start_refresh:
        def refresh() -> None:
            try:
                data = _control_plane_snapshot(name, callback, default)
                with _CONTROL_PLANE_SNAPSHOT_CACHE_LOCK:
                    # An installer may invalidate the cache while a probe runs.
                    # Do not republish that obsolete dependency snapshot.
                    if _CONTROL_PLANE_SNAPSHOT_CACHE.get(name) is cached:
                        cached.update({"at": time.monotonic(), "data": dict(data)})
            finally:
                with _CONTROL_PLANE_SNAPSHOT_CACHE_LOCK:
                    cached.pop("refresh_event", None)
                event.set()

        threading.Thread(target=refresh, name=f"control-plane-{name}", daemon=True).start()

    if isinstance(previous, dict):
        return dict(previous)
    event.wait(0.05)
    with _CONTROL_PLANE_SNAPSHOT_CACHE_LOCK:
        current = _CONTROL_PLANE_SNAPSHOT_CACHE.get(name)
        data = current.get("data") if current is cached else None
        return dict(data) if isinstance(data, dict) else dict(default)


def _phone_worker_control_plane_module() -> Any:
    global _PHONE_WORKER_CONTROL_PLANE_MODULE
    if _PHONE_WORKER_CONTROL_PLANE_MODULE is not None:
        return _PHONE_WORKER_CONTROL_PLANE_MODULE
    with _PHONE_WORKER_CONTROL_PLANE_LOCK:
        if _PHONE_WORKER_CONTROL_PLANE_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/control_plane.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_control_plane", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("control_plane.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not callable(getattr(module, "build_payload", None)):
                raise RuntimeError("control_plane.py não contém build_payload")
            _PHONE_WORKER_CONTROL_PLANE_MODULE = module
        return _PHONE_WORKER_CONTROL_PLANE_MODULE


def _core_worker_endpoint(host: str, effective_port: int | None) -> str:
    endpoint = str(os.getenv("CORE_WORKER_ENDPOINT") or os.getenv("PHONE_WORKER_ENDPOINT") or "").strip()
    if not endpoint and effective_port and host not in {"", "0.0.0.0", "::"}:
        address = f"[{host}]" if ":" in host and not host.startswith("[") else host
        endpoint = f"http://{address}:{effective_port}"
    return endpoint


def _core_worker_payload(*, host: str, port: int) -> dict[str, Any]:
    # Keep the recovery envelope independent of optional modules and probes.
    # Capture the effective port once; the caller's preferred port may not bind.
    _base_url, _token, worker_id = _core_worker_auth_parts()
    effective_port, direct_http_state = _EFFECTIVE_HTTP_PORT, _DIRECT_HTTP_STATE
    profile = _current_core_worker_profile()
    profile_label = _core_worker_profile_label(profile)
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    roles, capabilities = list(roles), list(capabilities)
    safe_mode = _phone_worker_safe_mode_enabled()
    payload = {
        "worker_id": worker_id,
        "physical_worker_id": worker_id,
        "name": _short_text(_default_worker_name(), limit=64, default="Core Phone Worker"),
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "runtime_kind": "termux",
        "runtime_mode": CORE_WORKER_RUNTIME_MODE,
        "version": PHONE_WORKER_VERSION,
        "source_hash": _phone_worker_source_hash(),
        "worker_update_transports": ["bootstrap-manifest-v2", "zip-v1", "inline-b64-v1"],
        "profile": profile,
        "profile_label": profile_label,
        "safe_mode": safe_mode,
        "endpoint": _core_worker_endpoint(host, effective_port),
        "roles": roles,
        "capabilities": capabilities,
        "supported_tasks": _supported_core_worker_job_types(),
        "health": {
            "ok": True,
            "runtime_mode": CORE_WORKER_RUNTIME_MODE,
            "internal_runtime_state": CORE_WORKER_INTERNAL_RUNTIME_STATE,
            "control_plane_alive": True,
            "direct_http_state": direct_http_state,
            "http_port": effective_port,
            "last_heartbeat_ok_at": _LAST_HEARTBEAT_OK_AT or None,
        },
        "status": {
            "connection": _ssh_connection_snapshot(),
            "core_worker_jobs": _core_job_runtime_snapshot(),
            "core_worker_network": _core_worker_network_runtime_snapshot(),
            "runtime_mode": CORE_WORKER_RUNTIME_MODE,
            "runtime": {
                "mode": CORE_WORKER_RUNTIME_MODE,
                "current_worker": "termux-phone-worker",
                "internal_runtime": CORE_WORKER_INTERNAL_RUNTIME_STATE,
                "migration_stage": "termux-bootstrap-builder",
                "summary": "Termux executa jobs reais; APK prepara runtime interno gradualmente.",
            },
            "profile": profile,
            "profile_label": profile_label,
            "http_host": host,
            "http_port": effective_port,
            "preferred_http_port": TERMUX_PRIMARY_HTTP_PORT,
            "recovery_http_port": TERMUX_RECOVERY_HTTP_PORT,
            "direct_http_state": direct_http_state,
            "control_plane_alive": True,
        },
    }
    try:
        module = _phone_worker_control_plane_module()
        telemetry_ttl = max(2.0, min(60.0, _env_float("CORE_WORKER_TELEMETRY_CACHE_SECONDS", 10.0)))
        # SSH heartbeats reuse a short, bounded readiness snapshot. Inventories
        # and dependency checks stay on the background system collector, never
        # on a heartbeat or TTS/command request thread.
        if _ssh_transport_enabled():
            music_agent = _music_agent_light_snapshot()
        else:
            music_agent = _control_plane_snapshot(
                "music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False}
            )
        system_snapshot = _cached_control_plane_snapshot(
            "system", _system_status, {"ok": False}, ttl=telemetry_ttl
        )
        if _ssh_transport_enabled() and isinstance(system_snapshot.get("music_agent"), dict):
            music_agent = {**system_snapshot["music_agent"], **music_agent}
        payload = module.build_payload(payload,
            system=system_snapshot,
            battery=_cached_control_plane_snapshot("battery", _battery_snapshot, _empty_battery_snapshot(), ttl=max(telemetry_ttl, 30.0)),
            network=_cached_control_plane_snapshot("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}, ttl=telemetry_ttl),
            updater=_bootstrap_updater_snapshot())
        return _phone_worker_music_bridge_module("control_plane").estender_payload(
            payload,
            music_node=_inactive_music_node_snapshot(),
            music_agent=music_agent,
        )
    except Exception as exc:
        print(f"[phone-worker] payload de recuperação: {type(exc).__name__}: {_short_text(exc, limit=100)}", flush=True)
        payload["roles"], payload["capabilities"] = roles[:16], capabilities[:24]
        payload["health"]["payload_mode"] = "bootstrap"
        return payload


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
        "physical_worker_id": selected_worker_id,
        "name": _short_text(selected_name, limit=64, default="Core Phone Worker"),
        "source": "termux-phone-worker",
    })
    requested_roles = _parse_env_list(roles) if roles else _env_list("CORE_WORKER_ROLES", [])
    requested_capabilities = _parse_env_list(capabilities) if capabilities else _env_list("CORE_WORKER_CAPABILITIES", [])
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
        "PHONE_WORKER_CONFIG_SCHEMA": "2",
        "PHONE_WORKER_SELF_UPDATE_ENABLED": "true",
        "PHONE_WORKER_BOOTSTRAP_UPDATE_ENABLED": "true",
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
    started = time.perf_counter()
    prepare_started = started
    try:
        payload = _core_worker_payload(host=host, port=port)
        prepare_ms = (time.perf_counter() - prepare_started) * 1000.0
        request_started = time.perf_counter()
        status, data = _post_core_worker_json("/core-worker/heartbeat", payload, timeout=timeout)
        request_ms = (time.perf_counter() - request_started) * 1000.0
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if 200 <= status < 300 and data.get("ok", True):
            _audit_core_worker_network_success("heartbeat", elapsed_ms=request_ms, transport_recorded=True)
            _remember_core_worker_http_success("heartbeat")
            _write_runtime_status(control_plane_alive=True, heartbeat_ok=True, reason="heartbeat_ok")
            if prepare_ms >= max(750.0, _env_float("CORE_WORKER_SLOW_PAYLOAD_LOG_MS", 1500.0)):
                _audit_core_worker_slow_payload(prepare_ms=prepare_ms, request_ms=request_ms, total_ms=elapsed_ms)
            with contextlib.suppress(Exception):
                _flush_pending_core_worker_job_results(timeout=min(5.0, max(1.0, timeout)))
            return True
        # O servidor respondeu: a rota de rede está saudável. HTTP 4xx/5xx é
        # falha de protocolo/autorização/servidor e não deve abrir o breaker de
        # Tailscale nem pausar jobs por até 60 s.
        _audit_core_worker_network_success("heartbeat", elapsed_ms=request_ms, transport_recorded=True)
        _audit_core_worker_http_failure(
            "heartbeat", status, data.get("error") or data, elapsed_ms=request_ms
        )
    except Exception as exc:
        # _post_json_url registra o erro de rede; registrar novamente é idempotente
        # para conteúdo, mas aumentaria artificialmente a streak. Só complete caso
        # a exceção tenha ocorrido na construção do payload, antes do request.
        with _CORE_WORKER_NETWORK_LOCK:
            has_network_error = bool(_CORE_WORKER_NETWORK_STATE.get("last_error_kind"))
        if not has_network_error:
            _remember_core_worker_network_error(exc)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        _audit_core_worker_network_failure("heartbeat", exc, elapsed_ms=elapsed_ms)
    _write_runtime_status(control_plane_alive=True, heartbeat_ok=False, reason="heartbeat_failed")
    return False


def _start_core_worker_heartbeat(*, host: str, port: int) -> None:
    if not _heartbeat_configured():
        print("[core-worker-heartbeat] desativado ou incompleto; defina CORE_WORKER_VPS_URL, CORE_WORKER_ID e CORE_WORKER_TOKEN", flush=True)
        return
    interval = max(10.0, min(300.0, _env_float("CORE_WORKER_HEARTBEAT_INTERVAL_SECONDS", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)))

    def loop() -> None:
        while True:
            _send_core_worker_heartbeat_once(host=host, port=port, timeout=max(3.0, min(15.0, _env_float("CORE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 6.0))))
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


def _header_ascii(value: Any, *, limit: int = 180) -> str:
    # Zero is a valid effective control (notably the Normal TTV pitch).
    # Stringify before _short_text, whose display fallback discards false values.
    text = _short_text(str(value) if value is not None else "", limit=limit, default="")
    return text.encode("ascii", errors="ignore").decode("ascii", errors="ignore")


def _audio_response(handler: BaseHTTPRequestHandler, status: int, data: bytes, meta: dict[str, Any]) -> None:
    fmt = str(meta.get("audio_format") or meta.get("format") or "mp3").strip().lower().replace(".", "")
    content_type = "audio/mpeg"
    if fmt in {"ogg", "opus", "ogg_opus", "oggopus"}:
        fmt = "ogg"
        content_type = "audio/ogg"
    elif fmt in {"wav", "wave"}:
        fmt = "wav"
        content_type = "audio/wav"
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Core-Worker-Audio-Format", fmt)
    handler.send_header("X-Core-Worker-Engine", _header_ascii(meta.get("engine")))
    handler.send_header("X-Core-Worker-Selected-Engine", _header_ascii(meta.get("selected_engine") or meta.get("engine")))
    handler.send_header("X-Core-Worker-Cache-Hit", "true" if meta.get("cache_hit") else "false")
    handler.send_header("X-Core-Worker-Sha256", _header_ascii(meta.get("sha256"), limit=80))
    handler.send_header("X-Core-Worker-Id", _header_ascii(meta.get("worker_id"), limit=100))
    handler.send_header("X-Core-Worker-Version", _header_ascii(meta.get("worker_version"), limit=40))
    if meta.get("teto_renderer_version"):
        handler.send_header("X-Core-Worker-Teto-Renderer", _header_ascii(meta.get("teto_renderer_version"), limit=60))
    if meta.get("teto_backend"):
        handler.send_header("X-Core-Worker-Teto-Backend", _header_ascii(meta.get("teto_backend"), limit=40))
    if meta.get("teto_reading_mode"):
        handler.send_header("X-Core-Worker-Teto-Reading", _header_ascii(meta.get("teto_reading_mode"), limit=40))
    if meta.get("teto_phonemizer_version"):
        handler.send_header("X-Core-Worker-Teto-Phonemizer", _header_ascii(meta.get("teto_phonemizer_version"), limit=60))
    if meta.get("teto_epenthetic_phonemes") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Epenthetic", _header_ascii(meta.get("teto_epenthetic_phonemes"), limit=20))
    if meta.get("teto_voicebank_profile"):
        handler.send_header("X-Core-Worker-Teto-Voicebank-Profile", _header_ascii(meta.get("teto_voicebank_profile"), limit=40))
    if meta.get("teto_coverage_percent") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Coverage", _header_ascii(meta.get("teto_coverage_percent"), limit=20))
    if meta.get("teto_cluster_hits") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Cluster-Hits", _header_ascii(meta.get("teto_cluster_hits"), limit=20))
    if meta.get("teto_timeline_mode"):
        handler.send_header("X-Core-Worker-Teto-Timeline", _header_ascii(meta.get("teto_timeline_mode"), limit=40))
    if meta.get("teto_timeline_aux_overlays") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Aux-Overlays", _header_ascii(meta.get("teto_timeline_aux_overlays"), limit=20))
    if meta.get("teto_pitch_offset_semitones") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Pitch-Semitones", _header_ascii(meta.get("teto_pitch_offset_semitones"), limit=20))
    for key, header in (
        ("ttv_voice_id", "X-Core-Worker-TTV-Voice-Id"),
        ("ttv_pitch_semitones", "X-Core-Worker-TTV-Pitch-Semitones"),
        ("ttv_speech_rate", "X-Core-Worker-TTV-Speech-Rate"),
        ("ttv_language", "X-Core-Worker-TTV-Language"),
        ("ttv_performance_hash", "X-Core-Worker-TTV-Performance-Hash"),
    ):
        if meta.get(key) not in (None, ""):
            handler.send_header(header, _header_ascii(meta.get(key), limit=64 if key == "ttv_performance_hash" else 40))
    if meta.get("teto_alias_path_cost") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Path-Cost", _header_ascii(meta.get("teto_alias_path_cost"), limit=20))
    if meta.get("teto_pitch_boundary_max_cents") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Pitch-Jump", _header_ascii(meta.get("teto_pitch_boundary_max_cents"), limit=20))
    if meta.get("teto_energy_boundary_max_db") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Energy-Jump", _header_ascii(meta.get("teto_energy_boundary_max_db"), limit=20))
    if meta.get("teto_continuity_repairs") not in (None, ""):
        handler.send_header("X-Core-Worker-Teto-Continuity-Repairs", _header_ascii(meta.get("teto_continuity_repairs"), limit=20))
    for key, header in (
        ("resample_ms", "X-Core-Worker-TTV-Resample-Ms"),
        ("compose_ms", "X-Core-Worker-TTV-Compose-Ms"),
        ("postprocess_ms", "X-Core-Worker-TTV-Postprocess-Ms"),
        ("cache_maintenance_ms", "X-Core-Worker-TTV-Cache-Maintenance-Ms"),
        ("render_workers", "X-Core-Worker-TTV-Render-Workers"),
        ("source_groups", "X-Core-Worker-TTV-Source-Groups"),
        ("fragment_cache_hits", "X-Core-Worker-TTV-Fragment-Cache-Hits"),
        ("fragment_cache_misses", "X-Core-Worker-TTV-Fragment-Cache-Misses"),
        ("resampler_calls", "X-Core-Worker-TTV-Resampler-Calls"),
        ("cache_scans", "X-Core-Worker-TTV-Cache-Scans"),
        ("cache_evictions", "X-Core-Worker-TTV-Cache-Evictions"),
        ("cache_size_bytes", "X-Core-Worker-TTV-Cache-Size-Bytes"),
        ("pcm_cache_hits", "X-Core-Worker-TTV-PCM-Cache-Hits"),
        ("pcm_cache_misses", "X-Core-Worker-TTV-PCM-Cache-Misses"),
        ("pcm_cache_bytes", "X-Core-Worker-TTV-PCM-Cache-Bytes"),
        ("normalization_gain", "X-Core-Worker-TTV-Normalization-Gain"),
    ):
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            number = float(value)
        except (OverflowError, ValueError):
            continue
        if not 0 <= number < float("inf"):
            continue
        if key == 'normalization_gain':
            if 0.6 <= number <= 1.2:
                handler.send_header(header, str(number))
        elif key.endswith("_ms"):
            handler.send_header(header, str(round(number, 2)))
        elif number.is_integer():
            handler.send_header(header, str(int(number)))
    for key, header in (
        ("worker_total_ms", "X-Core-Worker-Worker-Total-Ms"),
        ("worker_synth_ms", "X-Core-Worker-Worker-Synth-Ms"),
        ("cache_read_ms", "X-Core-Worker-Cache-Read-Ms"),
        ("android_synth_ms", "X-Core-Worker-Android-Synth-Ms"),
    ):
        value = meta.get(key)
        if value not in (None, ""):
            handler.send_header(header, _header_ascii(value, limit=40))
    timing = meta.get("timing_ms") if isinstance(meta.get("timing_ms"), dict) else {}
    if timing:
        if timing.get("worker_total") not in (None, ""):
            handler.send_header("X-Core-Worker-Worker-Total-Ms", _header_ascii(timing.get("worker_total"), limit=40))
        if timing.get("worker_synth") not in (None, ""):
            handler.send_header("X-Core-Worker-Worker-Synth-Ms", _header_ascii(timing.get("worker_synth"), limit=40))
        if timing.get("cache_read") not in (None, ""):
            handler.send_header("X-Core-Worker-Cache-Read-Ms", _header_ascii(timing.get("cache_read"), limit=40))
        if timing.get("android_synth") not in (None, ""):
            handler.send_header("X-Core-Worker-Android-Synth-Ms", _header_ascii(timing.get("android_synth"), limit=40))
        if timing.get("android_roundtrip") not in (None, ""):
            handler.send_header("X-Core-Worker-Android-Roundtrip-Ms", _header_ascii(timing.get("android_roundtrip"), limit=40))
        for key, header in (
            ("preflight", "X-Core-Worker-Preflight-Ms"),
            ("dependency_snapshot", "X-Core-Worker-Dependency-Snapshot-Ms"),
            ("engine_order", "X-Core-Worker-Engine-Order-Ms"),
            ("cache_key", "X-Core-Worker-Cache-Key-Ms"),
            ("cache_lookup", "X-Core-Worker-Cache-Lookup-Ms"),
            ("teto_render", "X-Core-Worker-Teto-Render-Ms"),
            ("teto_admission", "X-Core-Worker-Teto-Admission-Ms"),
            ("cache_store", "X-Core-Worker-Cache-Store-Ms"),
        ):
            if timing.get(key) not in (None, ""):
                handler.send_header(header, _header_ascii(timing.get(key), limit=40))
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


def _with_tts_audio_payload(payload: dict[str, Any], data: bytes, *, max_bytes: int, raw_response: bool) -> dict[str, Any]:
    if len(data) > max_bytes:
        raise ValueError("resultado grande demais")
    if raw_response:
        payload["_raw_audio"] = data
    else:
        payload["data_b64"] = _b64encode(data, max_bytes=max_bytes)
    return payload


def _android_tts_enabled() -> bool:
    return _env_bool("PHONE_WORKER_ANDROID_TTS_ENABLED", True)


def _android_tts_base_url() -> str:
    value = str(os.getenv("PHONE_WORKER_ANDROID_TTS_URL") or "http://127.0.0.1:8877").strip()
    return value.rstrip("/") or "http://127.0.0.1:8877"


def _android_tts_timeout_seconds(default: float = 0.45) -> float:
    return max(0.08, min(5.0, _env_float("PHONE_WORKER_ANDROID_TTS_STATUS_TIMEOUT_SECONDS", default)))


def _phone_worker_tts_android_module() -> Any:
    global _PHONE_WORKER_TTS_ANDROID_MODULE
    if _PHONE_WORKER_TTS_ANDROID_MODULE is not None:
        return _PHONE_WORKER_TTS_ANDROID_MODULE
    with _PHONE_WORKER_TTS_ANDROID_LOCK:
        if _PHONE_WORKER_TTS_ANDROID_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/tts_android.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_tts_android", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("tts_android.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = ("json_request", "raw_request", "synthesize")
            if not all(callable(getattr(module, name, None)) for name in required):
                raise RuntimeError("tts_android.py incompleto")
            _PHONE_WORKER_TTS_ANDROID_MODULE = module
        return _PHONE_WORKER_TTS_ANDROID_MODULE


def _android_tts_json_request(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 1.5) -> dict[str, Any]:
    return _phone_worker_tts_android_module().json_request(path, payload=payload, timeout=timeout,
        base_url=_android_tts_base_url(), version=PHONE_WORKER_VERSION,
        request_factory=urllib.request.Request, open_url=urllib.request.urlopen)


def _android_tts_raw_request(path: str, *, payload: dict[str, Any], timeout: float = 1.5, max_audio_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, dict[str, Any]]:
    return _phone_worker_tts_android_module().raw_request(path, payload=payload, timeout=timeout,
        max_audio_bytes=max_audio_bytes, base_url=_android_tts_base_url(), version=PHONE_WORKER_VERSION,
        request_factory=urllib.request.Request, open_url=urllib.request.urlopen, short_text=_short_text)


def _android_tts_status(*, use_cache: bool = True) -> dict[str, Any]:
    if not _android_tts_enabled():
        return {"ok": False, "available": False, "ready": False, "enabled": False, "engine": "android_native", "last_error": "PHONE_WORKER_ANDROID_TTS_ENABLED=false"}
    now = time.monotonic()
    max_age = max(0.2, min(60.0, _env_float("PHONE_WORKER_ANDROID_TTS_STATUS_CACHE_SECONDS", 5.0)))
    cached = _ANDROID_TTS_STATUS_CACHE.get("data") if isinstance(_ANDROID_TTS_STATUS_CACHE, dict) else {}
    if use_cache and isinstance(cached, dict) and cached and now - float(_ANDROID_TTS_STATUS_CACHE.get("at") or 0.0) <= max_age:
        return dict(cached)
    try:
        timeout = _android_tts_timeout_seconds()
        data = _android_tts_json_request("/native-tts/status", timeout=timeout)
        ready = bool(data.get("ok") or data.get("ready") or data.get("available"))
        result = dict(data)
        result.update({
            "ok": ready,
            "available": ready,
            "ready": ready,
            "enabled": True,
            "engine": "android_native",
            "url": _android_tts_base_url(),
        })
    except Exception as exc:
        result = {
            "ok": False,
            "available": False,
            "ready": False,
            "enabled": True,
            "engine": "android_native",
            "url": _android_tts_base_url(),
            "last_error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}",
        }
    _ANDROID_TTS_STATUS_CACHE["at"] = now
    _ANDROID_TTS_STATUS_CACHE["data"] = dict(result)
    return result


def _android_tts_ready() -> bool:
    status = _android_tts_status(use_cache=True)
    return bool(status.get("ok") or status.get("ready") or status.get("available"))

def _android_tts_voices(*, locale: str = "", limit: int = 500) -> dict[str, Any]:
    if not _android_tts_enabled():
        return {"ok": False, "engine": "android_native", "voices": [], "error": "PHONE_WORKER_ANDROID_TTS_ENABLED=false"}
    payload = {"locale": str(locale or ""), "limit": max(1, min(600, int(limit or 500)))}
    try:
        data = _android_tts_json_request("/native-tts/voices", payload=payload, timeout=max(0.6, _android_tts_timeout_seconds(1.2)))
        voices = data.get("voices") if isinstance(data, dict) else []
        if not isinstance(voices, list):
            voices = []
        return {
            "ok": bool(data.get("ok", True)),
            "engine": "android_native",
            "locale": data.get("locale") or locale,
            "total": data.get("total", len(voices)),
            "returned": data.get("returned", len(voices)),
            "voices": voices,
        }
    except Exception as exc:
        return {"ok": False, "engine": "android_native", "locale": locale, "voices": [], "error": f"{type(exc).__name__}: {_short_text(exc, limit=140)}"}

def _safe_name(name: Any, fallback: str = "file.bin") -> str:
    text = str(name or fallback).replace("\\", "/").strip().lstrip("/")
    parts = []
    for part in text.split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        parts.append(part[:120])
    return "/".join(parts) or fallback


def _available_memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text("utf-8", errors="replace").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                return max(0, int(parts[1]) // 1024)
    except Exception:
        return None
    return None


def _teto_resource_snapshot() -> dict[str, Any]:
    memory_mb = _available_memory_mb()
    min_memory_mb = max(128, _env_int("PHONE_WORKER_TETO_MIN_FREE_MEMORY_MB", 1200))
    battery, battery_age_ms = _teto_cached_battery_snapshot()
    level = battery.get("level") if isinstance(battery, dict) else None
    temperature = battery.get("temperature_c") if isinstance(battery, dict) else None
    status = str((battery or {}).get("status") or "").strip().lower() if isinstance(battery, dict) else ""
    charging = bool((battery or {}).get("charging")) if isinstance(battery, dict) else False
    charging = charging or status in {"charging", "full"}
    min_battery = max(0, min(100, _env_int("PHONE_WORKER_TETO_MIN_BATTERY_PERCENT", 25)))
    max_temperature = max(30.0, _env_float("PHONE_WORKER_TETO_MAX_BATTERY_TEMP_C", 43.0))
    allow_low_when_charging = _env_bool("PHONE_WORKER_TETO_ALLOW_LOW_BATTERY_WHEN_CHARGING", True)
    core_job = _core_job_runtime_snapshot()
    active_type = str(core_job.get("active_type") or "").strip().lower()
    heavy_types = {
        "worker_update", "self_update",
        "boot_repair", "audio_convert", "media_convert", "maintenance_heavy",
    }
    reasons: list[str] = []
    if active_type in heavy_types:
        reasons.append(f"tarefa pesada ativa: {active_type or 'unknown'}")
    if memory_mb is not None and memory_mb < min_memory_mb:
        reasons.append(f"memória disponível baixa: {memory_mb} MB < {min_memory_mb} MB")
    try:
        if temperature is not None and float(temperature) > max_temperature:
            reasons.append(f"bateria aquecida: {float(temperature):.1f} °C > {max_temperature:.1f} °C")
    except (TypeError, ValueError):
        pass
    try:
        if level is not None and float(level) < min_battery and not (charging and allow_low_when_charging):
            reasons.append(f"bateria baixa: {float(level):.0f}% < {min_battery}%")
    except (TypeError, ValueError):
        pass
    return {
        "ok": not reasons,
        "reason": "; ".join(reasons) if reasons else "ok",
        "memory_available_mb": memory_mb,
        "minimum_memory_mb": min_memory_mb,
        "battery_level": level,
        "battery_temperature_c": temperature,
        "battery_source": str((battery or {}).get("source") or "") if isinstance(battery, dict) else "",
        "battery_snapshot_age_ms": round(battery_age_ms, 2) if battery_age_ms is not None else None,
        "charging": charging,
        "active_heavy_job": active_type,
    }


def _phone_worker_tts_providers_module() -> Any:
    global _PHONE_WORKER_TTS_PROVIDERS_MODULE
    if _PHONE_WORKER_TTS_PROVIDERS_MODULE is not None:
        return _PHONE_WORKER_TTS_PROVIDERS_MODULE
    with _PHONE_WORKER_TTS_PROVIDERS_LOCK:
        if _PHONE_WORKER_TTS_PROVIDERS_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/tts_providers.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_tts_providers", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("tts_providers.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = ("synthesize_teto", "synthesize_edge", "synthesize_gtts", "render_telemetry")
            if not all(callable(getattr(module, name, None)) for name in required):
                raise RuntimeError("tts_providers.py incompleto")
            _PHONE_WORKER_TTS_PROVIDERS_MODULE = module
        return _PHONE_WORKER_TTS_PROVIDERS_MODULE


def _teto_backend() -> str:
    backend = str(os.getenv("PHONE_WORKER_TETO_BACKEND") or "utau").strip().lower().replace("_", "-")
    # Migrate the retired backend setting without invoking a paid engine or
    # leaving an existing phone installation unable to select its UTAU assets.
    if backend == "voicepeak":
        return "utau"
    if backend not in {"utau", "worldline-r"}:
        raise ValueError("PHONE_WORKER_TETO_BACKEND deve ser utau ou worldline-r")
    return backend


def _teto_renderer_config_fingerprint(*, backend: str | None = None) -> str:
    # Only explicit renderer settings participate. Pairing/authentication and
    # retired remote-provider credentials must never enter the renderer identity.
    keys = (
        "PHONE_WORKER_TETO_ENABLED", "PHONE_WORKER_TETO_VOICEBANK_DIR",
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR", "PHONE_WORKER_TETO_VOICEBANK_MODE",
        "PHONE_WORKER_TETO_MIN_ALIASES", "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES",
        "PHONE_WORKER_TETO_BASE_PITCH", "PHONE_WORKER_TETO_SPEECH_RATE",
        "PHONE_WORKER_TETO_TEMPO", "PHONE_WORKER_TETO_VELOCITY",
        "PHONE_WORKER_TETO_MODULATION", "PHONE_WORKER_TETO_FLAGS",
        "PHONE_WORKER_TETO_LENGTH_MODE", "PHONE_WORKER_TETO_RESAMPLER_COMMAND",
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR", "PHONE_WORKER_TETO_FRAGMENT_CACHE_MB",
        "PHONE_WORKER_TETO_PCM_CACHE_MB",
        "PHONE_WORKER_TETO_STATUS_CACHE_SECONDS", "PHONE_WORKER_TETO_WORLDLINE_CONTAINER",
        "PHONE_WORKER_TETO_WORLDLINE_LIBRARY", "PHONE_WORKER_WORLDLINE_CONTAINER",
        "PHONE_WORKER_WORLDLINE_LIBRARY", "PHONE_WORKER_TETO_ALIAS_SUFFIX",
    )
    profile = {name: str(os.getenv(name) or "") for name in keys}
    profile["backend"] = backend or _teto_backend()
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()


def _get_teto_renderer():
    global _TETO_RENDERER, _TETO_RENDERER_ERROR, _TETO_RENDERER_CONFIG
    backend = _teto_backend()
    configuration = _teto_renderer_config_fingerprint(backend=backend)
    with _TETO_RENDERER_LOCK:
        if _TETO_RENDERER is not None and _TETO_RENDERER_CONFIG == configuration:
            return _TETO_RENDERER
        _TETO_RENDERER = None
        _TETO_RENDERER_CONFIG = ""
        try:
            if backend == "worldline-r":
                from teto_renderer import WorldlineRenderer
                renderer_class = WorldlineRenderer
            else:
                from teto_renderer import TetoRenderer
                renderer_class = TetoRenderer
            _TETO_RENDERER = renderer_class(resource_guard=_teto_resource_snapshot)
            _TETO_RENDERER_CONFIG = configuration
            _TETO_RENDERER_ERROR = ""
            return _TETO_RENDERER
        except Exception as exc:
            _TETO_RENDERER_ERROR = f"{type(exc).__name__}: {_short_text(exc, limit=180)}"
            raise RuntimeError(f"renderer Teto indisponível: {_TETO_RENDERER_ERROR}") from exc


def _miku_renderer_environment() -> dict[str, str]:
    """Give Miku her own renderer settings without mutating process globals."""
    # The existing renderer uses TETO-prefixed keys internally, but this is an
    # authoritative Miku profile. Never seed it from another character's values.
    environment: dict[str, str] = {}
    bank = str(os.getenv("PHONE_WORKER_MIKU_VOICEBANK_DIR") or "").strip()
    english_bank = str(os.getenv("PHONE_WORKER_MIKU_ENGLISH_VOICEBANK_DIR") or bank).strip()
    command = str(os.getenv("PHONE_WORKER_MIKU_RESAMPLER_COMMAND") or "").strip()
    if not command:
        installed_esper = Path.home() / ".esper-utau/bin/resampler.py"
        if installed_esper.is_file():
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(installed_esper))}"
    environment.update({
        "PHONE_WORKER_TETO_ENABLED": "true" if _env_bool("PHONE_WORKER_MIKU_ENABLED", False) else "false",
        "PHONE_WORKER_TETO_VOICEBANK_DIR": bank,
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": english_bank,
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "english",
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND": command,
        "PHONE_WORKER_TETO_ALIAS_SUFFIX": str(os.getenv("PHONE_WORKER_MIKU_ALIAS_SUFFIX") or "_D4").strip(),
        "PHONE_WORKER_TETO_BASE_PITCH": str(os.getenv("PHONE_WORKER_MIKU_BASE_PITCH") or "C4"),
        "PHONE_WORKER_TETO_SPEECH_RATE": str(os.getenv("PHONE_WORKER_MIKU_SPEECH_RATE") or "1.0"),
        "PHONE_WORKER_TETO_FLAGS": str(os.getenv("PHONE_WORKER_MIKU_FLAGS") or ""),
        "PHONE_WORKER_TETO_VELOCITY": str(os.getenv("PHONE_WORKER_MIKU_VELOCITY") or "100"),
        "PHONE_WORKER_TETO_MODULATION": str(os.getenv("PHONE_WORKER_MIKU_MODULATION") or "15"),
        "PHONE_WORKER_TETO_TEMPO": str(os.getenv("PHONE_WORKER_MIKU_TEMPO") or "140"),
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(os.getenv("PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR") or
                                                   Path.home() / "phone-worker/cache/miku-fragments"),
    })
    defaults = {
        "LENGTH_MODE": "total", "FRAGMENT_CACHE_MB": "256", "PCM_CACHE_MB": "256", "STATUS_CACHE_SECONDS": "15",
        "MIN_ALIASES": "500", "ENGLISH_MIN_ALIASES": "500", "MAX_CHARACTERS": "180",
        "MAX_PHONEMES": "240", "MAX_AUDIO_SECONDS": "20", "RENDER_THREADS": "4",
    }
    environment.update({f"PHONE_WORKER_TETO_{suffix}": str(os.getenv(f"PHONE_WORKER_MIKU_{suffix}") or default)
                        for suffix, default in defaults.items()})
    # An English CVVC bank is required for both supported reading languages.
    # Empty/missing Miku paths must never discover a Kasane Teto default bank.
    if not english_bank:
        environment["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] = str(Path.home() / "voicebanks/hatsune-miku-english")
    return environment


def _get_miku_renderer():
    global _MIKU_RENDERER, _MIKU_RENDERER_CONFIG
    environment = _miku_renderer_environment()
    configuration = hashlib.sha256(json.dumps(environment, sort_keys=True).encode("utf-8")).hexdigest()
    with _TETO_RENDERER_LOCK:
        if _MIKU_RENDERER is not None and _MIKU_RENDERER_CONFIG == configuration:
            return _MIKU_RENDERER
        _MIKU_RENDERER = None
        _MIKU_RENDERER_CONFIG = ""
        try:
            from teto_renderer import TetoRenderer
            renderer = TetoRenderer(resource_guard=_teto_resource_snapshot,
                                    environment=environment, voice_id="hatsune-miku")
        except Exception as exc:
            reason = f"{type(exc).__name__}: {_short_text(exc, limit=180)}"
            raise RuntimeError(f"renderer Miku indisponível: {reason}") from exc
        _MIKU_RENDERER = renderer
        _MIKU_RENDERER_CONFIG = configuration
        return renderer


def _get_ttv_renderer(voice_id: str = "kasane-teto"):
    voice_id = _phone_worker_tts_policy_module().normalize_ttv_voice_id(voice_id)
    return _get_miku_renderer() if voice_id == "hatsune-miku" else _get_teto_renderer()


def _ttv_request_settings(body: dict[str, Any]) -> dict[str, Any]:
    policy = _phone_worker_tts_policy_module()
    voice_id = policy.normalize_ttv_voice_id(body.get("ttv_voice_id", "kasane-teto"))
    prefix = "MIKU" if voice_id == "hatsune-miku" else "TETO"
    return policy.effective_ttv_settings(
        body, default_speech_rate=os.getenv(f"PHONE_WORKER_{prefix}_SPEECH_RATE", "1.0"))


def _ttv_voice_state(status: dict[str, Any], voice_id: str) -> dict[str, Any]:
    """Select one voice; a missing Miku advertisement never inherits Teto."""
    voice_id = _phone_worker_tts_policy_module().normalize_ttv_voice_id(voice_id)
    voices = status.get("ttv_voices")
    if isinstance(voices, list):
        for row in voices:
            if isinstance(row, dict) and row.get("id") == voice_id:
                selected = dict(status) if voice_id == "kasane-teto" else {}
                # Aggregate protocol support does not grant it to a legacy
                # character renderer that has no performance method.
                selected.pop("ttv_performance_protocol", None)
                selected.update(row)
                if "language_fingerprints" in row:
                    selected["ttv_language_fingerprints"] = dict(row["language_fingerprints"] or {})
                selected["ttv_languages"] = list(row.get("languages") or [])
                selected["resources"] = status.get("resources", {"ok": True})
                return selected
        return {"ready": False, "available": False, "enabled": False,
                "last_error": "vocaloid TTV não anunciada pelo worker", "resources": status.get("resources", {})}
    if voice_id == "kasane-teto":
        return dict(status)  # Established single-voice worker/test contract.
    return {"ready": False, "available": False, "enabled": False,
            "last_error": "Hatsune Miku não instalada no worker", "resources": status.get("resources", {})}


def _ttv_single_voice_status(voice_id: str, *, force: bool = False) -> dict[str, Any]:
    is_miku = voice_id == "hatsune-miku"
    prefix = "MIKU" if is_miku else "TETO"
    enabled = _env_bool(f"PHONE_WORKER_{prefix}_ENABLED", False)
    result: dict[str, Any] = {
        "ok": False, "available": False, "ready": False, "enabled": enabled,
        "engine": "teto", "backend": "utau",
        "ttv_personal_settings": True, "ttv_languages": [], "ttv_language_fingerprints": {},
    }
    try:
        # A broken character configuration must not prevent the other voice
        # from appearing in aggregate health or accepting synthesis requests.
        if not is_miku:
            result["backend"] = _teto_backend()
        if not enabled:
            result["last_error"] = f"PHONE_WORKER_{prefix}_ENABLED=false"
            return result
        renderer = _get_ttv_renderer(voice_id)
        result.update(renderer.status(force=force))
        if callable(getattr(renderer, "synthesize_performance", None)):
            result["ttv_performance_protocol"] = max(1, min(2, int(getattr(renderer, "PERFORMANCE_PROTOCOL", 1))))
        else:
            result.pop("ttv_performance_protocol", None)
        if callable(getattr(renderer, "fingerprint", None)):
            result["ttv_language_fingerprints"] = {
                language: renderer.fingerprint(reading_language=language)
                for language in result.get("ttv_languages", [])
                if language == "en-US" or is_miku
            }
        if callable(getattr(renderer, "singing_fingerprint", None)):
            result["singing_fingerprints"] = {
                language: renderer.singing_fingerprint(reading_language=language)
                for language in ("pt-BR", "en-US")
            }
            result["singing_available"] = any(value != "unavailable" for value in result["singing_fingerprints"].values())
    except Exception as exc:
        result["last_error"] = f"{type(exc).__name__}: {_short_text(exc, limit=180)}"
    return result


def _teto_status(*, force: bool = False) -> dict[str, Any]:
    # Keep legacy Teto fields while advertising independent TTV voices.
    states = {voice_id: _ttv_single_voice_status(voice_id, force=force)
              for voice_id in ("kasane-teto", "hatsune-miku")}
    result = dict(states["kasane-teto"])
    enabled = any(state.get("enabled") for state in states.values())
    resources = {"ok": False, "reason": "engine desativada"}
    if enabled:
        try:
            resources = _teto_resource_snapshot()
        except Exception as exc:
            resources = {"ok": False, "reason": _short_text(exc, limit=180)}
    result["resources"] = resources
    result["ttv_performance_protocol"] = 2
    result["ttv_voices"] = []
    for voice_id, state in states.items():
        result["ttv_voices"].append({
            "id": voice_id, "name": "Hatsune Miku" if voice_id == "hatsune-miku" else "Kasane Teto",
            "ready": bool(state.get("ready") and resources.get("ok", True)),
            "available": bool(state.get("available")), "enabled": bool(state.get("enabled")),
            "supports_personal_rate": True, "languages": list(state.get("ttv_languages") or []),
            "fingerprint": str(state.get("fingerprint") or ""),
            "language_fingerprints": dict(state.get("ttv_language_fingerprints") or {}),
            "ttv_language_fingerprints": dict(state.get("ttv_language_fingerprints") or {}),
            "backend": state.get("backend", "utau"), "renderer_version": state.get("renderer_version", ""),
            "voicebank_profile": state.get("voicebank_profile", ""), "last_error": state.get("last_error", ""),
            **({"ttv_performance_protocol": state["ttv_performance_protocol"]} if state.get("ttv_performance_protocol") else {}),
            **({"singing_fingerprints": dict(state["singing_fingerprints"]),
                "singing_available": bool(state.get("singing_available"))}
               if "singing_fingerprints" in state else {}),
        })
    result.update({name: any(state.get(name) for state in states.values())
                   for name in ("ok", "available", "ready", "enabled")})
    return result


_EDGE_TTS_RUNTIME_VERSION = "7.3.1"
_EDGE_TTS_RUNTIME_INSTALL_LOCK = threading.Lock()
_EDGE_TTS_RUNTIME_INSTALL_RUNNING = False


@contextlib.contextmanager
def _edge_tts_runtime_install_lock():
    # The supervisor, Music Agent launcher and this process share site-packages.
    # flock is released by the kernel after a crash and needs no stale-PID cleanup.
    import fcntl
    directory = _dependency_install_state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "edge-tts.install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _invalidate_tts_dependency_caches() -> None:
    # pip runs in another process; a failed import from before installation
    # must be retried, and the next heartbeat must advertise the new engine.
    importlib.invalidate_caches()
    with _CONTROL_PLANE_SNAPSHOT_CACHE_LOCK:
        _CONTROL_PLANE_SNAPSHOT_CACHE.clear()


def _ensure_edge_tts_runtime_dependency() -> dict[str, Any]:
    """Repair Edge in this interpreter without involving the Music Agent.

    Edge is optional for music, but is required by the Phone Worker's R4
    recovery path. Keep its install separate from optional/heavy music deps and
    off the HTTP/heartbeat thread; do not borrow a test venv's site-packages.
    """
    global _EDGE_TTS_RUNTIME_INSTALL_RUNNING
    if _current_core_worker_profile() != "turbo" or not _env_bool("PHONE_WORKER_TTS_AGENT_ENABLED", True):
        return {"started": False, "reason": "tts_disabled"}
    if _module_import_ok("edge_tts")[0]:
        return {"started": False, "reason": "available"}
    if (not _env_bool("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS", True)
            or not _phone_worker_deps_mode_enabled() or _phone_worker_safe_mode_enabled()):
        return {"started": False, "reason": "disabled"}
    cooldown = max(60.0, float(_env_int("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS_COOLDOWN_SECONDS", 900)))
    with _EDGE_TTS_RUNTIME_INSTALL_LOCK:
        if _EDGE_TTS_RUNTIME_INSTALL_RUNNING:
            return {"started": False, "reason": "already_running"}
        allowed, reason, remaining = _dependency_attempt_allowed(
            f"edge-tts-{_EDGE_TTS_RUNTIME_VERSION}-runtime", cooldown)
        if not allowed:
            return {"started": False, "reason": reason, "remaining_seconds": round(remaining, 1)}
        _EDGE_TTS_RUNTIME_INSTALL_RUNNING = True

    def install() -> None:
        # A supervisor may have installed the same wheel after scheduling.
        importlib.invalidate_caches()
        if _module_import_ok("edge_tts")[0]:
            _invalidate_tts_dependency_caches()
            return
        package = f"edge-tts=={_EDGE_TTS_RUNTIME_VERSION}"
        command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                   "--no-input", "--upgrade", "--prefer-binary", "--only-binary=:all:", package]
        print(f"[phone-worker-edge] instalando {package} no Python do worker: {sys.executable}", flush=True)
        result = subprocess.run(command, timeout=max(45, _env_int(
            "PHONE_WORKER_AUTO_INSTALL_TTS_DEPS_TIMEOUT_SECONDS", 240)), check=False)
        if result.returncode:
            raise RuntimeError(f"pip encerrou com código {result.returncode}")
        _invalidate_tts_dependency_caches()
        ok, error = _module_import_ok("edge_tts")
        if not ok:
            raise RuntimeError(error)
        print("[phone-worker-edge] Edge disponível no runtime; capacidade será anunciada no próximo heartbeat", flush=True)

    def repair() -> None:
        global _EDGE_TTS_RUNTIME_INSTALL_RUNNING
        try:
            with _edge_tts_runtime_install_lock():
                install()
        except Exception as exc:
            print(f"[phone-worker-edge] reparo falhou: {type(exc).__name__}: {_short_text(exc, limit=160)}", flush=True)
        finally:
            with _EDGE_TTS_RUNTIME_INSTALL_LOCK:
                _EDGE_TTS_RUNTIME_INSTALL_RUNNING = False

    try:
        threading.Thread(target=repair, name="edge-tts-runtime-repair", daemon=True).start()
    except Exception as exc:
        with _EDGE_TTS_RUNTIME_INSTALL_LOCK:
            _EDGE_TTS_RUNTIME_INSTALL_RUNNING = False
        return {"started": False, "reason": "thread_failed", "error": _short_text(exc, limit=120)}
    return {"started": True, "package": f"edge-tts=={_EDGE_TTS_RUNTIME_VERSION}", "python": sys.executable}


def _turbo_dependency_snapshot() -> dict[str, Any]:
    profile = _current_core_worker_profile()
    deps: dict[str, Any] = {
        "profile": profile,
        "turbo": profile == "turbo",
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "curl": bool(shutil.which("curl")),
        "wget": bool(shutil.which("wget")),
        "piper_cli": bool(shutil.which("piper")),
        "piper_command": bool(str(os.getenv("PHONE_WORKER_PIPER_COMMAND", "") or "").strip()),
        "piper_model": False,
        "piper_config": False,
        "edge_tts": False,
        "gtts": False,
        "android_native_tts": False,
        "teto_tts": False,
    }
    model = str(os.getenv("PHONE_WORKER_PIPER_MODEL", "") or "").strip()
    config = str(os.getenv("PHONE_WORKER_PIPER_CONFIG", "") or "").strip()
    if model:
        with contextlib.suppress(Exception):
            deps["piper_model"] = Path(model).expanduser().exists() and Path(model).expanduser().stat().st_size > 0
    if config:
        with contextlib.suppress(Exception):
            deps["piper_config"] = Path(config).expanduser().exists() and Path(config).expanduser().stat().st_size > 0
    edge_ready, edge_error = _module_import_ok("edge_tts")
    deps["edge_tts"] = edge_ready
    if not edge_ready:
        deps["edge_tts_error"] = _short_text(edge_error, limit=80)
        deps["edge_tts_repair"] = _ensure_edge_tts_runtime_dependency()
    try:
        import gtts  # type: ignore  # noqa: F401
        deps["gtts"] = True
    except Exception as exc:
        deps["gtts_error"] = _short_text(exc, limit=80)
    android_status = _android_tts_status(use_cache=True)
    deps["android_native_tts"] = bool(android_status.get("ok") or android_status.get("ready") or android_status.get("available"))
    deps["android_native_enabled"] = bool(android_status.get("enabled", True))
    deps["android_native_url"] = str(android_status.get("url") or _android_tts_base_url())
    if android_status.get("last_error"):
        deps["android_native_error"] = _short_text(android_status.get("last_error"), limit=100)
    teto_status = _teto_status()
    deps["teto"] = teto_status
    teto_resources = teto_status.get("resources") if isinstance(teto_status.get("resources"), dict) else {}
    deps["teto_tts"] = bool(teto_status.get("ready") and teto_resources.get("ok", True))
    deps["teto_enabled"] = bool(teto_status.get("enabled"))
    deps["teto_fingerprint"] = str(teto_status.get("fingerprint") or "")[:64]
    if teto_status.get("last_error"):
        deps["teto_error"] = _short_text(teto_status.get("last_error"), limit=120)
    missing = [key for key in ("ffmpeg", "ffprobe", "edge_tts", "gtts") if not deps.get(key)]
    deps["ok"] = not missing
    deps["missing"] = missing
    return deps


def _teto_dependency_snapshot_fast() -> dict[str, Any]:
    """Resolve only Teto readiness for an explicit Teto request.

    This avoids probing Android TTS and importing unrelated network providers
    before the requested engine has even been attempted. Explicit Teto requests
    never substitute another character when the voicebank is unavailable.
    """
    profile = _current_core_worker_profile()
    teto_status = _teto_status()
    teto_resources = teto_status.get("resources") if isinstance(teto_status.get("resources"), dict) else {}
    return {
        "profile": profile,
        "turbo": profile == "turbo",
        "teto": teto_status,
        "teto_tts": bool(teto_status.get("ready") and teto_resources.get("ok", True)),
        "teto_enabled": bool(teto_status.get("enabled")),
        "teto_fingerprint": str(teto_status.get("fingerprint") or "")[:64],
        "ok": bool(teto_status.get("ready") and teto_resources.get("ok", True)),
        "missing": [],
    }


def _phone_worker_tts_policy_module() -> Any:
    global _PHONE_WORKER_TTS_POLICY_MODULE
    if _PHONE_WORKER_TTS_POLICY_MODULE is not None:
        return _PHONE_WORKER_TTS_POLICY_MODULE
    with _PHONE_WORKER_TTS_POLICY_LOCK:
        if _PHONE_WORKER_TTS_POLICY_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/tts_policy.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_tts_policy", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("tts_policy.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = ("normalize_engine", "available_engines", "preferred_engine", "engine_order",
                        "normalize_edge_rate", "normalize_edge_pitch", "normalize_gtts_language",
                        "sanitize_cache_key", "normalize_cache_format", "standard_cache_key",
                        "effective_ttv_settings", "normalize_ttv_voice_id", "normalize_ttv_speech_rate", "normalize_ttv_language",
                        "performance_request",
                        "cache_mode_allows_read", "cache_mode_allows_store")
            if not all(callable(getattr(module, name, None)) for name in required):
                raise RuntimeError("tts_policy.py incompleto")
            _PHONE_WORKER_TTS_POLICY_MODULE = module
        return _PHONE_WORKER_TTS_POLICY_MODULE


def _tts_agent_normalize_engine(raw: Any, *, default: str = "gtts") -> str:
    return _phone_worker_tts_policy_module().normalize_engine(raw, default=default)


def _tts_agent_available_engines(deps: dict[str, Any] | None = None) -> list[str]:
    deps = deps if isinstance(deps, dict) else _turbo_dependency_snapshot()
    return _phone_worker_tts_policy_module().available_engines(deps)

def _tts_agent_preferred_engine(available: list[str]) -> str:
    return _phone_worker_tts_policy_module().preferred_engine(available, requested=os.getenv("PHONE_WORKER_TTS_AGENT_ENGINE"))


def _tts_agent_queue_limit() -> int:
    return max(1, min(8, _env_int("PHONE_WORKER_TTS_AGENT_CONCURRENCY", 2)))


def _phone_worker_tts_cache_module() -> Any:
    global _PHONE_WORKER_TTS_CACHE_MODULE
    if _PHONE_WORKER_TTS_CACHE_MODULE is not None:
        return _PHONE_WORKER_TTS_CACHE_MODULE
    with _PHONE_WORKER_TTS_CACHE_LOCK:
        if _PHONE_WORKER_TTS_CACHE_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/tts_cache.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_tts_cache", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("tts_cache.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = ("prune_audio_cache", "find_file", "touch_file", "read_bytes", "publish_bytes")
            if not all(callable(getattr(module, name, None)) for name in required):
                raise RuntimeError("tts_cache.py incompleto")
            _PHONE_WORKER_TTS_CACHE_MODULE = module
        return _PHONE_WORKER_TTS_CACHE_MODULE


def _prune_audio_cache(root, max_bytes, max_files, *, protected=None):
    return _phone_worker_tts_cache_module().prune_audio_cache(
        root, max_bytes, max_files, protected=protected, os_api=os, wall_time=time.time)


_TTS_TRANSPORT_LOCK = threading.Lock()


def _tts_transport_module():
    # Health and synthesis can arrive on different HTTP threads at startup.
    with _TTS_TRANSPORT_LOCK:
        name = "tts_transport"
        module = sys.modules.get(name)
        if module is not None:
            return module
        path = Path(__file__).with_name("tts_transport.py")
        if not path.is_file():
            return None
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            return None
        return module


def _tts_agent_snapshot() -> dict[str, Any]:
    profile = _current_core_worker_profile()
    roles = _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))
    deps = _turbo_dependency_snapshot()
    available = _tts_agent_available_engines(deps)
    preferred = _tts_agent_preferred_engine(available)
    synth_allowed = profile == "turbo" and "tts-synth" in capabilities
    enabled = _env_bool("PHONE_WORKER_TTS_AGENT_ENABLED", True)
    with _TTS_AGENT_LOCK:
        active = int(_TTS_AGENT_ACTIVE)
        background_active = int(_TTS_AGENT_BACKGROUND_ACTIVE)
        background_total = int(_TTS_AGENT_BACKGROUND_TOTAL)
        background_aborted = int(_TTS_AGENT_BACKGROUND_ABORTED)
        total = int(_TTS_AGENT_TOTAL)
        failed = int(_TTS_AGENT_FAILED)
        total_ms = float(_TTS_AGENT_TOTAL_MS)
        last_error = str(_TTS_AGENT_LAST_ERROR or "")
        last_engine = str(_TTS_AGENT_LAST_ENGINE or "")
        last_ok_at = float(_TTS_AGENT_LAST_OK_AT or 0.0)
    avg_ms = round(total_ms / total, 2) if total else 0.0
    missing = []
    if not enabled:
        missing.append("PHONE_WORKER_TTS_AGENT_ENABLED=false")
    if profile != "turbo":
        missing.append("perfil turbo")
    if "tts-synth" not in capabilities:
        missing.append("capacidade tts-synth")
    if not available:
        missing.append("engine TTS disponível")
    return {
        "ok": bool(enabled and synth_allowed and available),
        "available": bool(enabled and synth_allowed and available),
        "synth_ready": bool(enabled and synth_allowed and available),
        "state": "ready" if (enabled and synth_allowed and available) else "not_ready",
        "reason": "ok" if (enabled and synth_allowed and available) else ", ".join(missing),
        "profile": profile,
        "roles": roles[:16],
        "capabilities": capabilities[:24],
        "available_engines": available,
        "preferred_engine": preferred,
        "selected_engine": preferred or last_engine,
        "deps": deps,
        "teto": deps.get("teto") if isinstance(deps.get("teto"), dict) else _teto_status(),
        "active": active,
        "background_active": background_active,
        "background_total": background_total,
        "background_aborted": background_aborted,
        "concurrency_limit": _tts_agent_queue_limit(),
        "stream_protocol": 2 if _tts_transport_module() is not None else 0,
        "stream_cache_only_protocol": 1 if getattr(_tts_transport_module(), "STREAM_CACHE_ONLY_PROTOCOL", 0) == 1 else 0,
        "ttv_performance_protocol": 2,
        "ttv_background_protocol": 1,
        "cache_binary_protocol": 2 if _tts_transport_module() is not None else 0,
        "total": total,
        "failed": failed,
        "avg_synth_ms": avg_ms,
        "last_error": _short_text(last_error, limit=180),
        "last_engine": last_engine,
        "last_ok_age_seconds": round(time.time() - last_ok_at, 1) if last_ok_at else None,
    }


def _tts_agent_record_start(*, background: bool = False) -> None:
    global _TTS_AGENT_ACTIVE, _TTS_AGENT_BACKGROUND_ACTIVE
    with _TTS_AGENT_LOCK:
        if background:
            if _TTS_AGENT_ACTIVE or _TTS_AGENT_BACKGROUND_ACTIVE:
                raise RuntimeError("TTS Agent ocupado; aquecimento TTV adiado")
            # Background work never occupies the foreground admission slot,
            # including installations configured with concurrency=1.
            _TTS_AGENT_BACKGROUND_ACTIVE += 1
            return
        if _TTS_AGENT_ACTIVE >= _tts_agent_queue_limit():
            raise RuntimeError("TTS Agent ocupado; fila local cheia")
        _TTS_AGENT_ACTIVE += 1


def _tts_agent_record_done(*, ok: bool, engine: str, elapsed_ms: float, error: str = "", background: bool = False) -> None:
    global _TTS_AGENT_ACTIVE, _TTS_AGENT_TOTAL, _TTS_AGENT_FAILED, _TTS_AGENT_TOTAL_MS, _TTS_AGENT_LAST_ERROR, _TTS_AGENT_LAST_ENGINE, _TTS_AGENT_LAST_OK_AT
    global _TTS_AGENT_BACKGROUND_ACTIVE, _TTS_AGENT_BACKGROUND_TOTAL, _TTS_AGENT_BACKGROUND_ABORTED
    with _TTS_AGENT_LOCK:
        if background:
            _TTS_AGENT_BACKGROUND_ACTIVE = max(0, _TTS_AGENT_BACKGROUND_ACTIVE - 1)
            _TTS_AGENT_BACKGROUND_TOTAL += 1
            _TTS_AGENT_BACKGROUND_ABORTED += int(not ok)
            return
        _TTS_AGENT_ACTIVE = max(0, _TTS_AGENT_ACTIVE - 1)
        _TTS_AGENT_TOTAL += 1
        _TTS_AGENT_TOTAL_MS += max(0.0, float(elapsed_ms or 0.0))
        _TTS_AGENT_LAST_ENGINE = str(engine or "")[:40]
        if ok:
            _TTS_AGENT_LAST_OK_AT = time.time()
            _TTS_AGENT_LAST_ERROR = ""
        else:
            _TTS_AGENT_FAILED += 1
            _TTS_AGENT_LAST_ERROR = _short_text(error, limit=220)

def _cache_dir_snapshot(path: Path, *, max_scan_files: int = 20000) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "files": 0,
        "bytes": 0,
        "scanned_files": 0,
    }
    try:
        if not path.exists():
            return result
        entries = []
        for item in path.iterdir():
            if not item.is_file():
                continue
            if item.suffix.lower() not in {".mp3", ".wav", ".ogg"}:
                continue
            entries.append(item)
            if len(entries) >= max_scan_files:
                break
        total = 0
        newest = 0.0
        oldest = 0.0
        for item in entries:
            with contextlib.suppress(Exception):
                st = item.stat()
                total += int(st.st_size or 0)
                newest = max(newest, float(st.st_mtime or 0.0))
                oldest = float(st.st_mtime or 0.0) if not oldest else min(oldest, float(st.st_mtime or 0.0))
        result.update({
            "files": len(entries),
            "bytes": total,
            "scanned_files": len(entries),
            "oldest_mtime": oldest or None,
            "newest_mtime": newest or None,
        })
    except Exception as exc:
        result["error"] = _short_text(exc, limit=100)
    return result


def _inactive_music_node_snapshot() -> dict[str, Any]:
    return _phone_worker_music_bridge_module("telemetria").inactive_music_node_snapshot()


def _worker_turbo_cache_snapshot() -> dict[str, Any]:
    tts_dir = Path(os.getenv("PHONE_WORKER_TTS_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "tts")).expanduser()
    piper_dir = Path(os.getenv("PHONE_WORKER_PIPER_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "piper")).expanduser()
    return {
        "tts": _cache_dir_snapshot(tts_dir),
        "piper": _cache_dir_snapshot(piper_dir),
        "tts_limits": {
            "max_mb": int(float(os.getenv("PHONE_WORKER_TTS_CACHE_MAX_MB", "4096") or 4096)),
            "max_files": int(float(os.getenv("PHONE_WORKER_TTS_CACHE_MAX_FILES", "20000") or 20000)),
        },
        "piper_limits": {
            "max_mb": int(float(os.getenv("PHONE_WORKER_PIPER_CACHE_MAX_MB", "2048") or 2048)),
            "max_files": int(float(os.getenv("PHONE_WORKER_PIPER_CACHE_MAX_FILES", "4096") or 4096)),
        },
    }


def _read_music_agent_version_from_path(path: Path | None = None) -> str:
    target = path or (_phone_worker_dir() / "cogs/musica/runtime_telefone/agente/servidor.py")
    return _phone_worker_music_bridge_module("telemetria").read_music_agent_version(target)


def _version_tuple_loose(value: Any) -> tuple[int, ...]:
    try:
        parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:4]]
    except Exception:
        parts = []
    return tuple(parts or [0])


def _version_lt_loose(current: Any, target: Any) -> bool:
    left = _version_tuple_loose(current)
    right = _version_tuple_loose(target)
    size = max(len(left), len(right))
    left = left + (0,) * (size - len(left))
    right = right + (0,) * (size - len(right))
    return left < right


def _music_agent_start_script() -> Path:
    return _phone_worker_music_bridge_module("servico").start_script(best_script=_best_script)


def _music_agent_pid_file() -> Path:
    return _phone_worker_music_bridge_module("servico").pid_file(phone_worker_dir=_phone_worker_dir)


def _music_agent_log_file() -> Path:
    return _phone_worker_music_bridge_module("servico").log_file(phone_worker_dir=_phone_worker_dir)


def _music_service_hooks() -> Any:
    return SimpleNamespace(
        phone_worker_dir=_phone_worker_dir,
        best_script=_best_script,
        read_pid_file=_read_pid_file,
        safe_telemetry=_safe_telemetry,
        snapshot=_music_agent_snapshot,
        pid_alive=_pid_alive,
        pgrep_count=_pgrep_count,
        run_text_command=_run_text_command,
        sanitize_log_text=_sanitize_log_text,
    )


_TTS_DEP_AUTOINSTALL_LOCK = threading.Lock()
_TTS_DEP_AUTOINSTALL_LAST_AT = 0.0
_TTS_DEP_AUTOINSTALL_RUNNING = False
_TTS_AGENT_LOCK = threading.Lock()
_TTS_AGENT_ACTIVE = 0
_TTS_AGENT_BACKGROUND_ACTIVE = 0
_TTS_AGENT_BACKGROUND_TOTAL = 0
_TTS_AGENT_BACKGROUND_ABORTED = 0
_TTS_AGENT_TOTAL = 0
_TTS_AGENT_FAILED = 0
_TTS_AGENT_TOTAL_MS = 0.0
_TTS_AGENT_LAST_ERROR = ""
_TTS_AGENT_LAST_ENGINE = ""
_TTS_AGENT_LAST_OK_AT = 0.0
_TTS_CACHE_MAINTENANCE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-cache-maint")
_TTS_CACHE_MAINTENANCE_LOCK = threading.Lock()
_TTS_CACHE_MAINTENANCE_PENDING = {}
_TTS_CACHE_MAINTENANCE_RUNNING = False
_TTS_CACHE_TOUCHES = {}


def _submit_tts_cache_maintenance(callback, *args, **kwargs) -> bool:
    global _TTS_CACHE_MAINTENANCE_RUNNING
    # One pending pass per cache category; new requests replace obsolete ones.
    key = getattr(callback, "__name__", str(type(callback)))
    with _TTS_CACHE_MAINTENANCE_LOCK:
        _TTS_CACHE_MAINTENANCE_PENDING[key] = (callback, args, kwargs)
        if _TTS_CACHE_MAINTENANCE_RUNNING:
            return True
        _TTS_CACHE_MAINTENANCE_RUNNING = True
    def run():
        global _TTS_CACHE_MAINTENANCE_RUNNING
        while True:
            with _TTS_CACHE_MAINTENANCE_LOCK:
                if not _TTS_CACHE_MAINTENANCE_PENDING:
                    _TTS_CACHE_MAINTENANCE_RUNNING = False
                    return
                _, (fn, positional, named) = _TTS_CACHE_MAINTENANCE_PENDING.popitem()
            with contextlib.suppress(Exception):
                fn(*positional, **named)
    try:
        _TTS_CACHE_MAINTENANCE_EXECUTOR.submit(run)
        return True
    except RuntimeError:
        with _TTS_CACHE_MAINTENANCE_LOCK:
            _TTS_CACHE_MAINTENANCE_RUNNING = False
            _TTS_CACHE_MAINTENANCE_PENDING.clear()
        return False


def _music_voice_dependency_specs() -> dict[str, dict[str, Any]]:
    return _phone_worker_music_bridge_module("telemetria").music_voice_dependency_specs()


def _module_import_ok(module_name: str) -> tuple[bool, str]:
    try:
        pins = {'edge_tts': ('edge-tts', _EDGE_TTS_RUNTIME_VERSION), 'gtts': ('gTTS', '2.5.4')}
        if module_name in pins:
            from importlib.metadata import version
            package, required = pins[module_name]
            if version(package) != required:
                return False, f'{package} precisa da versão {required}'
        # Do not cache an old Edge module in this process before an async pip
        # upgrade; import it only after the validated wheel is installed.
        importlib.import_module(module_name)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {_short_text(exc, limit=120)}"


def _phone_worker_deps_mode_enabled() -> bool:
    """Whether safe, missing-only dependency auto-install may run.

    Older emergency configs used *_MODE=off to stop heavy grpcio/google-cloud
    compile loops. Keep that value as safe-only instead of globally disabling
    idempotent installs. Use disabled/never/none to block all auto-install.
    """
    for key in ("PHONE_WORKER_TURBO_DEPS_INSTALL_MODE", "PHONE_WORKER_DEPS_INSTALL_MODE", "PHONE_WORKER_TURBO_DEPS_INSTALL", "PHONE_WORKER_TTS_DEPS_INSTALL"):
        raw = str(os.getenv(key) or "").strip().lower().strip('"\'')
        if raw in {"disabled", "disable", "never", "none", "bloqueado"}:
            return False
        if raw:
            return True
    return True


def _phone_worker_heavy_python_deps_enabled() -> bool:
    raw = str(os.getenv("PHONE_WORKER_HEAVY_PYTHON_DEPS_INSTALL") or "").strip().lower().strip('"\'')
    return raw in {"1", "true", "on", "yes", "sim"}


def _phone_worker_allow_pip_source_builds() -> bool:
    raw = str(os.getenv("PHONE_WORKER_ALLOW_PIP_SOURCE_BUILDS") or "").strip().lower().strip('"\'')
    return raw in {"1", "true", "on", "yes", "sim"}


def _dependency_install_state_dir() -> Path:
    return Path(os.getenv("PHONE_WORKER_DEPS_STATE_DIR") or (_phone_worker_dir() / ".dependency-install")).expanduser()


def _dependency_attempt_allowed(key: str, cooldown_seconds: float) -> tuple[bool, str, float]:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:120] or "dependency"
    state_dir = _dependency_install_state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    marker = state_dir / f"{safe_key}.last"
    now = time.time()
    last = 0.0
    try:
        last = float(marker.read_text(encoding="utf-8", errors="ignore").strip() or "0")
    except Exception:
        last = 0.0
    if last and now - last < cooldown_seconds:
        return False, "cooldown", max(0.0, cooldown_seconds - (now - last))
    try:
        marker.write_text(str(int(now)), encoding="utf-8")
    except Exception:
        pass
    return True, "allowed", 0.0


def _phone_worker_safe_mode_enabled() -> bool:
    for key in ("PHONE_WORKER_SAFE_MODE", "PHONE_WORKER_BASIC_ONLY", "PHONE_WORKER_LIGHT_MODE", "PHONE_WORKER_DISABLE_HEAVY_SERVICES"):
        if _env_bool(key, False):
            return True
    return False


def _env_autostart_enabled(name: str, default: str = "auto") -> bool:
    raw = str(os.getenv(name) if os.getenv(name) is not None else default).strip().lower().strip('"\'')
    if raw in {"1", "true", "on", "yes", "sim"}:
        return True
    if raw in {"0", "false", "off", "no", "nao", "não"}:
        return False
    if _phone_worker_safe_mode_enabled():
        return False
    return True


def _start_tts_dependency_autoinstall(missing: list[str], checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    global _TTS_DEP_AUTOINSTALL_LAST_AT, _TTS_DEP_AUTOINSTALL_RUNNING
    enabled = _env_bool("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS", True) and _phone_worker_deps_mode_enabled()
    if not enabled:
        return {"enabled": False, "started": False, "reason": "disabled"}
    if _phone_worker_safe_mode_enabled():
        return {"enabled": False, "started": False, "reason": "safe_mode"}
    specs = _music_voice_dependency_specs()
    heavy_enabled = _phone_worker_heavy_python_deps_enabled()
    install_items: list[dict[str, Any]] = []
    skipped_optional: list[str] = []
    for name in missing:
        spec = specs.get(name)
        if not spec or not spec.get("pip"):
            continue
        optional = bool(spec.get("optional"))
        if optional and not heavy_enabled:
            skipped_optional.append(name)
            continue
        install_items.append({"name": name, "package": str(spec.get("pip")), "optional": optional})
    system_packages: list[str] = []
    if ("ffmpeg" in missing or "ffprobe" in missing) and shutil.which("pkg"):
        system_packages.append("ffmpeg")
    install_items = list({str(item["name"]): item for item in install_items}.values())
    system_packages = [p for p in dict.fromkeys(system_packages) if p]
    if not install_items and not system_packages:
        reason = "optional_only" if skipped_optional else "nothing_installable"
        return {"enabled": True, "started": False, "reason": reason, "skipped_optional": skipped_optional}
    cooldown = max(60.0, float(_env_int("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS_COOLDOWN_SECONDS", 900)))
    now = time.time()
    with _TTS_DEP_AUTOINSTALL_LOCK:
        if _TTS_DEP_AUTOINSTALL_RUNNING:
            return {
                "enabled": True,
                "started": False,
                "reason": "already_running",
                "pip": [item["package"] for item in install_items],
                "system": system_packages,
                "skipped_optional": skipped_optional,
            }
        if _TTS_DEP_AUTOINSTALL_LAST_AT and now - _TTS_DEP_AUTOINSTALL_LAST_AT < cooldown:
            return {
                "enabled": True,
                "started": False,
                "reason": "cooldown",
                "remaining_seconds": round(cooldown - (now - _TTS_DEP_AUTOINSTALL_LAST_AT), 1),
                "pip": [item["package"] for item in install_items],
                "system": system_packages,
                "skipped_optional": skipped_optional,
            }
        allowed, reason, remaining = _dependency_attempt_allowed("tts-autoinstall", cooldown)
        if not allowed:
            return {
                "enabled": True,
                "started": False,
                "reason": reason,
                "remaining_seconds": round(remaining, 1),
                "pip": [item["package"] for item in install_items],
                "system": system_packages,
                "skipped_optional": skipped_optional,
            }
        _TTS_DEP_AUTOINSTALL_RUNNING = True
        _TTS_DEP_AUTOINSTALL_LAST_AT = now

    def _runner() -> None:
        global _TTS_DEP_AUTOINSTALL_RUNNING
        try:
            timeout = max(45, _env_int("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS_TIMEOUT_SECONDS", 240))
            allow_source = _phone_worker_allow_pip_source_builds()
            for item in install_items:
                name = str(item.get("name") or "")
                package = str(item.get("package") or "")
                if not package:
                    continue
                # Recheck inside the worker thread so a race does not reinstall an
                # already-present package.
                module = str(specs.get(name, {}).get("module") or "")
                install_lock = _edge_tts_runtime_install_lock() if module == "edge_tts" else contextlib.nullcontext()
                with install_lock:
                    importlib.invalidate_caches()
                    if module:
                        ok, _ = _module_import_ok(module)
                        if ok:
                            continue
                    pip_cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--upgrade", "--prefer-binary"]
                    if bool(item.get("optional")) or not allow_source:
                        pip_cmd.append("--only-binary=:all:")
                    pip_cmd.append(package)
                    result = subprocess.run(pip_cmd, timeout=timeout, check=False)
                    if result.returncode == 0:
                        _invalidate_tts_dependency_caches()
            if system_packages and shutil.which("pkg"):
                env = dict(os.environ)
                env.setdefault("DEBIAN_FRONTEND", "noninteractive")
                subprocess.run(["pkg", "install", "-y", *system_packages], timeout=timeout, check=False, env=env)
        except Exception:
            pass
        finally:
            with _TTS_DEP_AUTOINSTALL_LOCK:
                _TTS_DEP_AUTOINSTALL_RUNNING = False

    threading.Thread(target=_runner, name="tts-dependency-autoinstall", daemon=True).start()
    return {
        "enabled": True,
        "started": True,
        "pip": [item["package"] for item in install_items],
        "system": system_packages,
        "skipped_optional": skipped_optional,
        "safe_only_binary": not _phone_worker_allow_pip_source_builds(),
    }


def _music_voice_dependencies_snapshot() -> dict[str, Any]:
    hooks = SimpleNamespace(
        module_import_ok=_module_import_ok,
        which=shutil.which,
        start_dependency_autoinstall=_start_tts_dependency_autoinstall,
        env_bool=_env_bool,
    )
    return _phone_worker_music_bridge_module("telemetria").music_voice_dependencies_snapshot(hooks)


def _music_agent_snapshot() -> dict[str, Any]:
    hooks = SimpleNamespace(
        module_import_ok=_module_import_ok,
        which=shutil.which,
        start_dependency_autoinstall=_start_tts_dependency_autoinstall,
        env_bool=_env_bool,
        load_runtime_env=_load_phone_worker_runtime_env,
        ensure_token=_ensure_music_agent_token_env,
        safe_mode_enabled=_phone_worker_safe_mode_enabled,
        phone_worker_dir=_phone_worker_dir,
        active_release_dir=_active_release_dir,
        phone_worker_version=PHONE_WORKER_VERSION,
        env_float=_env_float,
        version_lt=_version_lt_loose,
        env_autostart_enabled=_env_autostart_enabled,
    )
    return _phone_worker_music_bridge_module("telemetria").music_agent_snapshot(hooks)


def _music_agent_light_snapshot() -> dict[str, Any]:
    def probe() -> dict[str, Any]:
        hooks = SimpleNamespace(load_runtime_env=_load_phone_worker_runtime_env, env_float=_env_float)
        return _phone_worker_music_bridge_module("telemetria").music_agent_health_snapshot(hooks)

    snapshot = _cached_control_plane_snapshot(
        "music_agent_light", probe,
        {"ok": False, "available": False, "configured": False, "reason": "health_pending"}, ttl=2.0,
    )
    observed = snapshot.get("observed_at")
    if isinstance(observed, (int, float)):
        age = time.time() - observed
        snapshot["observation_age_seconds"] = round(max(0.0, age), 3)
        if age < -5.0 or age > 5.0:
            snapshot.update(ok=False, available=False, reason="health_stale")
    return snapshot


_VOICE_AGENT_SESSION_LOCK = threading.RLock()
_VOICE_AGENT_SESSION_MEMORY: dict[str, Any] | None = None
_VOICE_AGENT_HANDOFF_MEMORY: dict[str, dict[str, Any]] = {}
_VOICE_AGENT_CONNECTION_MEMORY: dict[str, dict[str, Any]] = {}
_VOICE_AGENT_TRANSFER_MEMORY: dict[str, dict[str, Any]] = {}


class _VoiceAgentProbeCancelled(RuntimeError):
    """A scheduled probe lost its generation or temporary voice ownership."""


def _voice_agent_cancel_probe(guild_id: str, *, reason: str) -> None:
    with _VOICE_AGENT_SESSION_LOCK:
        current = _VOICE_AGENT_CONNECTION_MEMORY.get(guild_id)
        if isinstance(current, dict) and current.pop("_probe_id", None):
            current.update(state="connection_cancelled", stage="cancelled", connected_once=False,
                           closed_after_probe=False, error=reason, updated_at_ms=_voice_agent_now_ms())


def _voice_agent_assert_probe_current(guild_id: int, probe_id: str | None) -> None:
    if probe_id is None:
        return
    with _VOICE_AGENT_SESSION_LOCK:
        _voice_agent_prune_transfers()
        _voice_agent_prune_handoffs()
        key = str(guild_id)
        current = _VOICE_AGENT_CONNECTION_MEMORY.get(key) or {}
        handoff = _VOICE_AGENT_HANDOFF_MEMORY.get(key) or {}
        owner = str(handoff.get("voice_owner") or handoff.get("transport_owner") or "vps").strip().lower()
        if current.get("_probe_id") != probe_id or owner != "worker":
            raise _VoiceAgentProbeCancelled("probe cancelado, substituído ou sem posse da voz")


def _phone_worker_voice_state_module() -> Any:
    global _PHONE_WORKER_VOICE_STATE_MODULE
    if _PHONE_WORKER_VOICE_STATE_MODULE is not None:
        return _PHONE_WORKER_VOICE_STATE_MODULE
    with _PHONE_WORKER_VOICE_STATE_LOCK:
        if _PHONE_WORKER_VOICE_STATE_MODULE is None:
            path = Path(__file__).resolve().parent / "phone_worker_runtime/voice_state.py"
            spec = importlib.util.spec_from_file_location("core_phone_worker_runtime_voice_state", path)
            if not path.is_file() or spec is None or spec.loader is None:
                raise RuntimeError("voice_state.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = ("public_session", "public_handoff", "public_connection", "public_transfer", "expired_keys",
                        "session_summary", "handoff_summary", "connection_summary", "transfer_summary")
            if not all(callable(getattr(module, name, None)) for name in required):
                raise RuntimeError("voice_state.py incompleto")
            _PHONE_WORKER_VOICE_STATE_MODULE = module
        return _PHONE_WORKER_VOICE_STATE_MODULE


def _voice_agent_state_file() -> Path:
    base = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    raw = str(os.getenv("PHONE_WORKER_VOICE_AGENT_STATE_FILE") or "").strip()
    return Path(raw).expanduser() if raw else base / "voice-agent-state.json"


def _voice_agent_now_ms() -> int:
    return int(time.time() * 1000)


def _voice_agent_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _voice_agent_clean_text(value: Any, *, limit: int = 160) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:/@# -]+", "", str(value or "")).strip()[:limit]


def _voice_agent_flag(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "sim"}


def _voice_agent_load_state() -> dict[str, Any]:
    global _VOICE_AGENT_SESSION_MEMORY
    with _VOICE_AGENT_SESSION_LOCK:
        if isinstance(_VOICE_AGENT_SESSION_MEMORY, dict):
            return _VOICE_AGENT_SESSION_MEMORY
        path = _voice_agent_state_file()
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw or "{}")
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
        _VOICE_AGENT_SESSION_MEMORY = {
            "version": 1,
            "updated_at_ms": _voice_agent_now_ms(),
            "sessions": {str(k): v for k, v in sessions.items() if isinstance(v, dict)},
        }
        return _VOICE_AGENT_SESSION_MEMORY


def _voice_agent_save_state(state: dict[str, Any]) -> None:
    global _VOICE_AGENT_SESSION_MEMORY
    with _VOICE_AGENT_SESSION_LOCK:
        state["updated_at_ms"] = _voice_agent_now_ms()
        _VOICE_AGENT_SESSION_MEMORY = state
        path = _voice_agent_state_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            with contextlib.suppress(Exception):
                os.chmod(path, 0o600)
        except Exception:
            # O estado em memória continua suficiente; persistência é só conveniência.
            pass


def _voice_agent_public_session(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    return _phone_worker_voice_state_module().public_session(
        raw, now_ms=int(_voice_agent_now_ms() if now_ms is None else now_ms), int_value=_voice_agent_int)


def _voice_agent_public_handoff(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    return _phone_worker_voice_state_module().public_handoff(
        raw, now_ms=int(_voice_agent_now_ms() if now_ms is None else now_ms), int_value=_voice_agent_int, clean_text=_voice_agent_clean_text)


def _voice_agent_expired_keys(records: dict[str, Any], *, now_ms: int) -> list[str]:
    return _phone_worker_voice_state_module().expired_keys(records, now_ms=now_ms, int_value=_voice_agent_int)


def _voice_agent_revoke_handoff_owner(guild_id: str, *, now_ms: int, policy: str) -> None:
    # Caller holds the session lock; handoff credentials stay only in this map.
    handoff = _VOICE_AGENT_HANDOFF_MEMORY.get(guild_id)
    if isinstance(handoff, dict):
        handoff.update(voice_owner="vps", transport_owner="vps", allow_connection_probe=False,
                       connection_policy=policy, updated_at_ms=now_ms)
        _voice_agent_cancel_probe(guild_id, reason=policy)


def _voice_agent_prune_handoffs() -> dict[str, dict[str, Any]]:
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        stale = _voice_agent_expired_keys(_VOICE_AGENT_HANDOFF_MEMORY, now_ms=now_ms)
        for key in stale:
            _VOICE_AGENT_HANDOFF_MEMORY.pop(key, None)
            _voice_agent_cancel_probe(str(key), reason="voice_handoff_expired")
        return {str(k): dict(v) for k, v in _VOICE_AGENT_HANDOFF_MEMORY.items() if isinstance(v, dict)}


def _voice_agent_handoff_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    now_ms = _voice_agent_now_ms()
    handoffs_dict = _voice_agent_prune_handoffs()
    return _phone_worker_voice_state_module().handoff_summary(
        handoffs_dict, now_ms=now_ms, guild_id=guild_id, limit=limit, public_record=_voice_agent_public_handoff)


def _voice_agent_public_connection(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    return _phone_worker_voice_state_module().public_connection(
        raw, now_ms=int(_voice_agent_now_ms() if now_ms is None else now_ms), int_value=_voice_agent_int)


def _voice_agent_public_transfer(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    return _phone_worker_voice_state_module().public_transfer(
        raw, now_ms=int(_voice_agent_now_ms() if now_ms is None else now_ms), int_value=_voice_agent_int)


def _voice_agent_prune_transfers() -> dict[str, dict[str, Any]]:
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        stale = _voice_agent_expired_keys(_VOICE_AGENT_TRANSFER_MEMORY, now_ms=now_ms)
        for key in stale:
            _VOICE_AGENT_TRANSFER_MEMORY.pop(key, None)
            handoff = _VOICE_AGENT_HANDOFF_MEMORY.get(key)
            if isinstance(handoff, dict) and handoff.get("connection_policy") == "worker_ownership_granted_explicit_transfer":
                _voice_agent_revoke_handoff_owner(key, now_ms=now_ms, policy="vps_owner_transfer_expired")
        return {str(k): dict(v) for k, v in _VOICE_AGENT_TRANSFER_MEMORY.items() if isinstance(v, dict)}


def _voice_agent_transfer_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    now_ms = _voice_agent_now_ms()
    transfers_dict = _voice_agent_prune_transfers()
    return _phone_worker_voice_state_module().transfer_summary(
        transfers_dict, now_ms=now_ms, guild_id=guild_id, limit=limit, public_record=_voice_agent_public_transfer)


def _voice_agent_set_transfer(guild_id: int, **updates: Any) -> dict[str, Any]:
    key = str(int(guild_id or 0))
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        current = dict(_VOICE_AGENT_TRANSFER_MEMORY.get(key) or {})
        current.setdefault("guild_id", key)
        current.setdefault("created_at_ms", now_ms)
        current.update(updates)
        current["updated_at_ms"] = now_ms
        _VOICE_AGENT_TRANSFER_MEMORY[key] = current
        return dict(current)


def _voice_agent_prepare_transfer(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED", True):
        raise RuntimeError("controle de transferência de voz desativado")
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    channel_id = _voice_agent_int(body.get("channel_id"), 0)
    if guild_id <= 0 or channel_id <= 0:
        raise RuntimeError("guild_id/channel_id obrigatórios para transferência")
    ttl_seconds = max(10, min(180, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", 45))))
    now_ms = _voice_agent_now_ms()
    lease_id = f"vta:{guild_id}:{channel_id}:{now_ms}"
    current_owner = str(body.get("current_owner") or body.get("voice_owner") or "vps").strip().lower() or "vps"
    requested_owner = str(body.get("requested_owner") or "worker").strip().lower() or "worker"
    transfer = _voice_agent_set_transfer(
        guild_id,
        channel_id=str(channel_id),
        text_channel_id=str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
        requester_id=str(_voice_agent_int(body.get("requester_id"), 0) or ""),
        bot_user_id=str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
        source=_voice_agent_clean_text(body.get("source") or "tts", limit=40) or "tts",
        state="transfer_staged_waiting_vps_release",
        current_owner=current_owner,
        voice_owner=current_owner,
        requested_owner=requested_owner,
        lease_id=lease_id,
        allow_connection_probe=False,
        probe_authorized=False,
        reason=_voice_agent_clean_text(body.get("reason") or "preparado pela VPS; aguardando liberação explícita", limit=160),
        error="",
        expires_at_ms=now_ms + ttl_seconds * 1000,
    )
    return {"ok": True, "prepared": True, "state": transfer.get("state"), "transfer": _voice_agent_public_transfer(transfer), **_voice_agent_transfer_summary(guild_id=guild_id, limit=5)}


def _voice_agent_begin_transfer(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED", True):
        raise RuntimeError("controle de transferência de voz desativado")
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    if guild_id <= 0:
        raise RuntimeError("guild_id obrigatório para iniciar transferência")
    if not any(_voice_agent_flag(body.get(key)) for key in ("confirm_transfer", "confirm", "manual", "diagnostic")):
        raise RuntimeError("transferência exige confirmação explícita da VPS")
    with _VOICE_AGENT_SESSION_LOCK:
        transfers = _voice_agent_prune_transfers()
        current = dict(transfers.get(str(guild_id)) or {})
        if not current:
            current = _voice_agent_prepare_transfer(body).get("transfer") or {}
        handoffs = _voice_agent_prune_handoffs()
        handoff = dict(handoffs.get(str(guild_id)) or {})
        if not handoff:
            raise RuntimeError("handoff temporário ausente; não é seguro entregar posse")
        ttl_seconds = max(10, min(120, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", 45))))
        now_ms = _voice_agent_now_ms()
        lease_id = str(current.get("lease_id") or f"vta:{guild_id}:{now_ms}")
        current.pop("guild_id", None)
        current.update(
            state="worker_ownership_granted_waiting_probe",
            current_owner="worker",
            voice_owner="worker",
            requested_owner="worker",
            lease_id=lease_id,
            allow_connection_probe=True,
            probe_authorized=True,
            reason="VPS confirmou transferência controlada de posse da voz",
            error="",
            expires_at_ms=now_ms + ttl_seconds * 1000,
        )
        transfer = _voice_agent_set_transfer(guild_id, **current)
        with _VOICE_AGENT_SESSION_LOCK:
            handoff["voice_owner"] = "worker"
            handoff["transport_owner"] = "worker"
            handoff["allow_connection_probe"] = True
            handoff["connection_policy"] = "worker_ownership_granted_explicit_transfer"
            handoff["updated_at_ms"] = now_ms
            _VOICE_AGENT_HANDOFF_MEMORY[str(guild_id)] = handoff
        return {"ok": True, "started": True, "state": transfer.get("state"), "transfer": _voice_agent_public_transfer(transfer), **_voice_agent_transfer_summary(guild_id=guild_id, limit=5), **_voice_agent_handoff_summary(guild_id=guild_id, limit=5)}


def _voice_agent_release_transfer(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    if guild_id <= 0:
        raise RuntimeError("guild_id obrigatório para liberar transferência")
    reason = _voice_agent_clean_text(body.get("reason") or "released_by_vps", limit=120)
    with _VOICE_AGENT_SESSION_LOCK:
        now_ms = _voice_agent_now_ms()
        transfer = _voice_agent_set_transfer(
            guild_id,
            state="released_to_vps",
            current_owner="vps",
            voice_owner="vps",
            requested_owner="",
            allow_connection_probe=False,
            probe_authorized=False,
            reason=reason,
            expires_at_ms=now_ms + 10_000,
        )
        with _VOICE_AGENT_SESSION_LOCK:
            _voice_agent_revoke_handoff_owner(str(guild_id), now_ms=now_ms, policy="vps_owner_transfer_released")
        return {"ok": True, "released": True, "state": transfer.get("state"), "transfer": _voice_agent_public_transfer(transfer), **_voice_agent_transfer_summary(guild_id=guild_id, limit=5)}


def _voice_agent_connection_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        items = {str(k): dict(v) for k, v in _VOICE_AGENT_CONNECTION_MEMORY.items() if isinstance(v, dict)}
    return _phone_worker_voice_state_module().connection_summary(
        items, now_ms=now_ms, guild_id=guild_id, limit=limit, public_record=_voice_agent_public_connection)


def _voice_agent_normalize_endpoint(endpoint: str) -> tuple[str, str]:
    raw = str(endpoint or "").strip()
    raw = raw.replace("wss://", "").replace("ws://", "").replace("https://", "").replace("http://", "")
    raw = raw.split("/", 1)[0].strip()
    host = _voice_agent_clean_text(raw, limit=180)
    if not host:
        raise RuntimeError("endpoint de voz vazio")
    return host, f"wss://{host}/?v=4"


def _voice_agent_set_connection(guild_id: int, *, _expected_probe_id: str | None = None, **updates: Any) -> dict[str, Any]:
    key = str(int(guild_id or 0))
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        _voice_agent_assert_probe_current(guild_id, _expected_probe_id)
        current = dict(_VOICE_AGENT_CONNECTION_MEMORY.get(key) or {})
        if _expected_probe_id is None:
            current.pop("_probe_id", None)
        current.setdefault("guild_id", key)
        current.setdefault("started_at_ms", now_ms)
        current.update(updates)
        current["updated_at_ms"] = now_ms
        _VOICE_AGENT_CONNECTION_MEMORY[key] = current
        return dict(current)


def _voice_agent_udp_discovery_probe(*, ip: str, port: int, ssrc: int, timeout: float = 0.9) -> dict[str, Any]:
    result = {"attempted": False, "ok": False, "ip": "", "port": 0, "error": ""}
    if not ip or int(port or 0) <= 0 or int(ssrc or 0) <= 0:
        result["error"] = "ready sem ip/port/ssrc para UDP discovery"
        return result
    result["attempted"] = True
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(max(0.2, min(2.0, float(timeout or 0.9))))
        packet = bytearray(70)
        packet[0:4] = int(ssrc).to_bytes(4, "big", signed=False)
        sock.sendto(packet, (str(ip), int(port)))
        data, _addr = sock.recvfrom(70)
        if len(data) >= 70:
            discovered_ip = data[4:68].split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            discovered_port = int.from_bytes(data[68:70], "little", signed=False)
            result.update({"ok": bool(discovered_ip and discovered_port), "ip": discovered_ip, "port": discovered_port})
        else:
            result["error"] = f"resposta UDP curta: {len(data)} bytes"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {_short_text(exc, limit=120)}"
    finally:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
    return result


async def _voice_agent_probe_connection_async(handoff: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        import aiohttp  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"aiohttp indisponível para voice dry-run: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc

    guild_id = _voice_agent_int(handoff.get("guild_id"), 0)
    probe_id = handoff.get("_probe_id")
    bot_user_id = _voice_agent_clean_text(handoff.get("bot_user_id"), limit=80)
    session_id = str(handoff.get("session_id") or "").strip()
    token = str(handoff.get("voice_token") or "").strip()
    endpoint_host, ws_url = _voice_agent_normalize_endpoint(str(handoff.get("endpoint") or handoff.get("endpoint_host") or ""))
    if guild_id <= 0 or not bot_user_id or not session_id or not token:
        raise RuntimeError("handoff incompleto para conexão: guild_id/bot_user_id/session_id/token")

    started = time.perf_counter()
    _voice_agent_set_connection(
        guild_id, _expected_probe_id=probe_id,
        channel_id=str(handoff.get("channel_id") or ""),
        state="voice_ws_connecting",
        stage="ws_connect",
        dry_run=True,
        ws_url_present=True,
        endpoint_host=endpoint_host,
        error="",
    )
    hello_received = False
    ready_received = False
    ready_payload: dict[str, Any] = {}
    udp_result: dict[str, Any] = {"attempted": False, "ok": False}
    client_timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_seconds or 4.0)))
    async with aiohttp.ClientSession(timeout=client_timeout, headers={"User-Agent": f"CorePhoneWorkerVoiceAgent/{PHONE_WORKER_VERSION}"}) as session:
        async with session.ws_connect(ws_url, timeout=max(1.0, min(8.0, float(timeout_seconds or 4.0))), heartbeat=None) as ws:
            _voice_agent_set_connection(guild_id, _expected_probe_id=probe_id, state="voice_ws_identifying", stage="identify", endpoint_host=endpoint_host)
            await ws.send_json({
                "op": 0,
                "d": {
                    "server_id": str(guild_id),
                    "user_id": str(bot_user_id),
                    "session_id": session_id,
                    "token": token,
                },
            })
            deadline = time.monotonic() + max(1.0, float(timeout_seconds or 4.0))
            while time.monotonic() < deadline:
                remaining = max(0.1, min(1.0, deadline - time.monotonic()))
                msg = await ws.receive(timeout=remaining)
                _voice_agent_assert_probe_current(guild_id, probe_id)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data or "{}")
                    except Exception:
                        continue
                    op = payload.get("op")
                    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
                    if op == 8:
                        hello_received = True
                        interval = int(float(data.get("heartbeat_interval") or 0)) if data else 0
                        _voice_agent_set_connection(guild_id, _expected_probe_id=probe_id, state="voice_ws_hello", stage="hello", hello_received=True, heartbeat_interval_ms=interval)
                        # O dry-run não mantém sessão viva, mas manda um heartbeat curto para validar a ida/volta básica.
                        with contextlib.suppress(Exception):
                            await ws.send_json({"op": 3, "d": int(time.time() * 1000)})
                    elif op == 2:
                        ready_received = True
                        ready_payload = dict(data)
                        ssrc = _voice_agent_int(data.get("ssrc"), 0)
                        voice_ip = str(data.get("ip") or "").strip()
                        voice_port = _voice_agent_int(data.get("port"), 0)
                        modes = data.get("modes") if isinstance(data.get("modes"), list) else []
                        selected_protocol_ready = bool(voice_ip and voice_port and ssrc and modes)
                        _voice_agent_set_connection(
                            guild_id, _expected_probe_id=probe_id,
                            state="voice_ws_ready",
                            stage="ready",
                            ready_received=True,
                            ssrc_present=bool(ssrc),
                            selected_protocol_ready=selected_protocol_ready,
                            voice_ip=voice_ip,
                            voice_port=voice_port,
                            modes=[str(item)[:80] for item in modes[:8]],
                        )
                        udp_result = _voice_agent_udp_discovery_probe(ip=voice_ip, port=voice_port, ssrc=ssrc, timeout=max(0.2, min(1.2, float(timeout_seconds or 4.0) / 3.0)))
                        break
                    elif op == 6:
                        _voice_agent_set_connection(guild_id, _expected_probe_id=probe_id, heartbeat_ack=True)
                    elif op == 9:
                        raise RuntimeError("Voice WS invalid session no dry-run")
                elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                    break
            with contextlib.suppress(Exception):
                await ws.close(code=1000, message=b"Core Worker voice dry-run complete")

    if not ready_received:
        raise RuntimeError("Voice WS não retornou READY no dry-run")
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    final = _voice_agent_set_connection(
        guild_id, _expected_probe_id=probe_id,
        state="connected_dry_run",
        stage="closed_after_probe",
        hello_received=hello_received,
        ready_received=ready_received,
        connected_once=True,
        closed_after_probe=True,
        udp_probe_attempted=bool(udp_result.get("attempted")),
        udp_probe_ok=bool(udp_result.get("ok")),
        udp_probe_error=str(udp_result.get("error") or "")[:160],
        external_ip=str(udp_result.get("ip") or "")[:80],
        external_port=_voice_agent_int(udp_result.get("port"), 0),
        latency_ms=elapsed_ms,
        error="",
    )
    return _voice_agent_public_connection(final)


def _voice_agent_probe_worker(handoff: dict[str, Any], *, timeout_seconds: float) -> None:
    guild_id = _voice_agent_int(handoff.get("guild_id"), 0)
    probe_id = handoff.get("_probe_id")
    try:
        _voice_agent_assert_probe_current(guild_id, probe_id)
        result = asyncio.run(_voice_agent_probe_connection_async(handoff, timeout_seconds=timeout_seconds))
        _voice_agent_set_connection(guild_id, _expected_probe_id=probe_id, _probe_id="",
                                   **{k: v for k, v in result.items() if k not in {"guild_id", "_probe_id"}})
    except _VoiceAgentProbeCancelled:
        return
    except Exception as exc:
        with contextlib.suppress(_VoiceAgentProbeCancelled):
            _voice_agent_set_connection(
                guild_id, _expected_probe_id=probe_id, _probe_id="",
                state="connection_failed", stage="failed", dry_run=True, connected_once=False,
                error=f"{type(exc).__name__}: {_short_text(exc, limit=180)}")


def _voice_agent_start_connection_probe(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
        raise RuntimeError("Worker Voice Agent desativado")
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED", True):
        raise RuntimeError("voice connection dry-run desativado")
    with _VOICE_AGENT_SESSION_LOCK:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        _voice_agent_prune_transfers()
        handoffs = _voice_agent_prune_handoffs()
        handoff = handoffs.get(str(guild_id)) if guild_id > 0 else None
        if not handoff:
            # Se o chamador acabou de mandar um handoff embutido, aceita sem exigir registro prévio.
            if isinstance(body.get("discord_voice_handoff"), dict):
                handoff = _voice_agent_register_handoff_payload(body)
                with _VOICE_AGENT_SESSION_LOCK:
                    _VOICE_AGENT_HANDOFF_MEMORY[str(handoff["guild_id"])] = handoff
            else:
                raise RuntimeError("handoff temporário de voz não encontrado para a guild")
        guild_id = _voice_agent_int(handoff.get("guild_id"), 0)
        allow_probe = any(_voice_agent_flag(value) for value in (
            body.get("allow_probe"), body.get("allow_connection_probe"), handoff.get("allow_connection_probe"), handoff.get("allow_probe")))
        owner = str(handoff.get("voice_owner") or handoff.get("transport_owner") or "vps").strip().lower() or "vps"
        if owner != "worker":
            blocked = _voice_agent_set_connection(
                guild_id,
                channel_id=str(handoff.get("channel_id") or ""),
                state="waiting_for_voice_ownership",
                stage="handoff_received",
                dry_run=True,
                connected_once=False,
                endpoint_host=_voice_agent_clean_text(handoff.get("endpoint_host") or handoff.get("endpoint"), limit=160),
                error="dono atual da voz é a VPS; probe direto não iniciado",
            )
            return {
                "ok": True,
                "started": False,
                "blocked": True,
                "state": "waiting_for_explicit_voice_ownership_transfer",
                "reason": "vps_voice_owner_requires_explicit_begin_transfer",
                "connection": _voice_agent_public_connection(blocked),
                **_voice_agent_connection_summary(guild_id=guild_id, limit=5),
            }
        if not allow_probe:
            blocked = _voice_agent_set_connection(
                guild_id,
                channel_id=str(handoff.get("channel_id") or ""),
                state="connection_probe_blocked",
                stage="probe_not_authorized",
                dry_run=True,
                connected_once=False,
                endpoint_host=_voice_agent_clean_text(handoff.get("endpoint_host") or handoff.get("endpoint"), limit=160),
                error="probe de conexão exige allow_probe/force explícito",
            )
            return {"ok": True, "started": False, "blocked": True, "state": "connection_probe_blocked", "reason": "probe_not_authorized", "connection": _voice_agent_public_connection(blocked), **_voice_agent_connection_summary(guild_id=guild_id, limit=5)}
        timeout_seconds = max(1.0, min(10.0, float(body.get("timeout_seconds") or _env_float("PHONE_WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS", 4.0))))
        existing = _voice_agent_connection_summary(guild_id=guild_id, limit=1).get("last_connection") or {}
        if existing.get("state") in {"probing", "connecting", "voice_ws_connecting", "voice_ws_identifying", "voice_ws_hello", "voice_ws_ready"} and float(existing.get("updated_age_seconds", 999.0)) < 8.0:
            return {"ok": True, "started": False, "state": "connection_probe_already_running", **_voice_agent_connection_summary(guild_id=guild_id, limit=5)}
        probe_id = secrets.token_hex(16)
        handoff = {**handoff, "_probe_id": probe_id}
        _voice_agent_set_connection(
            guild_id, _probe_id=probe_id,
            channel_id=str(handoff.get("channel_id") or ""),
            state="probing",
            stage="scheduled",
            dry_run=True,
            connected_once=False,
            endpoint_host=_voice_agent_clean_text(handoff.get("endpoint_host") or handoff.get("endpoint"), limit=160),
            error="",
        )
    try:
        thread = threading.Thread(target=_voice_agent_probe_worker, kwargs={"handoff": dict(handoff), "timeout_seconds": timeout_seconds}, name=f"voice-agent-probe-{guild_id}", daemon=True)
        thread.start()
    except Exception as exc:
        with contextlib.suppress(_VoiceAgentProbeCancelled):
            _voice_agent_set_connection(guild_id, _expected_probe_id=probe_id, _probe_id="",
                state="connection_failed", stage="schedule_failed", connected_once=False,
                error=f"{type(exc).__name__}: {_short_text(exc, limit=180)}")
        raise
    return {"ok": True, "started": True, "state": "connection_probe_started", **_voice_agent_connection_summary(guild_id=guild_id, limit=5)}


def _voice_agent_clear_connection(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    removed = False
    with _VOICE_AGENT_SESSION_LOCK:
        if guild_id > 0:
            removed = _VOICE_AGENT_CONNECTION_MEMORY.pop(str(guild_id), None) is not None
        elif body.get("all"):
            removed = bool(_VOICE_AGENT_CONNECTION_MEMORY)
            _VOICE_AGENT_CONNECTION_MEMORY.clear()
    return {"ok": True, "cleared": removed, "state": "connection_cleared" if removed else "connection_not_found", **_voice_agent_connection_summary(limit=5)}

def _voice_agent_register_handoff_payload(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("discord_voice_handoff") if isinstance(body.get("discord_voice_handoff"), dict) else body
    guild_id = _voice_agent_int(body.get("guild_id") or raw.get("guild_id"), 0)
    channel_id = _voice_agent_int(body.get("channel_id") or raw.get("channel_id"), 0)
    if guild_id <= 0 or channel_id <= 0:
        raise RuntimeError("guild_id/channel_id obrigatórios para handoff de voz")
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_HANDOFF_ENABLED", True):
        raise RuntimeError("handoff do Worker Voice Agent desativado")
    ttl_seconds = max(10, min(180, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS", 60))))
    now_ms = _voice_agent_now_ms()
    session_id = _voice_agent_clean_text(raw.get("session_id"), limit=220)
    endpoint = _voice_agent_clean_text(raw.get("endpoint"), limit=220)
    token = str(raw.get("voice_token") or raw.get("token") or "").strip()
    if not (session_id and endpoint and token):
        raise RuntimeError("handoff incompleto: session_id/endpoint/voice_token obrigatórios")
    return {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "text_channel_id": str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
        "requester_id": str(_voice_agent_int(body.get("requester_id"), 0) or ""),
        "bot_user_id": str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
        "source": _voice_agent_clean_text(body.get("source") or "tts", limit=40) or "tts",
        "state": _voice_agent_clean_text(body.get("state") or "voice_handoff_registered_dry_run", limit=60) or "voice_handoff_registered_dry_run",
        "registered_by": _voice_agent_clean_text(body.get("registered_by") or "vps_control_plane", limit=60) or "vps_control_plane",
        "dry_run": bool(body.get("dry_run", True)),
        "voice_owner": str(body.get("voice_owner") or body.get("transport_owner") or "vps")[:40],
        "transport_owner": str(body.get("transport_owner") or body.get("voice_owner") or "vps")[:40],
        "allow_connection_probe": any(_voice_agent_flag(body.get(key)) for key in ("allow_connection_probe", "allow_probe")),
        "connection_policy": str(body.get("connection_policy") or "handoff_only_wait_for_voice_ownership")[:80],
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
        "expires_at_ms": now_ms + ttl_seconds * 1000,
        # Dados temporários necessários para a futura conexão Voice WS/UDP.
        # Eles ficam só em memória, nunca no arquivo voice-agent-state.json e nunca aparecem no painel.
        "session_id": session_id,
        "endpoint": endpoint,
        "endpoint_host": endpoint,
        "voice_token": token,
    }


def _voice_agent_register_handoff(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
        raise RuntimeError("Worker Voice Agent desativado")
    handoff = _voice_agent_register_handoff_payload(body)
    with _VOICE_AGENT_SESSION_LOCK:
        _voice_agent_cancel_probe(str(handoff["guild_id"]), reason="voice_handoff_replaced")
        _VOICE_AGENT_HANDOFF_MEMORY[str(handoff["guild_id"])] = handoff
    return {
        "ok": True,
        "registered": True,
        "state": "voice_handoff_registered_dry_run",
        "handoff": _voice_agent_public_handoff(handoff),
        **_voice_agent_handoff_summary(guild_id=_voice_agent_int(handoff.get("guild_id"), 0), limit=5),
    }


def _voice_agent_clear_handoff(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    removed = False
    with _VOICE_AGENT_SESSION_LOCK:
        if guild_id > 0:
            _voice_agent_cancel_probe(str(guild_id), reason="voice_handoff_cleared")
            removed = _VOICE_AGENT_HANDOFF_MEMORY.pop(str(guild_id), None) is not None
        elif body.get("all"):
            removed = bool(_VOICE_AGENT_HANDOFF_MEMORY)
            for key in _VOICE_AGENT_HANDOFF_MEMORY:
                _voice_agent_cancel_probe(str(key), reason="voice_handoff_cleared")
            _VOICE_AGENT_HANDOFF_MEMORY.clear()
    return {
        "ok": True,
        "cleared": removed,
        "state": "voice_handoff_cleared" if removed else "voice_handoff_not_found",
        "reason": _voice_agent_clean_text(body.get("reason"), limit=120),
        **_voice_agent_handoff_summary(limit=5),
    }


def _voice_agent_prune_sessions(state: dict[str, Any] | None = None) -> dict[str, Any]:
    with _VOICE_AGENT_SESSION_LOCK:
        now_ms = _voice_agent_now_ms()
        state = _voice_agent_load_state() if state is None else state
        sessions = state.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            state["sessions"] = sessions = {}
        stale = _voice_agent_expired_keys(sessions, now_ms=now_ms)
        for key in stale:
            sessions.pop(key, None)
        if stale:
            _voice_agent_save_state(state)
        return state


def _voice_agent_session_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    with _VOICE_AGENT_SESSION_LOCK:
        state = _voice_agent_prune_sessions()
        now_ms = _voice_agent_now_ms()
        sessions_dict = dict(state.get("sessions") or {})
    return _phone_worker_voice_state_module().session_summary(
        sessions_dict, now_ms=now_ms, guild_id=guild_id, limit=limit, public_record=_voice_agent_public_session,
        state_file=str(_voice_agent_state_file()))


def _voice_agent_register_session_payload(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    channel_id = _voice_agent_int(body.get("channel_id"), 0)
    if guild_id <= 0 or channel_id <= 0:
        raise RuntimeError("guild_id/channel_id obrigatórios para registrar sessão de voz")
    ttl_seconds = max(30, min(900, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_SESSION_TTL_SECONDS", 180))))
    now_ms = _voice_agent_now_ms()
    raw_voice = body.get("discord_voice") if isinstance(body.get("discord_voice"), dict) else {}
    voice = {
        "connected": bool(raw_voice.get("connected")),
        "channel_id": _voice_agent_int(raw_voice.get("channel_id"), channel_id),
        "session_id_present": bool(raw_voice.get("session_id_present")),
        "endpoint_present": bool(raw_voice.get("endpoint_present")),
        "endpoint_host": _voice_agent_clean_text(raw_voice.get("endpoint_host"), limit=160),
        "voice_token_present": bool(raw_voice.get("voice_token_present")),
        "self_deaf": raw_voice.get("self_deaf") if isinstance(raw_voice.get("self_deaf"), bool) else None,
        "self_mute": raw_voice.get("self_mute") if isinstance(raw_voice.get("self_mute"), bool) else None,
    }
    return {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "text_channel_id": str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
        "requester_id": str(_voice_agent_int(body.get("requester_id"), 0) or ""),
        "bot_user_id": str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
        "source": _voice_agent_clean_text(body.get("source") or "tts", limit=40) or "tts",
        "state": _voice_agent_clean_text(body.get("state") or "registered", limit=60) or "registered",
        "registered_by": _voice_agent_clean_text(body.get("registered_by") or "vps_control_plane", limit=60) or "vps_control_plane",
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
        "expires_at_ms": now_ms + ttl_seconds * 1000,
        "direct_tts_enabled": bool(body.get("direct_tts_enabled")),
        "discord_voice": voice,
        "note": "sem DISCORD_TOKEN e sem voice token bruto",
    }


def _voice_agent_register_session(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
        raise RuntimeError("Worker Voice Agent desativado")
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED", True):
        raise RuntimeError("sessão compartilhada do Worker Voice Agent desativada")
    session = _voice_agent_register_session_payload(body)
    with _VOICE_AGENT_SESSION_LOCK:
        state = _voice_agent_prune_sessions()
        sessions = state.setdefault("sessions", {})
        sessions[str(session["guild_id"])] = session
        _voice_agent_save_state(state)
    summary = _voice_agent_session_summary(guild_id=_voice_agent_int(session.get("guild_id"), 0), limit=5)
    return {
        "ok": True,
        "registered": True,
        "state": "session_registered",
        "session": _voice_agent_public_session(session),
        **summary,
    }


def _voice_agent_clear_session(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    with _VOICE_AGENT_SESSION_LOCK:
        state = _voice_agent_prune_sessions()
        sessions = state.setdefault("sessions", {})
        removed = False
        if guild_id > 0:
            removed = sessions.pop(str(guild_id), None) is not None
        elif body.get("all"):
            removed = bool(sessions)
            sessions.clear()
        _voice_agent_save_state(state)
    summary = _voice_agent_session_summary(limit=5)
    return {
        "ok": True,
        "cleared": removed,
        "state": "session_cleared" if removed else "session_not_found",
        "reason": _voice_agent_clean_text(body.get("reason"), limit=120),
        **summary,
    }

def _voice_agent_snapshot(*, music_agent: dict[str, Any] | None = None, tts_agent: dict[str, Any] | None = None) -> dict[str, Any]:
    hooks = SimpleNamespace(
        current_profile=_current_core_worker_profile,
        env_list=_env_list,
        profile_capabilities=_core_worker_profile_capabilities,
        env_bool=_env_bool,
        safe_mode_enabled=_phone_worker_safe_mode_enabled,
        safe_telemetry=_safe_telemetry,
        music_agent_snapshot=_music_agent_snapshot,
        tts_agent_snapshot=_tts_agent_snapshot,
        session_summary=_voice_agent_session_summary,
        handoff_summary=_voice_agent_handoff_summary,
        connection_summary=_voice_agent_connection_summary,
        transfer_summary=_voice_agent_transfer_summary,
        default_worker_id=_default_worker_id,
        phone_worker_version=PHONE_WORKER_VERSION,
    )
    return _phone_worker_music_bridge_module("voz_compartilhada").voice_agent_snapshot(
        hooks, music_agent=music_agent, tts_agent=tts_agent
    )

def _system_status() -> dict[str, Any]:
    auto_boot_repair = _auto_repair_local_boot_if_needed()
    disk = shutil.disk_usage(Path.home())
    load = None
    try:
        load = os.getloadavg()
    except Exception:
        load = None
    music_agent_snapshot = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
    tts_agent_snapshot = _safe_telemetry("tts agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False, "state": "telemetry_failed"})
    voice_agent_snapshot = _safe_telemetry(
        "voice agent",
        lambda: _voice_agent_snapshot(music_agent=music_agent_snapshot, tts_agent=tts_agent_snapshot),
        {"ok": False, "available": False, "state": "telemetry_failed"},
    )
    return {
        "ok": True,
        "worker": "phone-worker",
        "source": "termux-phone-worker",
        "runtime_kind": "termux",
        "physical_worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "runtime_mode": CORE_WORKER_RUNTIME_MODE,
        "runtime": {
            "mode": CORE_WORKER_RUNTIME_MODE,
            "current_worker": "termux-phone-worker",
            "internal_runtime": CORE_WORKER_INTERNAL_RUNTIME_STATE,
            "migration_stage": "termux-bootstrap-builder",
        },
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "name": _default_worker_name(),
        "version": PHONE_WORKER_VERSION,
        "source_hash": _phone_worker_source_hash(),
        "worker_update_transports": ["inline-b64-v1", "zip-v1"],
        "profile": _current_core_worker_profile(),
        "profile_label": _core_worker_profile_label(_current_core_worker_profile()),
        "core_worker_heartbeat": _heartbeat_configured(),
        "core_worker_jobs": {"configured": _core_worker_jobs_configured(), **_core_job_runtime_snapshot()},
        "core_worker_network": _core_worker_network_runtime_snapshot(),
        "connection": _ssh_connection_snapshot(),
        "music_node": _inactive_music_node_snapshot(),
        "music_agent": music_agent_snapshot,
        "voice_agent": voice_agent_snapshot,
        "music_voice_dependencies": _safe_telemetry("music voice dependencies", _music_voice_dependencies_snapshot, {"ok": False, "missing": ["unknown"]}),
        "scripts": _script_inventory(),
        "boot": _safe_telemetry("boot", _termux_boot_status_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "shell_autostart": _safe_telemetry("shell autostart", _termux_shell_autostart_status_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "auto_boot_repair": auto_boot_repair,
        "supervisor": _safe_telemetry("supervisor", _runtime_supervisor_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "sshd": _safe_telemetry("sshd", _sshd_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "supported_tasks": list(SUPPORTED_DIRECT_TASKS),
        "supported_core_worker_jobs": _supported_core_worker_job_types(),
        "pid": os.getpid(),
        "process_started_at": START_TIME,
        "status_updated_at": time.time(),
        "control_plane_alive": True,
        "direct_http_state": _DIRECT_HTTP_STATE,
        "http_port": _EFFECTIVE_HTTP_PORT,
        "preferred_http_port": TERMUX_PRIMARY_HTTP_PORT,
        "recovery_http_port": TERMUX_RECOVERY_HTTP_PORT,
        "last_heartbeat_ok_at": _LAST_HEARTBEAT_OK_AT or None,
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
        "turbo_dependencies": _turbo_dependency_snapshot(),
        "tts_agent": tts_agent_snapshot,
        "turbo_cache": _worker_turbo_cache_snapshot(),
    }


def _local_agent_status_payload(*, host: str, port: int) -> dict[str, Any]:
    profile = _current_core_worker_profile()
    safe_mode = _phone_worker_safe_mode_enabled()
    status = _safe_telemetry("system", _system_status, {"ok": False})
    roles = _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))
    if safe_mode:
        roles = [item for item in roles if not str(item).lower().startswith("music")]
        capabilities = [item for item in capabilities if not str(item).lower().startswith("music")]
    return {
        "ok": True,
        "local_only": True,
        "worker": "phone-worker",
        "source": "termux-phone-worker",
        "runtime_kind": "termux",
        "physical_worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "runtime_mode": CORE_WORKER_RUNTIME_MODE,
        "runtime": {
            "mode": CORE_WORKER_RUNTIME_MODE,
            "current_worker": "termux-phone-worker",
            "internal_runtime": CORE_WORKER_INTERNAL_RUNTIME_STATE,
            "migration_stage": "termux-bootstrap-builder",
        },
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "name": _default_worker_name(),
        "version": PHONE_WORKER_VERSION,
        "source_hash": _phone_worker_source_hash(),
        "worker_update_transports": ["inline-b64-v1", "zip-v1"],
        "profile": profile,
        "profile_label": _core_worker_profile_label(profile),
        "safe_mode": safe_mode,
        "roles": roles[:16],
        "capabilities": capabilities[:24],
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
        "tts_agent": status.get("tts_agent") if isinstance(status.get("tts_agent"), dict) else _safe_telemetry("tts agent", _tts_agent_snapshot, {"ok": False}),
        "voice_agent": status.get("voice_agent") if isinstance(status.get("voice_agent"), dict) else _safe_telemetry("voice agent", _voice_agent_snapshot, {"ok": False}),
        "boot_ok": ((status.get("boot") or {}).get("ok") if isinstance(status.get("boot"), dict) else None),
        "supervisor_ok": ((status.get("supervisor") or {}).get("supervisor_ok") if isinstance(status.get("supervisor"), dict) else None),
        "sshd_ok": ((status.get("sshd") or {}).get("ok") if isinstance(status.get("sshd"), dict) else None),
        "sshd_summary": ((status.get("sshd") or {}).get("summary") if isinstance(status.get("sshd"), dict) else None),
        "shell_autostart_ok": ((status.get("shell_autostart") or {}).get("ok") if isinstance(status.get("shell_autostart"), dict) else None),
        "shell_autostart_summary": ((status.get("shell_autostart") or {}).get("summary") if isinstance(status.get("shell_autostart"), dict) else None),
        "note": "Rota local para o APK Core Worker; Termux segue como runtime oficial nesta etapa e não expõe token.",
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
            # SSH reverse forwards also arrive from loopback. Keep the APK's
            # legacy local access, but never treat a tunnel caller as trusted
            # local maintenance without the existing worker credential.
            if _ssh_transport_enabled():
                if not self.token:
                    _error(self, HTTPStatus.FORBIDDEN, "rota local exige token do worker no modo SSH")
                    return False
                return self._require_auth()
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
        if path.startswith("/music/stream/"):
            if not self._require_auth():
                return
            stream_id = path.rsplit("/", 1)[-1]
            _stream_music_pcm(self, stream_id)
            return
        if path == "/local/status":
            if not self._require_local_client():
                return
            host, port = self._bind_host_port()
            _json_response(self, HTTPStatus.OK, _local_agent_status_payload(host=host, port=port))
            return
        if path in {"/tts-agent/health", "/tts-agent/status"}:
            if not self._require_auth():
                return
            try:
                agent = self._task_tts_agent_status({})
                payload = {
                    "ok": bool(agent.get("ok")),
                    "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
                    "version": PHONE_WORKER_VERSION,
                    "runtime_kind": "termux",
                    "pid": os.getpid(),
                    "process_started_at": START_TIME,
                    "connection": _ssh_connection_snapshot(),
                    "tts_agent": agent,
                }
                _json_response(self, HTTPStatus.OK, payload)
            except Exception as exc:
                _error(self, HTTPStatus.SERVICE_UNAVAILABLE, f"{type(exc).__name__}: {_short_text(exc, limit=180)}")
            return
        if path in {"/music-agent/health", "/music-agent/status"}:
            if not self._require_auth():
                return
            payload = {
                "ok": True,
                "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
                "version": PHONE_WORKER_VERSION, "runtime_kind": "termux", "pid": os.getpid(),
                "profile": _current_core_worker_profile(), "connection": _ssh_connection_snapshot(),
                "music_agent": _music_agent_light_snapshot(),
            }
            _json_response(self, HTTPStatus.OK, payload)
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
                heartbeat_ok = _send_core_worker_heartbeat_once(host=host, port=port, timeout=max(3.0, min(15.0, _env_float("CORE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 6.0))))
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
                result["synced_to_vps"] = _send_core_worker_heartbeat_once(host=host, port=port, timeout=max(3.0, min(15.0, _env_float("CORE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 6.0)))) if _heartbeat_configured() else False
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
        if path in {"/tts-agent/synthesize.stream", "/tts-agent/cache.raw"}:
            if not self._require_auth():
                return
            transport = _tts_transport_module()
            if transport is None:
                _error(self, HTTPStatus.NOT_FOUND, "protocolo TTS v2 indisponível")
                return
            try:
                if path.endswith("cache.raw"):
                    transport.store_binary_cache(self, sys.modules[__name__])
                else:
                    body = self._read_json()
                    if body is not None:
                        transport.serve_stream(self, body, sys.modules[__name__])
            except Exception as exc:
                self.close_connection = True
                _error(self, HTTPStatus.BAD_GATEWAY, f"{type(exc).__name__}: {_short_text(exc, limit=220)}")
            return
        if path == "/tts-agent/synthesize.raw":
            if not self._require_auth():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                result = self._task_tts_agent_synthesize(body, raw_response=True)
                max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
                raw = result.pop("_raw_audio", None)
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    raw = _b64decode(str(result.get("data_b64") or ""), max_bytes=max_audio_bytes)
                if not raw:
                    raise RuntimeError("TTS Agent não retornou áudio")
                raw = bytes(raw)
                result["worker_id"] = str(result.get("worker_id") or os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip()
                result["worker_version"] = str(result.get("worker_version") or PHONE_WORKER_VERSION)
                _audio_response(self, HTTPStatus.OK, raw, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_GATEWAY, f"{type(exc).__name__}: {_short_text(exc, limit=220)}")
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
            elif task in {"network_probe", "ssh_connection_diagnostic", "tailscale_status", "worker_logs", "worker_update", "vps_assist_probe", "hash_batch", "endpoint_probe", "media_probe", "audio_convert", "log_digest", "zip_audit", "boot_status", "boot_repair", "service_status", "service_start", "service_stop", "service_restart", "ffmpeg_check", "ffprobe_check"}:
                payload = _execute_core_worker_job({"type": task, "payload": body}, max_body_bytes=self.max_body_bytes, max_output_bytes=self.max_output_bytes, job_timeout=self.job_timeout)
            elif task == "sha256":
                payload = self._task_sha256(body)
            elif task == "zip":
                payload = self._task_zip(body)
            elif task == "zip_validate":
                payload = self._task_zip_validate(body)
            elif task == "maintenance_plan":
                payload = self._task_maintenance_plan(body)
            elif task in {"music_agent_status", "music_agent_command"}:
                payload = self._task_music_agent_proxy(body)
            elif task == "music_ytdlp_resolve":
                payload = self._task_music_ytdlp_resolve(body)
            elif task == "text_stats":
                payload = self._task_text_stats(body)
            elif task == "emoji_recolor":
                payload = self._task_emoji_recolor(body)
            elif task == "tts_cache_lookup":
                payload = self._task_tts_cache_lookup(body)
            elif task == "tts_cache_store":
                payload = self._task_tts_cache_store(body)
            elif task == "tts_synthesize_benchmark":
                payload = self._task_tts_synthesize_benchmark(body)
            elif task == "tts_synthesize_piper":
                payload = self._task_tts_synthesize_piper(body)
            elif task == "tts_agent_status":
                payload = self._task_tts_agent_status(body)
            elif task in {"tts_android_voices", "tts_atts_voices", "android_tts_voices"}:
                payload = self._task_tts_android_voices(body)
            elif task == "tts_agent_synthesize":
                payload = self._task_tts_agent_synthesize(body)
            elif task == "voice_agent_status":
                payload = self._task_voice_agent_status(body)
            elif task == "voice_agent_register_session":
                payload = self._task_voice_agent_register_session(body)
            elif task == "voice_agent_clear_session":
                payload = self._task_voice_agent_clear_session(body)
            elif task == "voice_agent_guild_status":
                payload = self._task_voice_agent_guild_status(body)
            elif task == "voice_agent_register_handoff":
                payload = self._task_voice_agent_register_handoff(body)
            elif task == "voice_agent_clear_handoff":
                payload = self._task_voice_agent_clear_handoff(body)
            elif task == "voice_agent_handoff_status":
                payload = self._task_voice_agent_handoff_status(body)
            elif task == "voice_agent_prepare_transfer":
                payload = self._task_voice_agent_prepare_transfer(body)
            elif task == "voice_agent_begin_transfer":
                payload = self._task_voice_agent_begin_transfer(body)
            elif task == "voice_agent_release_transfer":
                payload = self._task_voice_agent_release_transfer(body)
            elif task == "voice_agent_transfer_status":
                payload = self._task_voice_agent_transfer_status(body)
            elif task == "voice_agent_play_tts":
                payload = self._task_voice_agent_play_tts(body)
            elif task == "voice_agent_probe_connection":
                payload = self._task_voice_agent_probe_connection(body)
            elif task == "voice_agent_connection_status":
                payload = self._task_voice_agent_connection_status(body)
            elif task == "voice_agent_clear_connection":
                payload = self._task_voice_agent_clear_connection(body)
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

    def _normalize_tts_edge_rate(self, raw: Any) -> str:
        return _phone_worker_tts_policy_module().normalize_edge_rate(raw)

    def _normalize_tts_edge_pitch(self, raw: Any) -> str:
        return _phone_worker_tts_policy_module().normalize_edge_pitch(raw)

    def _normalize_tts_gtts_language(self, raw: Any) -> str:
        return _phone_worker_tts_policy_module().normalize_gtts_language(raw)


    def _ensure_tts_cache_allowed(self) -> tuple[list[str], list[str]]:
        profile = _current_core_worker_profile()
        roles, capabilities = _current_core_worker_roles_and_capabilities()
        caps = set(roles) | set(capabilities)
        if profile != "turbo" or "cache-worker" not in caps:
            raise RuntimeError("tts_cache é restrito ao perfil turbo com cache-worker")
        return roles, capabilities

    def _tts_cache_root(self) -> Path:
        configured = str(os.getenv("PHONE_WORKER_TTS_CACHE_DIR", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "phone-worker" / "cache" / "tts"

    def _tts_cache_limits(self) -> tuple[int, int]:
        def _as_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(float(os.getenv(name, str(default)) or default))
            except Exception:
                value = default
            return max(minimum, min(maximum, value))
        max_mb = _as_int("PHONE_WORKER_TTS_CACHE_MAX_MB", 4096, 64, 32768)
        max_files = _as_int("PHONE_WORKER_TTS_CACHE_MAX_FILES", 20000, 64, 100000)
        return max_mb * 1024 * 1024, max_files

    def _sanitize_tts_cache_key(self, raw: Any) -> str:
        return _phone_worker_tts_policy_module().sanitize_cache_key(raw)

    def _normalize_tts_cache_format(self, raw: Any) -> str:
        return _phone_worker_tts_policy_module().normalize_cache_format(raw)

    def _tts_cache_path(self, key: str, audio_format: str) -> Path:
        return self._tts_cache_root() / f"{key}.{self._normalize_tts_cache_format(audio_format)}"

    def _find_tts_cache_file(self, key: str) -> tuple[Path | None, str]:
        return _phone_worker_tts_cache_module().find_file(self._tts_cache_root(), key)

    def _touch_tts_cache_file(self, path: Path) -> None:
        return _phone_worker_tts_cache_module().touch_file(path,
            lock=_TTS_CACHE_MAINTENANCE_LOCK, touches=_TTS_CACHE_TOUCHES,
            monotonic=time.monotonic, utime=os.utime)

    def _prune_tts_cache(self, *, protected: Path | None = None) -> None:
        _prune_audio_cache(self._tts_cache_root(), *self._tts_cache_limits(), protected=protected)

    def _schedule_tts_cache_prune(self, *, protected: Path, piper: bool = False) -> bool:
        callback = self._prune_piper_cache if piper else self._prune_tts_cache
        return _submit_tts_cache_maintenance(callback, protected=protected)

    def _task_tts_cache_lookup(self, body: dict[str, Any]) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_cache_allowed()
        started = time.monotonic()
        key = self._sanitize_tts_cache_key(body.get("cache_key"))
        max_audio_bytes = max(1, int(body.get("max_audio_bytes") or self.max_output_bytes))
        path, audio_format = self._find_tts_cache_file(key)
        if path is None:
            total_ms = (time.monotonic() - started) * 1000.0
            return {
                "ok": False,
                "engine": str(body.get("engine") or "tts-cache")[:40],
                "cache_hit": False,
                "cache_exists_before": False,
                "cache_key": key[:16],
                "error": "worker turbo cache miss",
                "worker_profile": _current_core_worker_profile(),
                "worker_version": PHONE_WORKER_VERSION,
                "roles": roles[:16],
                "capabilities": capabilities[:24],
                "worker_synth_ms": 0.0,
                "worker_total_ms": round(total_ms, 2),
                "total_ms": round(total_ms, 2),
                "logs": [f"tts cache miss key={key[:16]}"],
            }
        read_started = time.monotonic()
        data = _phone_worker_tts_cache_module().read_bytes(path)
        read_ms = (time.monotonic() - read_started) * 1000.0
        if not data:
            raise RuntimeError("cache TTS vazio")
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"cache TTS grande demais: {len(data)} bytes")
        self._touch_tts_cache_file(path)
        digest = hashlib.sha256(data).hexdigest()
        total_ms = (time.monotonic() - started) * 1000.0
        return {
            "ok": True,
            "engine": str(body.get("engine") or "tts-cache")[:40],
            "audio_format": audio_format,
            "cache_hit": True,
            "cache_key": key[:16],
            "cache_file": path.name,
            "cache_read_ms": round(read_ms, 2),
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": 0.0,
            "worker_total_ms": round(total_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": [f"tts cache hit {path.name} {len(data)} B em {read_ms:.1f} ms"],
            "data_b64": _b64encode(data, max_bytes=max_audio_bytes),
        }

    def _task_tts_cache_store(self, body: dict[str, Any]) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_cache_allowed()
        started = time.monotonic()
        key = self._sanitize_tts_cache_key(body.get("cache_key"))
        audio_format = self._normalize_tts_cache_format(body.get("audio_format"))
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        if not data:
            raise RuntimeError("data_b64 vazio para cache TTS")
        expected_hash = str(body.get("sha256") or "").strip().lower()
        actual_hash = hashlib.sha256(data).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise RuntimeError("sha256 do cache TTS não confere")
        root = self._tts_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        path = self._tts_cache_path(key, audio_format)
        _phone_worker_tts_cache_module().publish_bytes(path, data,
            os_api=os, thread_id=threading.get_ident)
        self._touch_tts_cache_file(path)
        self._schedule_tts_cache_prune(protected=path)
        total_ms = (time.monotonic() - started) * 1000.0
        return {
            "ok": True,
            "engine": str(body.get("engine") or "tts-cache")[:40],
            "audio_format": audio_format,
            "cache_hit": False,
            "cache_stored": True,
            "cache_key": key[:16],
            "cache_file": path.name,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_total_ms": round(total_ms, 2),
            "size": len(data),
            "sha256": actual_hash,
            "logs": [f"tts cache store {path.name} {len(data)} B"],
        }

    def _ensure_tts_benchmark_turbo_allowed(self) -> tuple[list[str], list[str]]:
        profile = _current_core_worker_profile()
        roles, capabilities = _current_core_worker_roles_and_capabilities()
        caps = set(roles) | set(capabilities)
        if profile != "turbo" or "tts-synth" not in caps or "tts-benchmark" not in caps:
            raise RuntimeError("tts_synthesize_benchmark é restrito ao perfil turbo com tts-synth/tts-benchmark")
        return roles, capabilities

    def _ensure_tts_piper_turbo_allowed(self) -> tuple[list[str], list[str]]:
        profile = _current_core_worker_profile()
        roles, capabilities = _current_core_worker_roles_and_capabilities()
        caps = set(roles) | set(capabilities)
        if profile != "turbo" or "tts-synth" not in caps:
            raise RuntimeError("tts_synthesize_piper é restrito ao perfil turbo com tts-synth")
        return roles, capabilities


    def _task_voice_agent_status(self, body: dict[str, Any]) -> dict[str, Any]:
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot["music_agent"] = {
            "ok": bool(music_agent.get("ok")),
            "available": bool(music_agent.get("available")),
            "configured": bool(music_agent.get("configured")),
            "runtime_version": str(music_agent.get("runtime_version") or music_agent.get("version") or "")[:40],
            "latency_ms": music_agent.get("latency_ms"),
            "error": str(music_agent.get("error") or "")[:160],
        }
        snapshot["tts_agent"] = {
            "ok": bool(tts_agent.get("ok")),
            "available": bool(tts_agent.get("available")),
            "synth_ready": bool(tts_agent.get("synth_ready")),
            "selected_engine": str(tts_agent.get("selected_engine") or tts_agent.get("preferred_engine") or "")[:40],
            "available_engines": list(tts_agent.get("available_engines") or [])[:8],
            "last_error": str(tts_agent.get("last_error") or "")[:160],
        }
        snapshot.setdefault("logs", []).append("voice agent status coletado; direct_tts_voice ainda depende de etapa futura")
        return snapshot

    def _task_voice_agent_register_session(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_register_session(body)
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("sessão de voz registrada pela VPS; worker ainda não recebeu controle geral do bot")
        return snapshot

    def _task_voice_agent_clear_session(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_clear_session(body)
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("sessão de voz removida pelo controle da VPS")
        return snapshot

    def _task_voice_agent_guild_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_session_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["ok"] = bool(snapshot.get("ok"))
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de sessão por guild coletado")
        return snapshot

    def _task_voice_agent_register_handoff(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_register_handoff(body)
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("handoff temporário de voz recebido em dry-run; raw token guardado só em memória")
        return snapshot

    def _task_voice_agent_clear_handoff(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_clear_handoff(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("handoff temporário de voz limpo")
        return snapshot

    def _task_voice_agent_handoff_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_handoff_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de handoff de voz coletado")
        return snapshot

    def _task_voice_agent_prepare_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_prepare_transfer(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("transferência de posse preparada; owner ainda fica na VPS")
        return snapshot

    def _task_voice_agent_begin_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_begin_transfer(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("posse de voz concedida ao worker por confirmação explícita da VPS; ainda sem áudio automático")
        return snapshot

    def _task_voice_agent_release_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_release_transfer(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("posse de voz devolvida para a VPS")
        return snapshot

    def _task_voice_agent_transfer_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_transfer_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de transferência de posse coletado")
        return snapshot

    def _task_voice_agent_play_tts(self, body: dict[str, Any]) -> dict[str, Any]:
        if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
            raise RuntimeError("Worker Voice Agent desativado")
        if not _env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED", True):
            raise RuntimeError("TTS direto do Worker Voice Agent desativado")
        performance = _phone_worker_tts_policy_module().performance_request(body)
        if _tts_agent_normalize_engine(body.get("engine")) == "teto":
            # Validate even a prebuilt request before changing a voice lease.
            _ttv_request_settings(body)
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        channel_id = _voice_agent_int(body.get("voice_channel_id") or body.get("channel_id"), 0)
        if guild_id <= 0 or channel_id <= 0:
            raise RuntimeError("guild_id/channel_id obrigatórios para TTS direto")
        text = _voice_agent_clean_text(body.get("text") or body.get("content"), limit=_env_int("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS", 600))
        if performance:
            text = str(body.get("text") or "").strip()
        if not text and performance.get("score") is None and not (body.get("audio_b64") or body.get("audio_url") or body.get("url")):
            raise RuntimeError("texto/audio obrigatório para TTS direto")
        now_ms = _voice_agent_now_ms()
        transfer: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            transfers = _voice_agent_prune_transfers()
            transfer = dict(transfers.get(str(guild_id)) or {})
        if any(_voice_agent_flag(body.get(key)) for key in ("confirm_transfer", "confirm", "manual")):
            if str(transfer.get("voice_owner") or transfer.get("current_owner") or "").lower() != "worker":
                try:
                    transfer_result = _voice_agent_begin_transfer({**body, "confirm_transfer": True, "requested_owner": "worker"})
                    transfer = dict(transfer_result.get("transfer") or {})
                except Exception:
                    # Direct TTS can still be valid when the VPS had no active voice handoff;
                    # mark an explicit worker-owned lease for the Music Agent gateway path.
                    transfer = _voice_agent_set_transfer(
                        guild_id,
                        channel_id=str(channel_id),
                        text_channel_id=str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
                        requester_id=str(_voice_agent_int(body.get("requester_id"), 0) or ""),
                        bot_user_id=str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
                        source="tts_worker_voice_direct",
                        state="worker_ownership_granted_music_agent_gateway",
                        current_owner="worker",
                        voice_owner="worker",
                        requested_owner="worker",
                        lease_id=f"direct-tts:{guild_id}:{channel_id}:{now_ms}",
                        allow_connection_probe=False,
                        probe_authorized=False,
                        reason="TTS direto via Music Agent gateway; sem handoff VPS ativo",
                        error="",
                        expires_at_ms=now_ms + max(10, min(180, _env_int("PHONE_WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", 45))) * 1000,
                    )
        _voice_agent_set_connection(
            guild_id,
            channel_id=str(channel_id),
            state="worker_direct_tts_starting",
            stage="music_agent_voice_tts",
            direct_tts=True,
            dry_run=False,
            connected_once=False,
            error="",
        )
        started = time.perf_counter()
        proxy_body = dict(body)
        proxy_body["task"] = "music_agent_command"
        proxy_body["action"] = "voice_tts"
        proxy_body["voice_channel_id"] = channel_id
        proxy_body["channel_id"] = channel_id
        proxy_body["text"] = text or str(body.get("text") or body.get("content") or "")
        proxy_body["timeout_seconds"] = max(3.0, min(120.0 if performance else 90.0, float(body.get("timeout_seconds") or
            (120.0 if performance else _env_float("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS", 30.0)))))
        if (
            (proxy_body.get("text") or performance)
            and not (proxy_body.get("audio_b64") or proxy_body.get("audio_url") or proxy_body.get("url"))
            and _env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_WITH_TTS_AGENT", True)
        ):
            try:
                synth_body = dict(proxy_body)
                synth_body["task"] = "tts_agent_synthesize"
                synth_body.setdefault("cache_mode", "prefer")
                synth_body["timeout_seconds"] = max(3, min(int(proxy_body["timeout_seconds"]),
                    120 if performance else _env_int("PHONE_WORKER_TTS_AGENT_DIRECT_PREBUILD_TIMEOUT_SECONDS", 18)))
                synth_body["max_audio_bytes"] = max(1024, min(self.max_output_bytes, _env_int("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_BYTES", 12 * 1024 * 1024)))
                synth_result = self._task_tts_agent_synthesize(synth_body)
                if isinstance(synth_result, dict) and synth_result.get("ok") and synth_result.get("data_b64"):
                    proxy_body["audio_b64"] = str(synth_result.get("data_b64") or "")
                    proxy_body["audio_format"] = str(synth_result.get("audio_format") or "mp3")
                    proxy_body["engine"] = str(synth_result.get("selected_engine") or synth_result.get("engine") or proxy_body.get("engine") or "tts-agent")
                    proxy_body["selected_engine"] = proxy_body["engine"]
                    proxy_body["prebuilt_audio"] = True
                    proxy_body["prebuilt_audio_source"] = "phone_worker_tts_agent"
                    proxy_body["tts_agent_cache_hit"] = bool(synth_result.get("cache_hit"))
                    proxy_body["tts_agent_synth_ms"] = synth_result.get("worker_synth_ms")
                    proxy_body["tts_agent_total_ms"] = synth_result.get("worker_total_ms") or synth_result.get("total_ms")
            except Exception as exc:
                if performance:
                    raise RuntimeError(f"não foi possível gerar o canto/efeitos TTV: {_short_text(exc, limit=180)}") from exc
                # A falha de prebuild não deve quebrar o TTS direto: o Music Agent ainda
                # pode sintetizar no caminho antigo. Guardamos o motivo só para diagnóstico.
                proxy_body["tts_agent_prebuild_error"] = f"{type(exc).__name__}: {_short_text(exc, limit=140)}"
        if _tts_agent_normalize_engine(body.get("engine")) == "teto":
            if not (proxy_body.get("audio_b64") or proxy_body.get("audio_url") or proxy_body.get("url")):
                raise RuntimeError("Teto indisponível: síntese obrigatória antes da reprodução direta")
            if _tts_agent_normalize_engine(proxy_body.get("engine")) != "teto":
                raise RuntimeError("Teto não permite reprodução direta com outra voz")
        result = self._task_music_agent_proxy(proxy_body)
        ok = bool(isinstance(result, dict) and result.get("ok", False))
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        conn = _voice_agent_set_connection(
            guild_id,
            channel_id=str(channel_id),
            state="worker_direct_tts_ok" if ok else "worker_direct_tts_failed",
            stage="music_agent_voice_tts_done" if ok else "music_agent_voice_tts_failed",
            direct_tts=True,
            dry_run=False,
            connected_once=ok,
            ready_received=ok,
            latency_ms=elapsed_ms,
            playback_ms=float(result.get("playback_ms") or result.get("elapsed_ms") or 0.0) if isinstance(result, dict) else 0.0,
            engine=str(result.get("engine") or body.get("engine") or "")[:60] if isinstance(result, dict) else str(body.get("engine") or "")[:60],
            error="" if ok else _short_text((result or {}).get("error") if isinstance(result, dict) else "Music Agent retornou resposta inválida", limit=180),
        )
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        if not ok:
            snapshot.update({"ok": False, "direct_tts": False, "error": str((result or {}).get("error") if isinstance(result, dict) else "Music Agent falhou")[:220]})
        else:
            snapshot.update({"ok": True, "direct_tts": True, "engine": str(result.get("engine") or body.get("engine") or ""), "playback_ms": result.get("playback_ms"), "elapsed_ms": elapsed_ms})
        snapshot["worker_result"] = result
        snapshot["connection"] = _voice_agent_public_connection(conn)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("TTS direto worker→Discord executado via Music Agent voice plane" if ok else "TTS direto worker→Discord falhou; VPS deve usar fallback")
        return snapshot

    def _task_voice_agent_probe_connection(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_start_connection_probe(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("voice connection dry-run iniciado; sem áudio e sem DISCORD_TOKEN no worker")
        return snapshot

    def _task_voice_agent_connection_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_connection_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de conexão voice dry-run coletado")
        return snapshot

    def _task_voice_agent_clear_connection(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_clear_connection(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("estado de conexão voice dry-run limpo")
        return snapshot

    def _task_tts_agent_status(self, body: dict[str, Any]) -> dict[str, Any]:
        snapshot = _tts_agent_snapshot()
        snapshot.update({
            "ok": bool(snapshot.get("ok")),
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "connection": _ssh_connection_snapshot(),
            "logs": [f"tts agent {snapshot.get('state')} engines={','.join(snapshot.get('available_engines') or [])}"],
        })
        return snapshot

    def _task_tts_android_voices(self, body: dict[str, Any]) -> dict[str, Any]:
        locale = str(body.get("locale") or body.get("language") or "").strip()
        try:
            limit = int(body.get("limit") or 500)
        except Exception:
            limit = 500
        result = _android_tts_voices(locale=locale, limit=limit)
        result.update({
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "logs": [f"android tts voices locale={locale or 'all'} total={result.get('total', 0)} returned={len(result.get('voices') or [])}"],
        })
        return result

    def _tts_agent_engine_order(self, body: dict[str, Any], available: list[str]) -> list[str]:
        return _phone_worker_tts_policy_module().engine_order(body, available, preferred_default=body.get("preferred_engine") or os.getenv("PHONE_WORKER_TTS_AGENT_ENGINE"))

    def _tts_agent_standard_cache_enabled(self, roles: list[str], capabilities: list[str]) -> bool:
        if not _env_bool("PHONE_WORKER_TTS_AGENT_CACHE_ENABLED", True):
            return False
        if not _env_bool("PHONE_WORKER_TTS_AGENT_STANDARD_CACHE_ENABLED", True):
            return False
        profile = _current_core_worker_profile()
        if profile != "turbo" and not _env_bool("PHONE_WORKER_TTS_AGENT_CACHE_ALLOW_NON_TURBO", False):
            return False
        return True

    def _tts_agent_standard_cache_key(self, body: dict[str, Any], *, engine: str) -> str:
        normalized_engine = _tts_agent_normalize_engine(engine or body.get("engine"))
        # Only TTV consults renderer status/environment, at the established boundary.
        fingerprint, base_pitch = "unavailable", "C4"
        if normalized_engine == "teto":
            controls = _ttv_request_settings(body)
            status = _ttv_voice_state(_teto_status(), controls["ttv_voice_id"])
            fingerprint = str(status.get("fingerprint") or "unavailable")
            language = controls["ttv_language"]
            if language == "en-US" or controls["ttv_voice_id"] == "hatsune-miku":
                fingerprint = str((status.get("ttv_language_fingerprints") or {}).get(language) or "unavailable")
            if body.get("ttv_score") is not None:
                fingerprint = str((status.get("singing_fingerprints") or {}).get(language) or fingerprint)
                fingerprint = "singing-v5|" + fingerprint
            if controls["ttv_voice_id"] == "kasane-teto" and _teto_backend() == "worldline-r":
                # Keep the established UTAU cache contract while isolating the
                # native phrase renderer, including when no fingerprint exists.
                identity = ["worldline-r", status.get("renderer_version", ""),
                            _teto_renderer_config_fingerprint(), fingerprint]
                fingerprint = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
            prefix = "MIKU" if controls["ttv_voice_id"] == "hatsune-miku" else "TETO"
            base_pitch = str(os.getenv(f"PHONE_WORKER_{prefix}_BASE_PITCH") or "C4")
        return _phone_worker_tts_policy_module().standard_cache_key(
            body, engine=normalized_engine, sanitize_key=self._sanitize_tts_cache_key,
            normalize_rate=self._normalize_tts_edge_rate, normalize_pitch=self._normalize_tts_edge_pitch,
            normalize_language=self._normalize_tts_gtts_language,
            teto_fingerprint=fingerprint, teto_base_pitch=base_pitch,
            teto_speech_rate=controls["ttv_speech_rate"] if normalized_engine == "teto" else 1.0,
        )

    def _tts_agent_cache_mode_allows_read(self, body: dict[str, Any]) -> bool:
        return _phone_worker_tts_policy_module().cache_mode_allows_read(body)

    def _tts_agent_cache_mode_allows_store(self, body: dict[str, Any]) -> bool:
        return _phone_worker_tts_policy_module().cache_mode_allows_store(body)

    def _tts_agent_standard_cache_hit(self, *, key: str, engine: str, roles: list[str], capabilities: list[str], logs: list[str], started: float, max_audio_bytes: int, stage_ms: dict[str, float] | None = None, available_engines: list[str] | None = None, raw_response: bool = False, teto_pitch_semitones: Any = 0.0, ttv_settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
        stage_ms = stage_ms if isinstance(stage_ms, dict) else {}
        lookup_started = time.monotonic()
        path, audio_format = self._find_tts_cache_file(key)
        stage_ms["cache_lookup"] = round((time.monotonic() - lookup_started) * 1000.0, 2)
        if path is None:
            return None
        read_started = time.monotonic()
        data = _phone_worker_tts_cache_module().read_bytes(path, optional=True)
        if data is None:
            return None
        read_ms = (time.monotonic() - read_started) * 1000.0
        if not data:
            return None
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"cache TTS grande demais: {len(data)} bytes")
        self._touch_tts_cache_file(path)
        total_ms = (time.monotonic() - started) * 1000.0
        digest = hashlib.sha256(data).hexdigest()
        logs.append(f"standard-cache hit engine={engine} file={path.name} read={read_ms:.1f}ms")
        timing_ms = dict(stage_ms)
        timing_ms.update({"cache_read": round(read_ms, 2), "worker_total": round(total_ms, 2), "worker_synth": 0.0})
        result = {
            "ok": True,
            "engine": engine,
            "selected_engine": engine,
            "audio_format": audio_format,
            "cache_hit": True,
            "cache_key": key[:16],
            "cache_file": path.name,
            "cache_read_ms": round(read_ms, 2),
            "timing_ms": timing_ms,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "available_engines": list(available_engines) if available_engines is not None else _tts_agent_available_engines(),
            "worker_synth_ms": 0.0,
            "worker_total_ms": round(total_ms, 2),
            "total_ms": round(total_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
        }
        if engine == "teto":
            controls = ttv_settings or _phone_worker_tts_policy_module().effective_ttv_settings(
                {"teto_pitch_semitones": teto_pitch_semitones},
                default_speech_rate=os.getenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.0"))
            status = _ttv_voice_state(_teto_status(), controls["ttv_voice_id"])
            result.update({
                "teto_backend": status.get("backend") or ("utau" if controls["ttv_voice_id"] == "hatsune-miku" else _teto_backend()),
                "teto_renderer_version": status.get("renderer_version", ""),
                "teto_voicebank_profile": "english-cvvc" if (
                    controls["ttv_language"] == "en-US" or controls.get("ttv_mode") == "singing"
                ) else status.get("voicebank_profile", ""),
                "teto_pitch_mode": "utau-semitones",
                "teto_pitch_offset_semitones": controls["ttv_pitch_semitones"],
                "teto_speech_rate": controls["ttv_speech_rate"],
                **controls,
            })
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _store_tts_agent_standard_cache(self, *, key: str, data: bytes, audio_format: str, logs: list[str]) -> None:
        if not key or not data:
            return
        path = self._tts_cache_path(key, audio_format)
        try:
            _phone_worker_tts_cache_module().publish_bytes(path, data,
                os_api=os, thread_id=threading.get_ident, create_parent=True, cleanup_errors=Exception)
            self._touch_tts_cache_file(path)
            self._schedule_tts_cache_prune(protected=path)
            logs.append(f"standard-cache store {path.name} {len(data)}B")
        except Exception as exc:
            logs.append(f"standard-cache store falhou: {type(exc).__name__}: {_short_text(exc, limit=90)}")

    def _synthesize_standard_tts_bytes(self, body: dict[str, Any], *, engine: str, roles: list[str], capabilities: list[str], logs: list[str], started: float, max_audio_bytes: int, timeout: int, available_engines: list[str] | None = None, raw_response: bool = False) -> dict[str, Any]:
        normalized = str(engine or "gtts").strip().lower().replace("-", "_") or "gtts"
        engine = {"google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts", "kasane_teto": "teto", "teto_utau": "teto", "utau": "teto"}.get(normalized, normalized)
        text = str(body.get("text") or "").strip()
        performance = _phone_worker_tts_policy_module().performance_request(body, engine=engine)
        if not text and performance.get("score") is None:
            raise ValueError("texto vazio")
        ttv_settings = _ttv_request_settings(body) if engine == "teto" else {}
        if performance:
            ttv_settings.update({"ttv_performance_hash": performance["hash"],
                                 "ttv_performance_protocol": 2 if isinstance(performance["score"], dict) and "preset" in performance["score"] else 1,
                                 "ttv_mode": "singing" if performance["score"] is not None else "effects"})
        stage_ms: dict[str, float] = {}
        cache_key = ""
        cache_enabled = self._tts_agent_standard_cache_enabled(roles, capabilities)
        cache_read_allowed = cache_enabled and self._tts_agent_cache_mode_allows_read(body)
        cache_store_allowed = cache_enabled and self._tts_agent_cache_mode_allows_store(body)
        cache_started = None
        if cache_read_allowed or cache_store_allowed:
            key_started = time.monotonic()
            cache_started = key_started
            with contextlib.suppress(Exception):
                cache_key = self._tts_agent_standard_cache_key(body, engine=engine)
            stage_ms["cache_key"] = round((time.monotonic() - key_started) * 1000.0, 2)
            if cache_key and cache_read_allowed:
                hit = self._tts_agent_standard_cache_hit(
                    key=cache_key,
                    engine=engine,
                    roles=roles,
                    capabilities=capabilities,
                    logs=logs,
                    started=started,
                    max_audio_bytes=max_audio_bytes,
                    stage_ms=stage_ms,
                    available_engines=available_engines,
                    raw_response=raw_response,
                    teto_pitch_semitones=body.get("teto_pitch_semitones", 0.0),
                    ttv_settings=ttv_settings,
                )
                if hit is not None:
                    if engine == "teto" and time.monotonic() - cache_started >= float(timeout):
                        raise TimeoutError("prazo total do TTS Agent esgotado")
                    return hit
        audio_format = "mp3"
        data = b""
        response: dict[str, Any] = {}
        teto_meta: dict[str, Any] = {}
        synth_started = None if engine == "teto" else time.monotonic()
        if engine == "teto":
            remaining = float(timeout)
            if cache_started is not None:
                remaining -= time.monotonic() - cache_started
            if remaining <= 0:
                raise TimeoutError("prazo total do TTS Agent esgotado")
            data, audio_format, teto_meta = _phone_worker_tts_providers_module().synthesize_teto(
                text=text, timeout=remaining, max_audio_bytes=max_audio_bytes, logs=logs, stage_ms=stage_ms,
                heavy_lock=_HEAVY_RESOURCE_LOCK,
                background_state=_TETO_BACKGROUND_STATE,
                priority="background" if body.get("priority") == "background" else "foreground",
                get_renderer=lambda: _get_ttv_renderer(ttv_settings["ttv_voice_id"]),
                monotonic=time.monotonic, normalize_format=self._normalize_tts_cache_format,
                pitch_offset_semitones=ttv_settings["ttv_pitch_semitones"],
                speech_rate=ttv_settings["ttv_speech_rate"] if "ttv_speech_rate" in body or performance else None,
                **({"reading_language": ttv_settings["ttv_language"]} if "ttv_language" in body or performance else {}),
                **({"score": performance["score"], "segments": performance["segments"],
                    "performance_hash": performance["hash"]} if performance else {}))
        elif engine == "android_native":
            data, audio_format, response = _phone_worker_tts_android_module().synthesize(
                body, text=text, timeout=timeout, max_audio_bytes=max_audio_bytes, logs=logs, stage_ms=stage_ms,
                getenv=os.getenv, env_bool=_env_bool, monotonic=time.monotonic,
                raw_request=_android_tts_raw_request, json_request=_android_tts_json_request,
                short_text=_short_text, decode_audio=_b64decode, normalize_format=self._normalize_tts_cache_format)
        elif engine in {"edge", "gtts"} and _tts_transport_module() is not None:
            data = _tts_transport_module().synthesize_bytes(engine=engine, text=text,
                voice=str(body.get("voice") or body.get("fallback_voice") or "pt-BR-FranciscaNeural"),
                language=self._normalize_tts_gtts_language(body.get("language") or body.get("fallback_language")),
                rate=self._normalize_tts_edge_rate(body.get("rate")),
                pitch=self._normalize_tts_edge_pitch(body.get("pitch")),
                tld=str(body.get("tld") or "com"), timeout=timeout, max_bytes=max_audio_bytes)
            logs.append(f"{engine} transporte compartilhado")
        elif engine == "edge":
            data = _phone_worker_tts_providers_module().synthesize_edge(
                body, text=text, timeout=timeout, logs=logs,
                normalize_rate=self._normalize_tts_edge_rate, normalize_pitch=self._normalize_tts_edge_pitch,
                short_text=_short_text, asyncio_api=asyncio, io_api=io)
        else:
            data = _phone_worker_tts_providers_module().synthesize_gtts(
                body, text=text, timeout=timeout, logs=logs,
                normalize_language=self._normalize_tts_gtts_language, short_text=_short_text, io_api=io)
        if not data:
            raise RuntimeError("engine não gerou áudio")
        if engine == "teto":
            source_voice = teto_meta.get("voice_id", teto_meta.get("ttv_voice_id"))
            if (performance or source_voice is not None) and source_voice != ttv_settings["ttv_voice_id"]:
                raise RuntimeError("renderer retornou áudio de outra vocaloid TTV")
            if performance and (
                teto_meta.get("pitch_offset_semitones") != ttv_settings["ttv_pitch_semitones"] or
                teto_meta.get("speech_rate") != ttv_settings["ttv_speech_rate"] or
                teto_meta.get("reading_language") != ttv_settings["ttv_language"]
            ):
                raise RuntimeError("renderer retornou performance com ajustes TTV diferentes")
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"áudio grande demais: {len(data)} bytes")
        if engine == "teto" and stage_ms.get("teto_render") is not None:
            synth_ms = float(stage_ms.get("teto_render") or 0.0)
        else:
            if synth_started is None:
                raise RuntimeError("cronômetro da engine TTS não inicializado")
            synth_ms = (time.monotonic() - synth_started) * 1000.0
        stage_ms["worker_synth"] = round(synth_ms, 2)
        if cache_key and cache_store_allowed:
            store_started = time.monotonic()
            self._store_tts_agent_standard_cache(key=cache_key, data=data, audio_format=audio_format, logs=logs)
            stage_ms["cache_store"] = round((time.monotonic() - store_started) * 1000.0, 2)
        total_ms = (time.monotonic() - started) * 1000.0
        stage_ms["worker_total"] = round(total_ms, 2)
        digest = hashlib.sha256(data).hexdigest()
        result = {
            "ok": True,
            "engine": engine,
            "selected_engine": engine,
            "audio_format": audio_format,
            "cache_hit": False,
            "cache_key": cache_key[:16] if cache_key else "",
            "timing_ms": stage_ms,
            "android_synth_ms": response.get("android_synth_ms") if engine == "android_native" and isinstance(response, dict) else None,
            "android_voice": response.get("voice") if engine == "android_native" and isinstance(response, dict) else "",
            "android_locale": response.get("locale") if engine == "android_native" and isinstance(response, dict) else "",
            "teto_voicebank": teto_meta.get("voicebank") if engine == "teto" else "",
            "teto_backend": (teto_meta.get("backend") or ("utau" if ttv_settings["ttv_voice_id"] == "hatsune-miku" else _teto_backend())) if engine == "teto" else "",
            "teto_pitch_mode": "utau-semitones" if engine == "teto" else "",
            "teto_fingerprint": teto_meta.get("voicebank_fingerprint") if engine == "teto" else "",
            "teto_renderer_version": teto_meta.get("renderer_version") if engine == "teto" else "",
            "teto_phonemizer_version": teto_meta.get("phonemizer_version") if engine == "teto" else "",
            "teto_rendered_phonemes": teto_meta.get("rendered_phonemes") if engine == "teto" else None,
            "teto_missing_phonemes": teto_meta.get("missing_phonemes") if engine == "teto" else [],
            "teto_phonetic_units": teto_meta.get("phonetic_units") if engine == "teto" else None,
            "teto_auxiliary_phonemes": teto_meta.get("auxiliary_phonemes") if engine == "teto" else None,
            "teto_epenthetic_phonemes": teto_meta.get("epenthetic_phonemes") if engine == "teto" else None,
            "teto_voicebank_profile": teto_meta.get("voicebank_profile") if engine == "teto" else "",
            "teto_cvvc_direct": teto_meta.get("cvvc_direct") if engine == "teto" else None,
            "teto_cvvc_transitions": teto_meta.get("cvvc_transitions") if engine == "teto" else None,
            "teto_cluster_hits": teto_meta.get("cluster_hits") if engine == "teto" else None,
            "teto_approximated_phonemes": teto_meta.get("approximated_phonemes") if engine == "teto" else None,
            "teto_coverage_percent": teto_meta.get("coverage_percent") if engine == "teto" else None,
            "teto_timeline_mode": teto_meta.get("timeline_mode") if engine == "teto" else "",
            "teto_timeline_aux_overlays": teto_meta.get("timeline_aux_overlays") if engine == "teto" else None,
            "teto_timeline_audio_ms": teto_meta.get("timeline_audio_ms") if engine == "teto" else None,
            "teto_pitch_offset_semitones": teto_meta.get("pitch_offset_semitones") if engine == "teto" else None,
            "teto_speech_rate": teto_meta.get("speech_rate", ttv_settings.get("ttv_speech_rate")) if engine == "teto" else None,
            "teto_alias_path_cost": teto_meta.get("alias_path_cost") if engine == "teto" else None,
            "teto_mean_nucleus_ms": teto_meta.get("mean_nucleus_ms") if engine == "teto" else None,
            "teto_nucleus_duration_stddev_ms": teto_meta.get("nucleus_duration_stddev_ms") if engine == "teto" else None,
            "teto_pitch_boundary_max_cents": teto_meta.get("pitch_boundary_max_cents") if engine == "teto" else None,
            "teto_continuity_repairs": teto_meta.get("continuity_repairs") if engine == "teto" else None,
            "teto_energy_boundary_max_db": teto_meta.get("energy_boundary_max_db") if engine == "teto" else None,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "available_engines": list(available_engines) if available_engines is not None else _tts_agent_available_engines(),
            "worker_synth_ms": round(synth_ms, 2),
            "worker_total_ms": round(total_ms, 2),
            "total_ms": round(total_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
        }
        if engine == "teto":
            result.update(ttv_settings)
            result.update(_phone_worker_tts_providers_module().render_telemetry(teto_meta))
            result["ttv_speech_rate"] = result["teto_speech_rate"]
            result["ttv_pitch_semitones"] = teto_meta.get("pitch_offset_semitones", ttv_settings["ttv_pitch_semitones"])
            result["ttv_language"] = teto_meta.get("reading_language", ttv_settings["ttv_language"])
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _task_tts_agent_synthesize(self, body: dict[str, Any], *, raw_response: bool = False) -> dict[str, Any]:
        request_started = time.monotonic()
        roles, capabilities = self._ensure_tts_piper_turbo_allowed()
        if not _env_bool("PHONE_WORKER_TTS_AGENT_ENABLED", True):
            raise RuntimeError("PHONE_WORKER_TTS_AGENT_ENABLED=false")
        text = str(body.get("text") or "").strip()
        requested_engine = _tts_agent_normalize_engine(body.get("engine"))
        performance = _phone_worker_tts_policy_module().performance_request(body, engine=requested_engine)
        background = body.get("priority") == "background"
        if background and (requested_engine != "teto" or performance.get("score") is None):
            raise ValueError("aquecimento de fundo exige uma partitura TTV")
        if not text and performance.get("score") is None:
            raise ValueError("texto vazio")
        max_chars = 2048 if performance.get("score") is not None else max(64, _env_int("PHONE_WORKER_TTS_AGENT_MAX_TEXT_LENGTH", 1200))
        if len(text) > max_chars:
            raise ValueError(f"texto grande demais para TTS Agent ({len(text)} > {max_chars})")
        if requested_engine == "teto":
            controls = _ttv_request_settings(body)
            if performance.get("score") is not None and controls["ttv_voice_id"] == "kasane-teto" and _teto_backend() == "worldline-r":
                raise ValueError("canto MIDI exige ESPER; selecione o backend utau/ESPER da Teto")
            prefix = "MIKU" if controls["ttv_voice_id"] == "hatsune-miku" else "TETO"
            teto_max_chars = 2048 if performance.get("score") is not None else max(16, _env_int(f"PHONE_WORKER_{prefix}_MAX_CHARACTERS", 180))
            if len(text) > teto_max_chars:
                raise ValueError(f"texto grande demais para Teto ({len(text)} > {teto_max_chars})")
        timeout_limit = 120 if performance else self.job_timeout
        timeout = max(2, min(timeout_limit, int(float(body.get("timeout_seconds") or
            (120 if performance else os.getenv("PHONE_WORKER_TTS_AGENT_TIMEOUT_SECONDS") or self.job_timeout)))))
        max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
        teto_request = requested_engine == "teto"
        dependency_started = time.monotonic()
        deps = _teto_dependency_snapshot_fast() if teto_request else _turbo_dependency_snapshot()
        if teto_request:
            teto_state = _ttv_voice_state(deps.get("teto") or {}, controls["ttv_voice_id"])
            # Aggregate readiness keeps TTV visible when either character is
            # installed, but admission must always check the requested voice.
            voice_ready = bool(teto_state.get("ready") and (teto_state.get("resources") or {}).get("ok", True))
            if not voice_ready:
                reason = teto_state.get("last_error") or (teto_state.get("resources") or {}).get("reason") or "voicebank ou resampler indisponível"
                name = "Miku" if controls["ttv_voice_id"] == "hatsune-miku" else "Teto"
                raise RuntimeError(f"{name} indisponível: {_short_text(reason, limit=180)}")
            required_protocol = 2 if isinstance(performance.get("score"), dict) and "preset" in performance["score"] else 1
            if performance and isinstance((deps.get("teto") or {}).get("ttv_voices"), list) and int(teto_state.get("ttv_performance_protocol") or 0) < required_protocol:
                raise RuntimeError("vocaloid sem suporte MIDI/efeitos TTV; use ESPER e atualize o runtime")
        if teto_request and not deps.get("teto_tts"):
            reason = teto_state.get("last_error") or (teto_state.get("resources") or {}).get("reason") or "voicebank ou resampler indisponível"
            raise RuntimeError(f"Teto indisponível: {_short_text(reason, limit=180)}")
        dependency_ms = (time.monotonic() - dependency_started) * 1000.0
        available = _tts_agent_available_engines(deps)
        order_started = time.monotonic()
        order = ["teto"] if teto_request else self._tts_agent_engine_order(body, available)
        engine_order_ms = (time.monotonic() - order_started) * 1000.0
        if not order:
            raise RuntimeError("nenhuma engine TTS pronta no worker")
        preflight_ms = (time.monotonic() - request_started) * 1000.0
        base_logs = [
            f"perfil={_current_core_worker_profile()} versão={PHONE_WORKER_VERSION}",
            f"tts-agent chars={len(text)} order={','.join(order)} timeout={timeout}s",
        ]
        errors: list[str] = []
        accounting = {"background": True} if background else {}
        _tts_agent_record_start(**accounting)
        started = request_started if teto_request else time.monotonic()
        selected = ""

        def finalize_timing(result: dict[str, Any]) -> dict[str, Any]:
            total_ms = (time.monotonic() - request_started) * 1000.0
            timing = result.get("timing_ms") if isinstance(result.get("timing_ms"), dict) else {}
            timing = dict(timing)
            timing["preflight"] = round(preflight_ms, 2)
            timing["dependency_snapshot"] = round(dependency_ms, 2)
            timing["engine_order"] = round(engine_order_ms, 2)
            timing["worker_total"] = round(total_ms, 2)
            result["timing_ms"] = timing
            result["worker_total_ms"] = round(total_ms, 2)
            result["total_ms"] = round(total_ms, 2)
            return result

        try:
            for engine in order:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("prazo total do TTS Agent esgotado")
                selected = engine
                try:
                    if engine == "piper":
                        piper_body = dict(body)
                        piper_body["engine"] = "piper"
                        piper_body["timeout_seconds"] = max(1, int(remaining))
                        piper_body.setdefault("cache_mode", "prefer")
                        result = self._synthesize_piper_bytes(piper_body, benchmark=False, raw_response=raw_response)
                        result["engine"] = "piper"
                        result["selected_engine"] = "piper"
                        result["worker_id"] = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip()
                        result["available_engines"] = available
                        result["logs"] = (base_logs + list(result.get("logs") or []))[:10]
                        elapsed_ms = (time.monotonic() - started) * 1000.0
                        finalize_timing(result)
                        _tts_agent_record_done(ok=True, engine="piper", elapsed_ms=elapsed_ms, **accounting)
                        return result
                    engine_body = dict(body)
                    if engine != requested_engine:
                        engine_body['engine'] = engine
                        engine_body.pop('cache_key', None)
                        for setting in ('voice', 'language', 'rate', 'pitch'):
                            fallback_value = body.get('fallback_' + setting)
                            if fallback_value not in (None, ''):
                                engine_body[setting] = fallback_value
                    result = self._synthesize_standard_tts_bytes(
                        engine_body,
                        engine=engine,
                        roles=roles,
                        capabilities=capabilities,
                        logs=list(base_logs),
                        started=started,
                        max_audio_bytes=max_audio_bytes,
                        timeout=remaining,
                        available_engines=available,
                        raw_response=raw_response,
                    )
                    if errors:
                        result["fallback_errors"] = errors[:4]
                        if engine != requested_engine:
                            result["fallback_from_engine"] = requested_engine
                        existing_logs = list(result.get("logs") or [])
                        existing_logs.extend(f"fallback_before={item}" for item in errors[:2])
                        result["logs"] = existing_logs[:10]
                    finalize_timing(result)
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    _tts_agent_record_done(ok=True, engine=engine, elapsed_ms=elapsed_ms, **accounting)
                    return result
                except Exception as exc:
                    failure = f"{engine}: {type(exc).__name__}: {_short_text(exc, limit=140)}"
                    errors.append(failure)
                    if teto_request:
                        raise
                    if engine == "teto":
                        print(f"[phone-worker-tts] Teto falhou antes do fallback: {failure}", flush=True)
                    continue
            raise RuntimeError("; ".join(errors) or "todas as engines falharam")
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            _tts_agent_record_done(ok=False, engine=selected, elapsed_ms=elapsed_ms, error=str(exc), **accounting)
            raise

    def _resolve_piper_command(self) -> str:
        configured = str(os.getenv("PHONE_WORKER_PIPER_COMMAND", "") or "").strip()
        if configured:
            return configured
        found = shutil.which("piper")
        if found:
            return found
        raise RuntimeError("piper não encontrado; configure PHONE_WORKER_PIPER_COMMAND ou instale o binário no worker turbo")

    def _resolve_piper_model(self, body: dict[str, Any]) -> tuple[str, str]:
        model = str(body.get("model") or body.get("model_path") or os.getenv("PHONE_WORKER_PIPER_MODEL", "") or "").strip()
        config = str(body.get("config") or body.get("config_path") or os.getenv("PHONE_WORKER_PIPER_CONFIG", "") or "").strip()
        model_name = str(body.get("model_name") or os.getenv("PHONE_WORKER_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
        if not model:
            # Atalho opcional para manter vários modelos por nome sem mexer no bot.
            env_key = re.sub(r"[^A-Z0-9]+", "_", model_name.upper()).strip("_")
            if env_key:
                model = str(os.getenv(f"PHONE_WORKER_PIPER_MODEL_{env_key}", "") or "").strip()
                config = config or str(os.getenv(f"PHONE_WORKER_PIPER_CONFIG_{env_key}", "") or "").strip()
        if not model:
            raise RuntimeError("Piper sem modelo; configure PHONE_WORKER_PIPER_MODEL=/caminho/voz.onnx no worker turbo")
        model_path = Path(model).expanduser()
        if not model_path.exists() or model_path.stat().st_size <= 0:
            raise RuntimeError(f"modelo Piper não encontrado ou vazio: {model_path}")
        if not config:
            candidate = Path(str(model_path) + ".json")
            if candidate.exists():
                config = str(candidate)
        if config:
            config_path = Path(config).expanduser()
            if not config_path.exists() or config_path.stat().st_size <= 0:
                raise RuntimeError(f"config Piper não encontrado ou vazio: {config_path}")
            config = str(config_path)
        return str(model_path), config

    def _piper_cache_enabled(self, body: dict[str, Any]) -> bool:
        raw = body.get("cache_enabled", os.getenv("PHONE_WORKER_PIPER_CACHE_ENABLED", "true"))
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

    def _piper_cache_dir(self) -> Path:
        configured = str(os.getenv("PHONE_WORKER_PIPER_CACHE_DIR", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "phone-worker" / "cache" / "piper"

    def _piper_cache_limits(self) -> tuple[int, int]:
        def _as_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(float(os.getenv(name, str(default)) or default))
            except Exception:
                value = default
            return max(minimum, min(maximum, value))
        max_mb = _as_int("PHONE_WORKER_PIPER_CACHE_MAX_MB", 2048, 32, 16384)
        max_files = _as_int("PHONE_WORKER_PIPER_CACHE_MAX_FILES", 4096, 32, 50000)
        return max_mb * 1024 * 1024, max_files

    def _normalize_piper_cache_text(self, text: str) -> str:
        return " ".join(str(text or "").strip().split()).lower()

    def _piper_cache_key(self, *, text: str, model_path: str, config_path: str, model_name: str, audio_format_hint: str) -> str:
        model = Path(model_path)
        try:
            stat = model.stat()
            model_sig = f"{model.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
        except Exception:
            model_sig = str(model_path)
        bitrate = str(os.getenv("PHONE_WORKER_PIPER_MP3_BITRATE", "96k") or "96k")
        payload = "|".join([
            "piper-worker-cache-v2",
            PHONE_WORKER_VERSION,
            str(model_name or "turbo-default"),
            model_sig,
            str(config_path or ""),
            str(audio_format_hint or "auto"),
            bitrate,
            self._normalize_piper_cache_text(text),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _find_piper_cache_file(self, key: str) -> tuple[Path | None, str]:
        cache_dir = self._piper_cache_dir()
        for fmt, suffix in (("mp3", ".mp3"), ("wav", ".wav")):
            path = cache_dir / f"{key}{suffix}"
            if path.exists() and path.stat().st_size > 0:
                return path, fmt
        return None, ""

    def _touch_piper_cache_file(self, path: Path) -> None:
        now = time.time()
        with contextlib.suppress(Exception):
            os.utime(path, (now, now))

    def _prune_piper_cache(self, *, protected: Path | None = None) -> None:
        _prune_audio_cache(self._piper_cache_dir(), *self._piper_cache_limits(), protected=protected)

    def _piper_cache_hit_result(self, *, path: Path, audio_format: str, key: str, roles: list[str], capabilities: list[str], logs: list[str], max_audio_bytes: int, started: float, cache_mode: str = "prefer", raw_response: bool = False) -> dict[str, Any]:
        read_started = time.monotonic()
        data = path.read_bytes()
        read_ms = (time.monotonic() - read_started) * 1000.0
        if not data:
            raise RuntimeError("cache Piper vazio")
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"cache Piper grande demais: {len(data)} bytes")
        self._touch_piper_cache_file(path)
        digest = hashlib.sha256(data).hexdigest()
        total_ms = (time.monotonic() - started) * 1000.0
        logs.append(f"cache hit Piper {path.name} {len(data)} B em {read_ms:.1f} ms")
        result = {
            "ok": True,
            "engine": "piper",
            "audio_format": audio_format,
            "cache_hit": True,
            "cache_exists_before": True,
            "cache_mode": cache_mode,
            "cache_key": key[:16],
            "cache_file": path.name,
            "cache_read_ms": round(read_ms, 2),
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": 0.0,
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
            "worker_total_ms": round(total_ms, 2),
        }
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _piper_cache_miss_result(self, *, key: str, roles: list[str], capabilities: list[str], logs: list[str], started: float, cache_mode: str) -> dict[str, Any]:
        total_ms = (time.monotonic() - started) * 1000.0
        logs.append(f"cache only Piper miss key={key[:16]}")
        return {
            "ok": False,
            "engine": "piper",
            "cache_hit": False,
            "cache_exists_before": False,
            "cache_mode": cache_mode,
            "cache_key": key[:16],
            "error": "cache Piper miss",
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": 0.0,
            "size": 0,
            "logs": logs[:10],
            "worker_total_ms": round(total_ms, 2),
        }

    def _synthesize_piper_bytes(self, body: dict[str, Any], *, benchmark: bool, raw_response: bool = False) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_piper_turbo_allowed()
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("texto vazio")
        if len(text) > 1600:
            raise ValueError("texto grande demais para Piper experimental")
        timeout = max(2, min(self.job_timeout, int(float(body.get("timeout_seconds") or self.job_timeout))))
        max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
        model_path, config_path = self._resolve_piper_model(body)
        model_name = str(body.get("model_name") or os.getenv("PHONE_WORKER_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
        cache_mode = str(body.get("cache_mode") or "prefer").strip().lower().replace("-", "_")
        cache_enabled = self._piper_cache_enabled(body)
        cache_only_modes = {"only", "cache_only", "hit", "hit_only", "read", "read_only"}
        cache_lookup_modes = cache_only_modes | {"prefer", "preferred", "auto"}
        logs: list[str] = [
            f"perfil={_current_core_worker_profile()} versão={PHONE_WORKER_VERSION}",
            f"engine=piper chars={len(text)} timeout={timeout}s modelo={model_name} cache_mode={cache_mode}",
        ]
        started = time.monotonic()
        cache_key = self._piper_cache_key(text=text, model_path=model_path, config_path=config_path, model_name=model_name, audio_format_hint="auto")
        logs.append(f"cache key Piper {cache_key[:16]}")
        if cache_enabled and cache_mode in cache_lookup_modes:
            cached_path, cached_format = self._find_piper_cache_file(cache_key)
            if cached_path is not None:
                return self._piper_cache_hit_result(
                    path=cached_path,
                    audio_format=cached_format,
                    key=cache_key,
                    roles=roles,
                    capabilities=capabilities,
                    logs=logs,
                    max_audio_bytes=max_audio_bytes,
                    started=started,
                    cache_mode=cache_mode,
                    raw_response=raw_response,
                )
            logs.append("cache miss Piper")
            if cache_mode in cache_only_modes:
                return self._piper_cache_miss_result(
                    key=cache_key,
                    roles=roles,
                    capabilities=capabilities,
                    logs=logs,
                    started=started,
                    cache_mode=cache_mode,
                )
        elif cache_enabled:
            logs.append(f"cache Piper ignorado por modo={cache_mode}")
        else:
            logs.append("cache Piper desativado")

        piper_cmd = self._resolve_piper_command()
        with tempfile.TemporaryDirectory(prefix="phone-worker-piper-") as tmp:
            tmp_dir = Path(tmp)
            wav_path = tmp_dir / "speech.wav"
            mp3_path = tmp_dir / "speech.mp3"
            cmd = [piper_cmd, "--model", model_path, "--output_file", str(wav_path)]
            if config_path:
                cmd.extend(["--config", config_path])
            extra_args = shlex.split(str(os.getenv("PHONE_WORKER_PIPER_EXTRA_ARGS", "") or ""))
            if extra_args:
                cmd.extend(extra_args)
            piper_started = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd,
                    input=(text + "\n").encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Piper timeout após {timeout}s") from exc
            piper_ms = (time.monotonic() - piper_started) * 1000.0
            stderr = _short_text((proc.stderr or b"").decode("utf-8", errors="ignore"), limit=220)
            if proc.returncode != 0:
                raise RuntimeError(f"Piper saiu com código {proc.returncode}: {stderr or 'sem stderr'}")
            if not wav_path.exists() or wav_path.stat().st_size <= 0:
                raise RuntimeError(f"Piper não gerou WAV válido: {stderr or 'sem stderr'}")
            logs.append(f"piper wav {wav_path.stat().st_size} B em {piper_ms:.1f} ms")

            output_path = wav_path
            audio_format = "wav"
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                conv_started = time.monotonic()
                conv_cmd = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav_path),
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    str(os.getenv("PHONE_WORKER_PIPER_MP3_BITRATE", "96k") or "96k"),
                    str(mp3_path),
                ]
                conv = subprocess.run(conv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(2, min(timeout, 8)), check=False)
                conv_ms = (time.monotonic() - conv_started) * 1000.0
                if conv.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 0:
                    output_path = mp3_path
                    audio_format = "mp3"
                    logs.append(f"ffmpeg mp3 {mp3_path.stat().st_size} B em {conv_ms:.1f} ms")
                else:
                    conv_err = _short_text((conv.stderr or b"").decode("utf-8", errors="ignore"), limit=160)
                    logs.append(f"ffmpeg mp3 indisponível; usando wav: {conv_err or 'sem stderr'}")
            else:
                logs.append("ffmpeg não encontrado; retornando wav")

            data = output_path.read_bytes()
            if not data:
                raise RuntimeError("Piper retornou áudio vazio")
            if len(data) > max_audio_bytes:
                raise RuntimeError(f"áudio grande demais: {len(data)} bytes")
        synth_ms = (time.monotonic() - started) * 1000.0
        digest = hashlib.sha256(data).hexdigest()
        cache_stored = False
        if cache_enabled and cache_mode not in {"bypass"}:
            try:
                cache_dir = self._piper_cache_dir()
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path = cache_dir / f"{cache_key}.{audio_format}"
                tmp_cache_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
                tmp_cache_path.write_bytes(data)
                os.replace(tmp_cache_path, cache_path)
                self._touch_piper_cache_file(cache_path)
                self._schedule_tts_cache_prune(protected=cache_path, piper=True)
                cache_stored = True
                logs.append(f"cache store Piper {cache_path.name} {len(data)} B")
            except Exception as exc:
                logs.append(f"cache store Piper falhou: {_short_text(exc, limit=120)}")
        logs.append(f"worker Piper gerou {len(data)} bytes em {synth_ms:.1f} ms")
        result = {
            "ok": True,
            "engine": "piper",
            "audio_format": audio_format,
            "cache_hit": False,
            "cache_exists_before": False,
            "cache_mode": cache_mode,
            "cache_key": cache_key[:16],
            "cache_file": f"{cache_key}.{audio_format}" if cache_stored else "",
            "cache_stored": cache_stored,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": round(synth_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
            "worker_total_ms": round(synth_ms, 2),
        }
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _task_tts_synthesize_piper(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._synthesize_piper_bytes(body, benchmark=False)

    def _task_tts_synthesize_benchmark(self, body: dict[str, Any]) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_benchmark_turbo_allowed()
        engine = str(body.get("engine") or "gtts").strip().lower().replace("-", "_")
        if engine not in {"android_native", "edge", "gtts"}:
            raise ValueError("engine inválida para benchmark TTS")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("texto vazio")
        if len(text) > 1200:
            raise ValueError("texto grande demais para benchmark")
        timeout = max(2, min(self.job_timeout, int(float(body.get("timeout_seconds") or self.job_timeout))))
        max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
        logs: list[str] = [
            f"perfil={_current_core_worker_profile()} versão={PHONE_WORKER_VERSION}",
            f"engine={engine} chars={len(text)} timeout={timeout}s",
        ]
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="phone-worker-tts-bench-") as tmp:
            tmp_dir = Path(tmp)
            audio_format = "mp3"
            out_path = tmp_dir / "speech.mp3"
            try:
                if engine == "android_native":
                    # Benchmark measures the single requested engine. Fallback
                    # substitution belongs to the agent's multi-engine path.
                    engine_body = dict(body)
                    result = self._synthesize_standard_tts_bytes(
                        engine_body,
                        engine="android_native",
                        roles=roles,
                        capabilities=capabilities,
                        logs=list(logs),
                        started=started,
                        max_audio_bytes=max_audio_bytes,
                        timeout=timeout,
                    )
                    result["engine"] = "android_native"
                    result["logs"] = (logs + list(result.get("logs") or []))[:10]
                    return result
                if engine == "edge":
                    try:
                        import edge_tts  # type: ignore
                    except Exception as exc:
                        raise RuntimeError(f"edge-tts não instalado no worker: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc
                    voice = str(body.get("voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
                    rate = self._normalize_tts_edge_rate(body.get("rate"))
                    pitch = self._normalize_tts_edge_pitch(body.get("pitch"))

                    async def _save_edge() -> None:
                        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
                        await communicate.save(str(out_path))

                    asyncio.run(asyncio.wait_for(_save_edge(), timeout=timeout))
                    logs.append(f"edge voice={voice} rate={rate} pitch={pitch}")
                elif engine == "piper":
                    result = self._synthesize_piper_bytes(body, benchmark=True)
                    result["engine"] = "piper"
                    result["logs"] = (logs + list(result.get("logs") or []))[:10]
                    return result
                else:
                    try:
                        from gtts import gTTS  # type: ignore
                    except Exception as exc:
                        raise RuntimeError(f"gTTS não instalado no worker: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc
                    language = self._normalize_tts_gtts_language(body.get("language"))
                    tts = gTTS(text=text, lang=language, timeout=(min(3.5, timeout), min(8.0, timeout)))
                    with open(out_path, "wb") as handle:
                        tts.write_to_fp(handle)
                    logs.append(f"gtts language={language}")
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"{engine} timeout após {timeout}s no worker") from exc
            synth_ms = (time.monotonic() - started) * 1000.0
            if not out_path.exists() or out_path.stat().st_size <= 0:
                raise RuntimeError("engine não gerou arquivo de áudio")
            data = out_path.read_bytes()
            if len(data) > max_audio_bytes:
                raise RuntimeError(f"áudio grande demais: {len(data)} bytes")
        digest = hashlib.sha256(data).hexdigest()
        logs.append(f"worker gerou {len(data)} bytes em {synth_ms:.1f} ms")
        return {
            "ok": True,
            "engine": engine,
            "audio_format": audio_format,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": round(synth_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:8],
            "data_b64": _b64encode(data, max_bytes=max_audio_bytes),
        }

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
        reclaimable = reclaimable_temp + reclaimable_logs
        recommendations: list[str] = []
        if old_temp:
            recommendations.append(f"limpar {len(old_temp)} cache(s)/temporário(s) antigos com cerca de {_format_bytes(reclaimable_temp)}")
        if old_logs:
            recommendations.append(f"arquivar ou remover {len(old_logs)} log(s) antigos com cerca de {_format_bytes(reclaimable_logs)}")
        if not recommendations:
            recommendations.append("nenhuma limpeza automática necessária agora")
        summary = f"{scanned} arquivo(s) analisados; {_format_bytes(reclaimable)} recuperável estimado; nada foi apagado"
        return {
            "ok": True,
            "summary": summary,
            "safe": True,
            "note": "Plano apenas sugere limpeza; o worker não remove arquivos automaticamente.",
            "scanned": scanned,
            "total_size": total_size,
            "by_kind": by_kind,
            "largest": largest[:30],
            "old_temp_candidates": old_temp[:80],
            "old_log_candidates": old_logs[:80],
            "estimated_reclaimable": reclaimable,
            "estimated_reclaimable_temp": reclaimable_temp,
            "estimated_reclaimable_logs": reclaimable_logs,
            "recommendations": recommendations[:12],
        }

    def _emoji_cdn_url(self, emoji: dict[str, Any]) -> str:
        emoji_id = str(emoji.get("id") or "")
        ext = "gif" if bool(emoji.get("animated")) else "png"
        return f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=128&quality=lossless"

    def _download_emoji_asset(self, emoji: dict[str, Any], *, limit: int = 900_000) -> bytes:
        req = urllib.request.Request(self._emoji_cdn_url(emoji), headers={"User-Agent": "CorePhoneWorker/emoji-recolor"})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError("emoji base grande demais")
        return data

    def _rgb_from_hex_worker(self, value: Any) -> tuple[int, int, int]:
        raw = str(value or "#5865F2").strip().upper()
        if not raw.startswith("#"):
            raw = "#" + raw
        if not re.fullmatch(r"#[0-9A-F]{6}", raw):
            raw = "#5865F2"
        return int(raw[1:3], 16), int(raw[3:5], 16), int(raw[5:7], 16)

    def _mix_rgb_worker(self, first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        amount = max(0.0, min(1.0, float(amount)))
        return (
            max(0, min(255, int(round(first[0] * (1.0 - amount) + second[0] * amount)))),
            max(0, min(255, int(round(first[1] * (1.0 - amount) + second[1] * amount)))),
            max(0, min(255, int(round(first[2] * (1.0 - amount) + second[2] * amount)))),
        )

    def _adjust_rgb_hsv_worker(self, rgb: tuple[int, int, int], *, sat_mul: float = 1.0, val_mul: float = 1.0, hue_shift: float = 0.0) -> tuple[int, int, int]:
        h, sat, val = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        h = (h + hue_shift) % 1.0
        sat = max(0.0, min(1.0, sat * sat_mul))
        val = max(0.0, min(1.0, val * val_mul))
        r, g, b = colorsys.hsv_to_rgb(h, sat, val)
        return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))

    def _palette_from_hex_worker(self, values: Any, fallback: tuple[int, int, int]) -> list[tuple[int, int, int]]:
        result: list[tuple[int, int, int]] = []
        if isinstance(values, list):
            for item in values[:8]:
                try:
                    result.append(self._rgb_from_hex_worker(item))
                except Exception:
                    continue
        if result:
            return result
        return [
            fallback,
            self._adjust_rgb_hsv_worker(fallback, sat_mul=0.92, val_mul=1.24),
            self._adjust_rgb_hsv_worker(fallback, sat_mul=1.06, val_mul=0.68),
        ]

    def _fit_emoji_canvas_frame(self, frame: Any, *, canvas_size: int = 128) -> Any:
        if Image is None:
            raise RuntimeError("Pillow não instalado no worker")
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        rgba = frame.convert("RGBA")
        if rgba.size == (canvas_size, canvas_size):
            return rgba.copy()
        width, height = rgba.size
        if width <= 0 or height <= 0:
            return Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        scale = min(canvas_size / float(width), canvas_size / float(height))
        new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        resized = rgba.resize(new_size, resampling)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        canvas.alpha_composite(resized, ((canvas_size - resized.width) // 2, (canvas_size - resized.height) // 2))
        return canvas

    def _recolor_rgba_image(self, img: Any, rgb: tuple[int, int, int], palette: list[tuple[int, int, int]] | None = None) -> Any:
        img = img.convert("RGBA")
        px = img.load()
        base = rgb
        usable_palette = palette or [base]
        light = usable_palette[1] if len(usable_palette) > 1 else self._adjust_rgb_hsv_worker(base, sat_mul=0.92, val_mul=1.22)
        dark = usable_palette[2] if len(usable_palette) > 2 else self._adjust_rgb_hsv_worker(base, sat_mul=1.04, val_mul=0.68)
        accents = usable_palette[3:] or [base]
        width, height = img.size
        for y in range(height):
            for x in range(width):
                r, g, b, a = px[x, y]
                if a < 8:
                    continue
                lum = max(0.0, min(1.0, (r * 0.299 + g * 0.587 + b * 0.114) / 255.0))
                if lum < 0.50:
                    target = self._mix_rgb_worker(dark, base, lum / 0.50)
                else:
                    target = self._mix_rgb_worker(base, light, (lum - 0.50) / 0.50)
                try:
                    oh, osat, _oval = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    if accents and osat > 0.08:
                        accent = accents[(int(oh * 12) + (x // 24) + (y // 24)) % len(accents)]
                        target = self._mix_rgb_worker(target, accent, 0.10)
                except Exception:
                    pass
                px[x, y] = (target[0], target[1], target[2], a)
        return img

    def _save_static_emoji_png(self, img: Any) -> bytes:
        candidate = self._fit_emoji_canvas_frame(img, canvas_size=128)
        out = io.BytesIO()
        candidate.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= 256 * 1024:
            return data
        quantized = candidate.convert("P", palette=Image.Palette.ADAPTIVE, colors=96).convert("RGBA") if Image is not None else candidate
        out = io.BytesIO()
        quantized.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= 256 * 1024:
            return data
        raise RuntimeError("emoji estático ficou maior que 256 KiB")

    def _save_animated_emoji_gif(self, frames: list[Any], durations: list[int]) -> bytes | None:
        if not frames:
            return None
        normalized_frames = [self._fit_emoji_canvas_frame(frame, canvas_size=128) for frame in frames]
        for step in (1, 2, 3, 4, 5, 6, 8, 10):
            selected = [frame for idx, frame in enumerate(normalized_frames) if idx % step == 0]
            selected_durations = [max(20, min(500, int((durations[idx] if idx < len(durations) else 80) * step))) for idx in range(len(normalized_frames)) if idx % step == 0]
            if not selected:
                continue
            out = io.BytesIO()
            selected[0].save(out, format="GIF", save_all=True, append_images=selected[1:], duration=selected_durations, loop=0, optimize=True, disposal=2)
            data = out.getvalue()
            if len(data) <= 256 * 1024:
                return data
        return None

    def _recolor_emoji_bytes(self, raw: bytes, *, animated: bool, color: str, palette: list[tuple[int, int, int]] | None = None) -> tuple[bytes, str]:
        if Image is None:
            raise RuntimeError("Pillow não instalado no worker")
        rgb = self._rgb_from_hex_worker(color)
        subtle_palette = palette or self._palette_from_hex_worker([], rgb)
        with Image.open(io.BytesIO(raw)) as img:
            if animated and getattr(img, "is_animated", False) and ImageSequence is not None:
                raw_frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(img)]
                frames = [self._recolor_rgba_image(self._fit_emoji_canvas_frame(frame), rgb, subtle_palette) for frame in raw_frames]
                durations = [int(getattr(frame, "info", {}).get("duration") or img.info.get("duration") or 80) for frame in ImageSequence.Iterator(img)]
                data = self._save_animated_emoji_gif(frames, durations)
                if data is not None:
                    return data, "gif"
                return self._save_static_emoji_png(frames[0]), "png"
            fitted = self._fit_emoji_canvas_frame(img.convert("RGBA"))
            out_img = self._recolor_rgba_image(fitted, rgb, subtle_palette)
            return self._save_static_emoji_png(out_img), "png"

    def _task_emoji_recolor(self, body: dict[str, Any]) -> dict[str, Any]:
        emojis = body.get("emojis") or []
        if not isinstance(emojis, list):
            raise ValueError("emojis precisa ser lista")
        color = str(body.get("color") or "#5865F2")
        base_rgb = self._rgb_from_hex_worker(color)
        monochrome = bool(body.get("monochrome", False))
        palette = self._palette_from_hex_worker(body.get("palette"), base_rgb)
        # A VPS já escolhe a cor/paleta efetiva da mensagem. O worker só executa
        # o processamento pesado e não deve trocar essa decisão por outra cor.
        # Quando o avatar é monocromático, a paleta recebida já vem coerente com isso.
        if monochrome and not body.get("palette"):
            palette = [
                base_rgb,
                self._adjust_rgb_hsv_worker(base_rgb, sat_mul=0.70, val_mul=1.18),
                self._adjust_rgb_hsv_worker(base_rgb, sat_mul=0.85, val_mul=0.72),
            ]
        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for raw in emojis[:4]:
            if not isinstance(raw, dict):
                continue
            raw_variants = raw.get("raw_variants")
            if not isinstance(raw_variants, list):
                raw_variants = []
            raw_variants = [str(item or "") for item in raw_variants if str(item or "")]
            emoji = {
                "raw": str(raw.get("raw") or ""),
                "raw_variants": raw_variants,
                "key": str(raw.get("key") or ""),
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or "emoji")[:32],
                "animated": bool(raw.get("animated")),
            }
            if emoji["raw"] and emoji["raw"] not in emoji["raw_variants"]:
                emoji["raw_variants"].insert(0, emoji["raw"])
            if not re.fullmatch(r"\d{15,25}", emoji["id"]):
                continue
            try:
                data = self._download_emoji_asset(emoji)
                out, fmt = self._recolor_emoji_bytes(data, animated=bool(emoji.get("animated")), color=color, palette=palette)
                items.append({**emoji, "format": fmt, "size": len(out), "data_b64": _b64encode(out, max_bytes=512 * 1024)})
            except Exception as exc:
                errors.append({"id": emoji["id"], "error": str(exc)[:160]})
                continue
        return {"ok": True, "items": items, "count": len(items), "errors": errors[:4], "monochrome": monochrome, "summary": f"{len(items)} emoji(s) recolorido(s)"}


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
            "phone_worker": r"phone-worker|phone_worker",
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


    def _task_hash_batch(self, body: dict[str, Any]) -> dict[str, Any]:
        files = body.get("files") or []
        if not isinstance(files, list) or not files:
            raise ValueError("files vazio")
        if len(files) > 64:
            raise ValueError("arquivos demais para hash_batch")
        total = 0
        results: list[dict[str, Any]] = []
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"files[{index}] inválido")
            name = _safe_name(item.get("name"), fallback=f"file-{index}.bin")
            data = _b64decode(str(item.get("data_b64") or ""), max_bytes=self.max_body_bytes)
            total += len(data)
            if total > self.max_body_bytes:
                raise ValueError("entrada total grande demais")
            results.append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return {"ok": True, "summary": f"{len(results)} hash(es) calculados", "files": results, "total_bytes": total}

    def _task_endpoint_probe(self, body: dict[str, Any]) -> dict[str, Any]:
        raw_targets = body.get("targets") or body.get("urls") or []
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        if not isinstance(raw_targets, list) or not raw_targets:
            base_url, _token, _worker_id = _core_worker_auth_parts()
            raw_targets = [base_url.rstrip("/") + "/health"] if base_url else []
        if not raw_targets:
            raise ValueError("nenhum endpoint informado")
        timeout = max(0.5, min(8.0, float(body.get("timeout_seconds") or 3.0)))
        max_targets = max(1, min(8, int(body.get("max_targets") or 4)))
        results: list[dict[str, Any]] = []
        for raw_url in raw_targets[:max_targets]:
            url = str(raw_url or "").strip()
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                results.append({"url": _short_text(url, limit=120), "ok": False, "error": "URL inválida"})
                continue
            started = time.perf_counter()
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json,text/plain,*/*", "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}"}, method="GET")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    sample = resp.read(512)
                    status = int(getattr(resp, "status", 200) or 200)
                results.append({
                    "url": _short_text(url, limit=160),
                    "ok": 200 <= status < 500,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "bytes_sampled": len(sample),
                })
            except Exception as exc:
                results.append({"url": _short_text(url, limit=160), "ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"})
        return {"ok": any(item.get("ok") for item in results), "summary": "endpoints testados pelo worker", "results": results}

    def _task_music_agent_proxy(self, body: dict[str, Any]) -> dict[str, Any]:
        hooks = SimpleNamespace(
            load_runtime_env=_load_phone_worker_runtime_env,
            ensure_token=_ensure_music_agent_token_env,
            truthy=lambda value, default=False: _phone_worker_music_bridge_module("configuracao").truthy(value, default),
            safe_telemetry=_safe_telemetry,
            snapshot=_music_agent_snapshot,
            version_lt=_version_lt_loose,
            run_service=_run_service_action,
            short_text=_short_text,
        )
        return _phone_worker_music_bridge_module("proxy").proxy_music_agent(
            body, max_output_bytes=self.max_output_bytes, hooks=hooks
        )

    def _task_music_ytdlp_resolve(self, body: dict[str, Any]) -> dict[str, Any]:
        return _phone_worker_music_bridge_module("resolucao").resolve_ytdlp(
            body, job_timeout=self.job_timeout
        )

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


def _phone_worker_pending_results_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_PENDING_RESULTS_FILE") or (_phone_worker_dir() / "phone-worker-pending-results.json")).expanduser()


def _phone_worker_pending_results_archive_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_PENDING_RESULTS_ARCHIVE_FILE") or (_phone_worker_dir() / "phone-worker-pending-results.archive.json")).expanduser()


def _phone_worker_update_status_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_UPDATE_STATUS_FILE") or (_phone_worker_dir() / "phone-worker-update.status.json")).expanduser()


def _write_json_file_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_persisted_pending_core_job_results() -> None:
    data = _read_json_file(_phone_worker_pending_results_file())
    items = data.get("results") if isinstance(data.get("results"), dict) else {}
    if not items:
        return
    with _CORE_JOB_LOCK:
        for job_id, payload in list(items.items())[:20]:
            if isinstance(payload, dict) and str(job_id or "").strip():
                _PENDING_CORE_JOB_RESULTS[str(job_id)] = dict(payload)


def _persist_pending_core_job_results() -> None:
    path = _phone_worker_pending_results_file()
    with _CORE_JOB_LOCK:
        items = {k: v for k, v in _PENDING_CORE_JOB_RESULTS.items() if k and isinstance(v, dict)}
    if not items:
        with contextlib.suppress(Exception):
            path.unlink()
        return
    try:
        _write_json_file_atomic(path, {"updated_at": time.time(), "results": items})
    except Exception as exc:
        print(f"[core-worker-jobs] não consegui persistir resultado pendente: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)


def _archive_pending_core_job_result(job_id: str, payload: dict[str, Any], *, reason: str, response: dict[str, Any] | None = None) -> None:
    """Move um resultado pendente impossível de reenviar para histórico local curto.

    Isso evita loops eternos quando a VPS já limpou o job antigo, mas preserva
    informação suficiente para diagnóstico.
    """
    safe_job_id = _short_text(job_id, limit=80)
    if not safe_job_id:
        return
    path = _phone_worker_pending_results_archive_file()
    archive = _read_json_file(path)
    items = archive.get("items") if isinstance(archive.get("items"), list) else []
    entry = {
        "job_id": safe_job_id,
        "archived_at": time.time(),
        "reason": _short_text(reason, limit=180),
        "summary": _short_text(payload.get("summary") or payload.get("error") or "", limit=180),
        "status": _short_text(payload.get("status") or "", limit=40),
        "type": _short_text((payload.get("result") or {}).get("type") if isinstance(payload.get("result"), dict) else payload.get("type"), limit=60),
        "stored_at": payload.get("stored_at"),
        "response": _short_text(json.dumps(response or {}, ensure_ascii=False, separators=(",", ":")), limit=600),
    }
    items.append(entry)
    items = items[-40:]
    try:
        _write_json_file_atomic(path, {"updated_at": time.time(), "items": items})
    except Exception as exc:
        print(f"[core-worker-jobs] não consegui arquivar resultado pendente antigo: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)


def _job_result_rejection_is_permanent(code: int, data: dict[str, Any]) -> bool:
    text = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    if int(code or 0) == 404 and ("job não encontrado" in text or "job nao encontrado" in text or "job not found" in text):
        return True
    return False


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
    watch_alive = result.get("watch_pid_alive") is True
    result["watchdog_ok"] = bool(watch_alive)
    result["supervisor_ok"] = bool(
        (result.get("pid_file_alive") in {True, None})
        and (result.get("duplicates") in {0, None})
        and watch_alive
    )
    return result


def _sshd_snapshot() -> dict[str, Any]:
    """Diagnóstico local do canal SSH do Termux usado pela VPS para wake.

    Não inicia/paralisa SSH automaticamente aqui; apenas informa se o caminho
    que a VPS tenta usar parece existir. Isso ajuda o painel a diferenciar
    "Tailscale ativo" de "SSHD/porta indisponível".
    """
    configured_port = str(os.getenv("PHONE_WORKER_SSH_PORT") or "8022").strip() or "8022"
    result: dict[str, Any] = {
        "ok": False,
        "source": "termux-sshd",
        "installed": bool(shutil.which("sshd")),
        "port": configured_port,
        "running": False,
        "processes": 0,
        "listening": False,
        "listening_ports": [],
    }
    try:
        count = _pgrep_count("sshd")
        result["processes"] = count
        result["running"] = count > 0
    except Exception as exc:
        result["process_error"] = _short_text(exc, limit=100)
    output = ""
    if shutil.which("ss"):
        _code, stdout, stderr = _run_text_command(["ss", "-lnt"], timeout=2.0, max_bytes=16384)
        output = stdout or stderr or ""
    elif shutil.which("netstat"):
        _code, stdout, stderr = _run_text_command(["netstat", "-lnt"], timeout=2.0, max_bytes=16384)
        output = stdout or stderr or ""
    ports: list[str] = []
    if output:
        for line in output.splitlines():
            if "LISTEN" not in line.upper() and not re.search(r"[:.]\d+\s", line):
                continue
            for match in re.findall(r"(?::|\.)(\d{2,5})(?:\s|$)", line):
                if match not in ports:
                    ports.append(match)
        result["listening_ports"] = ports[:16]
        result["listening"] = configured_port in ports or "22" in ports
    if not result["installed"]:
        result["summary"] = "sshd não instalado no Termux"
    elif result["listening"]:
        result["summary"] = f"sshd ouvindo porta {configured_port}"
    elif result["running"]:
        result["summary"] = "sshd rodando, mas porta configurada não apareceu ouvindo"
    else:
        result["summary"] = "sshd parado; wake via SSH não funciona"
    result["ok"] = bool(result.get("installed") and (result.get("running") or result.get("listening")))
    return result


def _termux_boot_script_path() -> Path:
    return (Path.home() / ".termux" / "boot" / "10-core-worker").expanduser()


def _termux_boot_script_content() -> str:
    return "\n".join([
        '#!/data/data/com.termux/files/usr/bin/sh',
        '# Auto-start do Core Worker pelo Termux:Boot.',
        '# Criado/reparado pelo phone-worker. Não coloque segredos aqui.',
        'termux-wake-lock 2>/dev/null || true',
        'sleep "${PHONE_WORKER_BOOT_DELAY_SECONDS:-25}"',
        'cd "$HOME/phone-worker" || exit 0',
        'if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then',
        '  nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.boot.log" 2>&1 &',
        '  exit 0',
        'fi',
        'echo \'[core-worker-boot] watch-phone-worker.sh não encontrado\' >> "$HOME/phone-worker.log"',
        ''
    ])


def _termux_shell_autostart_block() -> str:
    """Bloco gerenciado para iniciar o watchdog quando o Termux é aberto."""
    return "\n".join([
        '# >>> core-worker-autostart >>>',
        '# Bloco gerenciado pelo Core Worker. Não coloque segredos aqui.',
        'if [ -z "${CORE_WORKER_SHELL_AUTOSTART_DONE:-}" ]; then',
        '  export CORE_WORKER_SHELL_AUTOSTART_DONE=1',
        '  if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then',
        '    (',
        '      termux-wake-lock >/dev/null 2>&1 || true',
        '      cd "$HOME/phone-worker" >/dev/null 2>&1 || exit 0',
        '      nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.shell.log" 2>&1 &',
        '    ) >/dev/null 2>&1 &',
        '  fi',
        'fi',
        '# <<< core-worker-autostart <<<',
        ''
    ])


def _update_managed_shell_block(path: Path, block: str) -> bool:
    start = '# >>> core-worker-autostart >>>'
    end = '# <<< core-worker-autostart <<<'
    path = path.expanduser()
    try:
        text = path.read_text(encoding='utf-8', errors='ignore') if path.exists() else ''
    except Exception:
        text = ''
    pattern = re.compile(re.escape(start) + r'.*?' + re.escape(end) + r'\n?', re.DOTALL)
    clean = pattern.sub('', text).rstrip()
    new_text = (clean + '\n\n' if clean else '') + block
    if text == new_text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding='utf-8')
    return True


def _termux_shell_autostart_status_snapshot() -> dict[str, Any]:
    files = [Path.home() / '.bashrc', Path.home() / '.profile']
    items: list[dict[str, Any]] = []
    ok_any = False
    for path in files:
        exists = path.exists()
        has_block = False
        has_watchdog = False
        try:
            text = path.read_text(encoding='utf-8', errors='ignore') if exists else ''
            has_block = '# >>> core-worker-autostart >>>' in text and '# <<< core-worker-autostart <<<' in text
            has_watchdog = 'watch-phone-worker.sh' in text
        except Exception:
            pass
        ok = bool(exists and has_block and has_watchdog)
        ok_any = ok_any or ok
        items.append({'path': str(path), 'exists': exists, 'content_ok': ok, 'has_block': has_block, 'has_watchdog': has_watchdog})
    return {
        'ok': bool(ok_any),
        'source': 'termux-shell',
        'files': items,
        'summary': 'shell abre watchdog ao abrir Termux' if ok_any else 'shell não dispara watchdog ao abrir Termux',
    }


def _repair_termux_shell_autostart() -> dict[str, Any]:
    block = _termux_shell_autostart_block()
    changed: list[str] = []
    errors: list[str] = []
    for path in (Path.home() / '.bashrc', Path.home() / '.profile'):
        try:
            if _update_managed_shell_block(path, block):
                changed.append(str(path))
        except Exception as exc:
            errors.append(f'{path}: {type(exc).__name__}: {_short_text(exc, limit=100)}')
    status = _termux_shell_autostart_status_snapshot()
    status['changed_files'] = changed
    status['changed'] = bool(changed)
    if errors:
        status['errors'] = errors[:4]
        status['ok'] = False
        status['summary'] = 'falha reparando autostart de shell do Termux'
    return status


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
            has_watch = "watch-phone-worker.sh" in content
            direct_start = "start-phone-worker.sh" in content and not has_watch
            result["mode"] = "watchdog" if has_watch else ("direct-start" if direct_start else "unknown")
            result["content_ok"] = has_watch and "phone-worker" in content and "nohup" in content
            result["uses_watchdog"] = bool(has_watch)
            result["direct_start_only"] = bool(direct_start)
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
        result["warning"] = "script de boot ausente/incompleto ou não aponta para watchdog"
    pieces = [
        "script existe" if result.get("exists") else "script ausente",
        "executável" if result.get("executable") else "sem permissão de execução",
        "watchdog" if result.get("uses_watchdog") else "sem watchdog",
        "Termux:Boot instalado" if package.get("available") else "Termux:Boot não detectado",
    ]
    result["summary"] = ("boot automático ok: " if result.get("ok") else "boot automático precisa atenção: ") + "; ".join(pieces)
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
    shell_status = _repair_termux_shell_autostart()
    status = _termux_boot_status_snapshot()
    status.update({
        "ok": bool(status.get("ok")) and bool(shell_status.get("ok", True)),
        "changed": bool(changed or shell_status.get("changed")),
        "shell_autostart": shell_status,
        "summary": "boot/shell automático reparado" if status.get("ok") else "boot automático criado, verifique Termux:Boot",
    })
    return status


def _home_script(name: str) -> Path:
    return (Path.home() / name).expanduser()


def _active_release_dir() -> Path | None:
    explicit = str(os.getenv("PHONE_WORKER_RELEASE_DIR") or "").strip()
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.append(Path(os.getenv("PHONE_WORKER_RUNTIME_ROOT") or (Path.home() / ".core-worker-runtime")).expanduser() / "current")
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved.is_dir() and (resolved / "phone_worker.py").is_file():
            return resolved
    return None


def _runtime_entrypoint_wrapper(name: str) -> str:
    return "\n".join([
        '#!/data/data/com.termux/files/usr/bin/bash',
        '# Wrapper autorreparado pelo Core Worker; a release `current` é autoritativa.',
        'set -u',
        'RUNTIME_ROOT="${PHONE_WORKER_RUNTIME_ROOT:-$HOME/.core-worker-runtime}"',
        'WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"',
        'for LINK in current previous; do',
        f'  TARGET="$RUNTIME_ROOT/$LINK/{name}"',
        '  if [ -f "$TARGET" ]; then',
        '    RELEASE_DIR="$(cd "$(dirname "$TARGET")" 2>/dev/null && pwd -P || true)"',
        '    if [ -n "$RELEASE_DIR" ]; then export PHONE_WORKER_RELEASE_DIR="$RELEASE_DIR"; fi',
        '    export PHONE_WORKER_DIR="$WORKER_DIR"',
        '    exec /data/data/com.termux/files/usr/bin/bash "$TARGET" "$@"',
        '  fi',
        'done',
        f'echo "[core-worker-wrapper] {name} não encontrado em current/previous" >&2',
        'exit 1',
        '',
    ])


def _repair_runtime_entrypoint_wrappers() -> dict[str, Any]:
    active = _active_release_dir()
    root = _phone_worker_dir()
    if active is None:
        return {"ok": False, "changed": [], "reason": "release ativa ausente"}
    try:
        if root.resolve() == active.resolve():
            return {"ok": True, "changed": [], "reason": "worker_dir já é release ativa"}
    except Exception:
        pass
    changed: list[str] = []
    errors: list[str] = []
    root.mkdir(parents=True, exist_ok=True)
    for name in ("start-phone-worker.sh", "watch-phone-worker.sh"):
        if not (active / name).is_file():
            errors.append(f"{name}: ausente na release ativa")
            continue
        path = root / name
        content = _runtime_entrypoint_wrapper(name)
        try:
            previous = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
            if previous != content:
                tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
                tmp.write_text(content, encoding="utf-8")
                os.chmod(tmp, 0o755)
                tmp.replace(path)
                changed.append(name)
            else:
                os.chmod(path, 0o755)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {_short_text(exc, limit=100)}")
    return {"ok": not errors, "changed": changed, "errors": errors[:4], "release": str(active)}


def _script_candidates(name: str) -> list[Path]:
    # A release ativa é autoritativa; cópias em ~/phone-worker são apenas
    # wrappers/compatibilidade e não podem vencer código recém-promovido.
    candidates: list[Path] = []
    active = _active_release_dir()
    if active is not None:
        candidates.append(active / name)
    worker_path = (_phone_worker_dir() / name).expanduser()
    home_path = _home_script(name)
    for path in (worker_path, home_path):
        if path not in candidates:
            candidates.append(path)
    return candidates


def _best_script(name: str) -> Path:
    for path in _script_candidates(name):
        if path.exists():
            return path
    return _script_candidates(name)[0]


def _duplicate_installations_snapshot() -> dict[str, Any]:
    home = Path.home().expanduser()
    official = _phone_worker_dir().expanduser()
    candidates: list[Path] = []
    for path in (official, home / "phone-worker-install", home / "phone-worker"):
        try:
            resolved = path.resolve()
            if path.exists() and (path / "phone_worker.py").exists() and all(resolved != existing.resolve() for existing in candidates):
                candidates.append(path)
        except Exception:
            continue

    boot_text = ""
    with contextlib.suppress(Exception):
        boot_text = _termux_boot_script_path().read_text(encoding="utf-8", errors="ignore")[:8192]
    env_dir = str(os.getenv("PHONE_WORKER_DIR") or "").strip()

    duplicate_details: list[dict[str, Any]] = []
    for path in candidates:
        try:
            if path.resolve() == official.resolve():
                continue
        except Exception:
            continue
        path_s = str(path)
        active_reasons: list[str] = []
        if path_s and path_s in boot_text:
            active_reasons.append("referenciada pelo Termux:Boot")
        if env_dir and str(Path(env_dir).expanduser()) == path_s:
            active_reasons.append("PHONE_WORKER_DIR aponta para ela")
        for script_name in ("start-phone-worker.sh", "watch-phone-worker.sh"):
            script_path = path / script_name
            if script_path.exists() and os.access(script_path, os.X_OK):
                # Existir executável em duplicata não é ativo sozinho, mas ajuda no diagnóstico.
                pass
        duplicate_details.append({
            "path": path_s,
            "active": bool(active_reasons),
            "reason": "; ".join(active_reasons) if active_reasons else "duplicata encontrada, mas boot oficial não aponta para ela",
        })

    return {
        "official": str(official),
        "found": [str(path) for path in candidates],
        "duplicates": [item["path"] for item in duplicate_details],
        "details": duplicate_details,
        "has_duplicates": bool(duplicate_details),
        "active_duplicates": [item["path"] for item in duplicate_details if item.get("active")],
        "has_active_duplicates": any(bool(item.get("active")) for item in duplicate_details),
    }


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
    installs = _duplicate_installations_snapshot()
    shell_autostart = _termux_shell_autostart_status_snapshot()
    return {
        "complete": complete,
        "mirrored": mirrored,
        "scripts": scripts,
        "shell_autostart": shell_autostart,
        "installations": installs,
        # Só marcamos como problema quando a duplicata parece ativa/referenciada.
        # Diretórios antigos inativos continuam visíveis no diagnóstico sem poluir o card.
        "duplicate_installations": bool(installs.get("has_active_duplicates")),
        "duplicate_installations_found": bool(installs.get("has_duplicates")),
    }


def _local_boot_needs_repair() -> tuple[bool, str]:
    boot = _termux_boot_status_snapshot()
    scripts = _script_inventory()
    reasons: list[str] = []
    if not boot.get("ok"):
        reasons.append(str(boot.get("warning") or "boot incompleto"))
    installs = scripts.get("installations") if isinstance(scripts.get("installations"), dict) else {}
    if installs.get("has_active_duplicates"):
        reasons.append("duplicata ativa aponta para instalação antiga")
    shell_autostart = _termux_shell_autostart_status_snapshot()
    if not shell_autostart.get("ok"):
        reasons.append("Termux aberto não dispara watchdog")
    return bool(reasons), "; ".join(reasons)


def _auto_repair_local_boot_if_needed() -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_AUTO_BOOT_REPAIR", True):
        return {"enabled": False}
    try:
        needed, reason = _local_boot_needs_repair()
        shell_status = _termux_shell_autostart_status_snapshot()
        shell_needed = not bool(shell_status.get("ok"))
        if not needed and not shell_needed:
            return {"enabled": True, "changed": False, "reason": "ok", "shell_autostart": shell_status}
        repaired = _repair_termux_boot_script() if needed else _termux_boot_status_snapshot()
        shell_repaired = _repair_termux_shell_autostart() if shell_needed else shell_status
        reasons = [reason] if reason else []
        if shell_needed:
            reasons.append("shell do Termux não iniciava watchdog ao abrir app")
        return {
            "enabled": True,
            "changed": bool(needed or shell_needed),
            "reason": "; ".join(reasons) or "reparo aplicado",
            "boot": repaired,
            "shell_autostart": shell_repaired,
        }
    except Exception as exc:
        return {"enabled": True, "changed": False, "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"}


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
        "music": "music-agent",
        "music-agent": "music-agent",
        "musicagent": "music-agent",
        "agent-music": "music-agent",
    }
    service = aliases.get(service, service)
    if service not in {"phone-worker", "phone-worker-watch", "music-agent", "tailscale"}:
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
        watch_pid = _read_pid_file(_phone_worker_watch_pid_file())
        tmux_running = _tmux_session_exists(watch_session)
        return {
            "ok": True,
            "service": service,
            "manageable": True,
            "running": bool(_pid_alive(watch_pid) or tmux_running),
            "tmux_session": watch_session,
            "tmux_running": tmux_running,
            "watch_pid_file": str(_phone_worker_watch_pid_file()),
            "watch_pid": watch_pid,
            "watch_pid_alive": _pid_alive(watch_pid),
            "script": str(_best_script("watch-phone-worker.sh")),
            "scripts": _script_inventory(),
        }
    if service == "music-agent":
        return _phone_worker_music_bridge_module("servico").service_status(_music_service_hooks())

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
            watch_pid = _read_pid_file(_phone_worker_watch_pid_file())
            if watch_pid:
                with contextlib.suppress(Exception):
                    os.kill(int(watch_pid), 15)
            with contextlib.suppress(Exception):
                _phone_worker_watch_pid_file().unlink()
        if action in {"start", "restart"}:
            if not watch_script.exists():
                raise FileNotFoundError(str(watch_script))
            subprocess.Popen(["bash", str(watch_script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(1)
        return _service_status(service) | {"action": action}

    if service == "music-agent":
        return _phone_worker_music_bridge_module("servico").run_service_action(action, _music_service_hooks())

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


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


_GRADLE_DEFAULT_JVM_OPTS_RE = re.compile(r"(?m)^(?P<prefix>[ \t]*DEFAULT_JVM_OPTS[ \t]*=)[^\r\n]*$")


_TERMUX_ANDROID_SYSTEM_LIBRARIES = frozenset({
    "libandroid.so",
    "libbinder_ndk.so",
    "libc.so",
    "libdl.so",
    "libEGL.so",
    "libGLESv2.so",
    "libjnigraphics.so",
    "liblog.so",
    "libm.so",
    "libmediandk.so",
    "libnativewindow.so",
    "libOpenSLES.so",
    "libstdc++.so",
    "libsync.so",
    "libvulkan.so",
    "libz.so",
})
_ELF_NEEDED_RE = re.compile(r"\(NEEDED\).*?\[([^\]]+)\]")
_ELF_SONAME_RE = re.compile(r"\(SONAME\).*?\[([^\]]+)\]")


_WORKER_UPDATE_TARGETS: dict[str, tuple[str, str, int]] = {
    "phone_worker.py": ("worker", "phone_worker.py", 0o755),
    "phone_worker_runtime/__init__.py": ("worker", "phone_worker_runtime/__init__.py", 0o644),
    "phone_worker_runtime/config.py": ("worker", "phone_worker_runtime/config.py", 0o644),
    "phone_worker_runtime/telemetry.py": ("worker", "phone_worker_runtime/telemetry.py", 0o644),
    "phone_worker_runtime/ssh_transport.py": ("worker", "phone_worker_runtime/ssh_transport.py", 0o644),
    "phone_worker_runtime/control_plane.py": ("worker", "phone_worker_runtime/control_plane.py", 0o644),
    "phone_worker_runtime/voice_state.py": ("worker", "phone_worker_runtime/voice_state.py", 0o644),
    "phone_worker_runtime/tts_policy.py": ("worker", "phone_worker_runtime/tts_policy.py", 0o644),
    "phone_worker_runtime/tts_cache.py": ("worker", "phone_worker_runtime/tts_cache.py", 0o644),
    "phone_worker_runtime/tts_android.py": ("worker", "phone_worker_runtime/tts_android.py", 0o644),
    "phone_worker_runtime/tts_providers.py": ("worker", "phone_worker_runtime/tts_providers.py", 0o644),
    "phone_worker_runtime/pcm_io.py": ("worker", "phone_worker_runtime/pcm_io.py", 0o644),
    "tts_transport.py": ("worker", "tts_transport.py", 0o644),
    "phone_worker_bootstrap.py": ("worker", "phone_worker_bootstrap.py", 0o755),
    "cogs/__init__.py": ("worker", "cogs/__init__.py", 0o644),
    "cogs/musica/__init__.py": ("worker", "cogs/musica/__init__.py", 0o644),
    "cogs/musica/runtime_telefone/__init__.py": ("worker", "cogs/musica/runtime_telefone/__init__.py", 0o644),
    "cogs/musica/runtime_telefone/agente/__init__.py": ("worker", "cogs/musica/runtime_telefone/agente/__init__.py", 0o644),
    "cogs/musica/runtime_telefone/agente/configuracao.py": ("worker", "cogs/musica/runtime_telefone/agente/configuracao.py", 0o644),
    "cogs/musica/runtime_telefone/agente/ciclo_vida.py": ("worker", "cogs/musica/runtime_telefone/agente/ciclo_vida.py", 0o644),
    "cogs/musica/runtime_telefone/agente/correspondencia.py": ("worker", "cogs/musica/runtime_telefone/agente/correspondencia.py", 0o644),
    "cogs/musica/runtime_telefone/agente/utilitarios.py": ("worker", "cogs/musica/runtime_telefone/agente/utilitarios.py", 0o644),
    "cogs/musica/runtime_telefone/agente/estado.py": ("worker", "cogs/musica/runtime_telefone/agente/estado.py", 0o644),
    "cogs/musica/runtime_telefone/agente/efeitos.py": ("worker", "cogs/musica/runtime_telefone/agente/efeitos.py", 0o644),
    "cogs/musica/runtime_telefone/agente/mixer_pcm.py": ("worker", "cogs/musica/runtime_telefone/agente/mixer_pcm.py", 0o644),
    "cogs/musica/runtime_telefone/agente/buffer_pcm.py": ("worker", "cogs/musica/runtime_telefone/agente/buffer_pcm.py", 0o644),
    "cogs/musica/runtime_telefone/agente/preparacao_audio.py": ("worker", "cogs/musica/runtime_telefone/agente/preparacao_audio.py", 0o644),
    "cogs/musica/runtime_telefone/agente/validade_stream.py": ("worker", "cogs/musica/runtime_telefone/agente/validade_stream.py", 0o644),
    "cogs/musica/runtime_telefone/agente/archive_manifest.py": ("worker", "cogs/musica/runtime_telefone/agente/archive_manifest.py", 0o644),
    "cogs/musica/runtime_telefone/agente/archive_ogg.py": ("worker", "cogs/musica/runtime_telefone/agente/archive_ogg.py", 0o644),
    "cogs/musica/runtime_telefone/agente/archive_pipeline.py": ("worker", "cogs/musica/runtime_telefone/agente/archive_pipeline.py", 0o644),
    "cogs/musica/runtime_telefone/agente/archive_staging.py": ("worker", "cogs/musica/runtime_telefone/agente/archive_staging.py", 0o644),
    "cogs/musica/runtime_telefone/agente/audio_segmentado.py": ("worker", "cogs/musica/runtime_telefone/agente/audio_segmentado.py", 0o644),
    "cogs/musica/runtime_telefone/agente/resolucao.py": ("worker", "cogs/musica/runtime_telefone/agente/resolucao.py", 0o644),
    "cogs/musica/runtime_telefone/agente/ytdlp_quente.py": ("worker", "cogs/musica/runtime_telefone/agente/ytdlp_quente.py", 0o644),
    "cogs/musica/runtime_telefone/agente/reproducao.py": ("worker", "cogs/musica/runtime_telefone/agente/reproducao.py", 0o644),
    "cogs/musica/runtime_telefone/agente/tts.py": ("worker", "cogs/musica/runtime_telefone/agente/tts.py", 0o644),
    "cogs/musica/runtime_telefone/agente/servidor.py": ("worker", "cogs/musica/runtime_telefone/agente/servidor.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/__init__.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/__init__.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/configuracao.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/configuracao.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/control_plane.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/control_plane.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/streams.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/streams.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/resolucao.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/resolucao.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/proxy.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/proxy.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/telemetria.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/telemetria.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/voz_compartilhada.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/voz_compartilhada.py", 0o644),
    "cogs/musica/runtime_telefone/ponte_worker/servico.py": ("worker", "cogs/musica/runtime_telefone/ponte_worker/servico.py", 0o644),
    "cogs/musica/runtime_telefone/termux/__init__.py": ("worker", "cogs/musica/runtime_telefone/termux/__init__.py", 0o644),
    "cogs/musica/runtime_telefone/termux/integracao-worker.sh": ("worker", "cogs/musica/runtime_telefone/termux/integracao-worker.sh", 0o755),
    "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh": ("worker", "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh", 0o755),
    "cogs/musica/runtime_telefone/termux/musica.env.example": ("worker", "cogs/musica/runtime_telefone/termux/musica.env.example", 0o600),
    "start-phone-worker.sh": ("worker", "start-phone-worker.sh", 0o755),
    "watch-phone-worker.sh": ("worker", "watch-phone-worker.sh", 0o755),
    "pair-phone-worker.sh": ("worker", "pair-phone-worker.sh", 0o755),
    "repair-phone-worker.sh": ("worker", "repair-phone-worker.sh", 0o755),
    "bootstrap-phone-worker.sh": ("worker", "bootstrap-phone-worker.sh", 0o755),
    "install.sh": ("worker", "install.sh", 0o755),
    "README.md": ("worker", "README.md", 0o644),
    "phone-worker.env.example": ("worker", "phone-worker.env.example", 0o600),
    "teto_renderer/__init__.py": ("worker", "teto_renderer/__init__.py", 0o644),
    "teto_renderer/errors.py": ("worker", "teto_renderer/errors.py", 0o644),
    "teto_renderer/cache.py": ("worker", "teto_renderer/cache.py", 0o644),
    "teto_renderer/voicebank.py": ("worker", "teto_renderer/voicebank.py", 0o644),
    "teto_renderer/phonemizer.py": ("worker", "teto_renderer/phonemizer.py", 0o644),
    "teto_renderer/ptbr_g2p.py": ("worker", "teto_renderer/ptbr_g2p.py", 0o644),
    "teto_renderer/prosody.py": ("worker", "teto_renderer/prosody.py", 0o644),
    "teto_renderer/renderer.py": ("worker", "teto_renderer/renderer.py", 0o644),
    "teto_renderer/worldline.py": ("worker", "teto_renderer/worldline.py", 0o644),
    "teto_renderer/worldline_native.py": ("worker", "teto_renderer/worldline_native.py", 0o644),
    "teto_renderer/OPENUTAU_LICENSE.txt": ("worker", "teto_renderer/OPENUTAU_LICENSE.txt", 0o644),
    "scripts/validate-teto-assets.py": ("worker", "scripts/validate-teto-assets.py", 0o755),
}
# These installation-only files remain accepted for manual/legacy updates,
# but are not part of the immutable runtime release or its source hash.
_PHONE_WORKER_SOURCE_HASH_EXCLUDED = frozenset({"repair-phone-worker.sh", "install.sh", "bootstrap-phone-worker.sh", "README.md", "phone-worker.env.example", "cogs/musica/runtime_telefone/termux/musica.env.example", "scripts/validate-teto-assets.py", "teto_renderer/OPENUTAU_LICENSE.txt"})
_PHONE_WORKER_SOURCE_HASH_CACHE: dict[str, Any] = {"signature": None, "value": ""}
_PHONE_WORKER_SOURCE_HASH_LOCK = threading.Lock()


def _phone_worker_source_hash() -> str:
    """Hash do conjunto instalável, ignorando logs, env e arquivos temporários."""
    entries: list[tuple[str, Path, int, int, int]] = []
    for target in sorted(_WORKER_UPDATE_TARGETS):
        if target in _PHONE_WORKER_SOURCE_HASH_EXCLUDED:
            continue
        path, _mode = _safe_update_target_path(target)
        if not path.is_file() and target.startswith("cogs/") and not str(os.getenv("PHONE_WORKER_RELEASE_DIR") or "").strip():
            # Em checkout da VPS, a fonte autoritativa do domínio de música fica
            # fora de deploy/termux. Releases extraídas continuam usando apenas
            # arquivos presentes no próprio diretório instalado.
            repo_candidate = Path(__file__).resolve().parents[3] / target
            if repo_candidate.is_file():
                path = repo_candidate
        try:
            stat_result = path.stat()
        except OSError:
            return ""
        if not path.is_file():
            return ""
        entries.append((target, path, int(stat_result.st_size), int(stat_result.st_mtime_ns), int(stat_result.st_ino)))
    signature = tuple((target, size, mtime_ns, inode) for target, _path, size, mtime_ns, inode in entries)
    with _PHONE_WORKER_SOURCE_HASH_LOCK:
        if _PHONE_WORKER_SOURCE_HASH_CACHE.get("signature") == signature:
            return str(_PHONE_WORKER_SOURCE_HASH_CACHE.get("value") or "")
        digest = hashlib.sha256()
        for target, path, _size, _mtime_ns, _inode in entries:
            digest.update(target.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_path(path).encode("ascii"))
            digest.update(b"\n")
        value = digest.hexdigest()
        _PHONE_WORKER_SOURCE_HASH_CACHE.update({"signature": signature, "value": value})
        return value


def _normalize_worker_update_target(target: str) -> str:
    raw = str(target or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError(f"arquivo de update não permitido: {raw or '<vazio>'}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"arquivo de update não permitido: {raw}")
    normalized = "/".join(parts)
    if normalized not in _WORKER_UPDATE_TARGETS:
        raise ValueError(f"arquivo de update não permitido: {normalized}")
    return normalized


def _safe_update_target_path(target: str) -> tuple[Path, int]:
    clean = _normalize_worker_update_target(target)
    location, filename, mode = _WORKER_UPDATE_TARGETS[clean]
    if location == "worker":
        release_dir = str(os.getenv("PHONE_WORKER_RELEASE_DIR") or "").strip()
        base = (Path(release_dir) if release_dir else _phone_worker_dir()).expanduser().resolve()
    else:
        base = Path.home().expanduser().resolve()
    path = (base / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"destino de update escapou da pasta permitida: {clean}") from exc
    return path, mode


def _read_phone_worker_version_from_path(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    match = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else ""


def _load_worker_update_files(payload: dict[str, Any], *, max_file_bytes: int, max_total_bytes: int) -> tuple[list[dict[str, Any]], str]:
    archive_url = str(payload.get("update_zip_url") or "").strip()
    if archive_url:
        parsed = urllib.parse.urlparse(archive_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("update_zip_url inválida")
        expected_archive_sha = str(payload.get("update_zip_sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_archive_sha):
            raise ValueError("sha256 do pacote de update ausente/inválido")
        max_archive_bytes = max(256 * 1024, _env_int("PHONE_WORKER_UPDATE_ARCHIVE_MAX_BYTES", 8 * 1024 * 1024))
        with tempfile.TemporaryDirectory(prefix="phone-worker-update-") as temp_dir:
            archive_path = Path(temp_dir) / "update.zip"
            download = _download_url_to_file(
                archive_url,
                archive_path,
                timeout=max(10.0, _env_float("PHONE_WORKER_UPDATE_DOWNLOAD_TIMEOUT_SECONDS", 60.0)),
                max_bytes=max_archive_bytes,
                accept="application/zip,*/*",
                size_label="pacote de update",
            )
            if not download.get("ok"):
                raise RuntimeError(f"download do update falhou: {_short_text(download.get('error'), limit=160)}")
            if str(download.get("sha256") or "").lower() != expected_archive_sha:
                raise ValueError("sha256 divergente no pacote de update")
            expected_bytes = int(payload.get("update_zip_bytes") or 0)
            if expected_bytes and int(download.get("bytes") or 0) != expected_bytes:
                raise ValueError("tamanho divergente no pacote de update")
            with zipfile.ZipFile(archive_path) as zf:
                members = [member for member in zf.infolist() if not member.is_dir()]
                names = [str(member.filename or "").replace("\\", "/") for member in members]
                if len(names) != len(set(names)) or len(names) > 25:
                    raise ValueError("pacote de update com membros duplicados/em excesso")
                if "phone-worker-update.json" not in names:
                    raise ValueError("manifesto do pacote de update ausente")
                manifest_info = zf.getinfo("phone-worker-update.json")
                if manifest_info.file_size > 64 * 1024:
                    raise ValueError("manifesto do pacote de update grande demais")
                manifest = json.loads(zf.read(manifest_info).decode("utf-8"))
                if not isinstance(manifest, dict) or manifest.get("schema") != "core-phone-worker-update-v1":
                    raise ValueError("schema inválido no pacote de update")
                manifest_files = manifest.get("files")
                if not isinstance(manifest_files, list) or not manifest_files or len(manifest_files) > 24:
                    raise ValueError("lista de arquivos inválida no pacote de update")
                for key in ("version", "source_hash"):
                    expected = str(payload.get(key) or "").strip()
                    actual = str(manifest.get(key) or "").strip()
                    if expected and actual != expected:
                        raise ValueError(f"{key} divergente no pacote de update")
                prepared: list[dict[str, Any]] = []
                total = 0
                expected_names = {"phone-worker-update.json"}
                seen_targets: set[str] = set()
                for item in manifest_files:
                    if not isinstance(item, dict):
                        raise ValueError("item inválido no manifesto de update")
                    target = _normalize_worker_update_target(str(item.get("target") or ""))
                    _target_path, allowed_mode = _safe_update_target_path(target)
                    declared_mode = int(item.get("mode") or allowed_mode)
                    if declared_mode != allowed_mode:
                        raise ValueError(f"modo não permitido no update: {target}")
                    if target in seen_targets:
                        raise ValueError(f"arquivo duplicado no manifesto: {target}")
                    seen_targets.add(target)
                    expected_names.add(target)
                    info = zf.getinfo(target)
                    if info.flag_bits & 0x1 or info.file_size > max_file_bytes:
                        raise ValueError(f"arquivo inválido/grande demais no update: {target}")
                    raw = zf.read(info)
                    total += len(raw)
                    if total > max_total_bytes:
                        raise ValueError("update grande demais")
                    sha = hashlib.sha256(raw).hexdigest()
                    if sha != str(item.get("sha256") or "").strip().lower() or len(raw) != int(item.get("bytes") or -1):
                        raise ValueError(f"integridade divergente em {target}")
                    prepared.append({"target": target, "mode": allowed_mode, "sha256": sha, "raw": raw})
                if set(names) != expected_names:
                    raise ValueError("pacote de update contém arquivo não declarado")
                return prepared, "zip-v1"

    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("payload de update sem arquivos")
    if len(files) > 24:
        raise ValueError("arquivos demais no update")
    prepared = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("item inválido no update")
        target = _normalize_worker_update_target(str(item.get("target") or item.get("name") or ""))
        _target_path, allowed_mode = _safe_update_target_path(target)
        declared_mode = int(item.get("mode") or allowed_mode)
        if declared_mode != allowed_mode:
            raise ValueError(f"modo não permitido no update: {target}")
        raw = _b64decode(str(item.get("data_b64") or ""), max_bytes=max_file_bytes)
        total += len(raw)
        if total > max_total_bytes:
            raise ValueError("update grande demais")
        sha = hashlib.sha256(raw).hexdigest()
        expected = str(item.get("sha256") or "").strip().lower()
        if expected and expected != sha:
            raise ValueError(f"sha256 divergente em {target}")
        prepared.append({"target": target, "mode": allowed_mode, "sha256": sha, "raw": raw})
    return prepared, "inline-b64-v1"


def _apply_worker_update(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("bootstrap_manifest") is True or str(payload.get("update_transport") or "").strip() == "bootstrap-manifest-v2":
        bootstrap = Path(__file__).with_name("phone_worker_bootstrap.py")
        if not bootstrap.is_file():
            return {"ok": False, "summary": "bootstrap updater ausente; execute repair-phone-worker.sh uma vez", "state": "blocked_by_bootstrap_missing"}
        try:
            proc = subprocess.run([sys.executable, str(bootstrap), "--check"], cwd=str(bootstrap.parent), capture_output=True, text=True, timeout=max(30, _env_int("PHONE_WORKER_BOOTSTRAP_JOB_TIMEOUT_SECONDS", 180)))
            output = _sanitize_log_text((proc.stdout or "") + "\n" + (proc.stderr or ""), limit=4000).strip()
            parsed = {}
            for line in reversed((proc.stdout or "").splitlines()):
                try:
                    candidate = json.loads(line)
                except Exception:
                    continue
                if isinstance(candidate, dict):
                    parsed = candidate
                    break
            ok = proc.returncode == 0 and parsed.get("ok", True) is not False
            return {"ok": ok, "summary": _short_text(parsed.get("summary") or output or ("bootstrap concluído" if ok else "bootstrap falhou"), limit=240), "bootstrap": parsed, "update_transport": "bootstrap-manifest-v2", "restart_pending": bool(parsed.get("restart_pending"))}
        except Exception as exc:
            return {"ok": False, "summary": f"bootstrap updater falhou: {type(exc).__name__}: {_short_text(exc, limit=120)}", "update_transport": "bootstrap-manifest-v2"}
    if not _env_bool("PHONE_WORKER_SELF_UPDATE_ENABLED", True):
        raise PermissionError("self-update do phone-worker desativado por configuração")
    target_source_hash = str(payload.get("source_hash") or "").strip().lower()
    if target_source_hash and not re.fullmatch(r"[0-9a-f]{64}", target_source_hash):
        raise ValueError("source_hash do update ausente/inválido")
    current_source_hash = _phone_worker_source_hash()
    if target_source_hash and current_source_hash == target_source_hash:
        result: dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "summary": "update já aplicado; hash da fonte confere",
            "current_version": PHONE_WORKER_VERSION,
            "source_hash": current_source_hash,
        }
        # Uma entrega pode ser repetida depois de os arquivos terem sido gravados,
        # mas antes de o watchdog reiniciar o processo antigo. Preserve o restart
        # no caminho idempotente para nunca ficar executando código anterior.
        if _env_bool("PHONE_WORKER_UPDATE_RESTART", bool(payload.get("restart", True))):
            result.update({
                "deferred_restart": True,
                "deferred_restart_mode": "watchdog",
                "_deferred_phone_worker_action": "restart",
                "_deferred_phone_worker_session": str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker"),
                "_deferred_start_script": str(_best_script("start-phone-worker.sh")),
                "_deferred_watch_script": str(_best_script("watch-phone-worker.sh")),
            })
        return result
    updated: list[dict[str, Any]] = []
    errors: list[str] = []
    max_file_bytes = max(1024, _env_int("PHONE_WORKER_UPDATE_MAX_FILE_BYTES", 768 * 1024))
    max_total_bytes = max(max_file_bytes, _env_int("PHONE_WORKER_UPDATE_MAX_TOTAL_BYTES", 2 * 1024 * 1024))
    files, update_transport = _load_worker_update_files(payload, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes)
    total = sum(len(item.get("raw") or b"") for item in files)

    for item in files:
        target = str(item.get("target") or "").strip()
        try:
            normalized_target = _normalize_worker_update_target(target)
            path, mode = _safe_update_target_path(normalized_target)
            raw = bytes(item.get("raw") or b"")
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
            updated.append({"target": normalized_target, "paths": [str(p) for p in applied_paths], "bytes": len(raw), "sha256": actual[:12]})
        except Exception as exc:
            errors.append(f"{target or '<sem alvo>'}: {type(exc).__name__}: {_short_text(exc, limit=100)}")

    if errors:
        return {"ok": False, "summary": "update parcial/falhou", "updated": updated, "errors": errors[:8], "total_bytes": total}

    applied_source_hash = _phone_worker_source_hash()
    if target_source_hash and applied_source_hash != target_source_hash:
        return {
            "ok": False,
            "summary": "update aplicado, mas hash final da fonte divergiu",
            "updated": updated,
            "expected_source_hash": target_source_hash,
            "applied_source_hash": applied_source_hash,
            "total_bytes": total,
        }

    canonical_music_server = _phone_worker_dir() / "cogs/musica/runtime_telefone/agente/servidor.py"
    canonical_music_start = _phone_worker_dir() / "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh"
    if canonical_music_server.is_file() and canonical_music_start.is_file():
        for legacy in (
            _phone_worker_dir() / "music_agent.py",
            _phone_worker_dir() / "start-phone-music-agent.sh",
            _home_script("start-phone-music-agent.sh"),
        ):
            with contextlib.suppress(Exception):
                legacy.unlink()

    target_version = _short_text(payload.get("version"), limit=48, default="desconhecida")
    applied_version = _read_phone_worker_version_from_path(_phone_worker_dir() / "phone_worker.py") or target_version
    updated_names = {str(item.get("target") or "") for item in updated}
    applied_music_agent_version = _read_music_agent_version_from_path(_phone_worker_dir() / "cogs/musica/runtime_telefone/agente/servidor.py")
    music_agent_restart: dict[str, Any] | None = None
    if {"cogs/musica/runtime_telefone/agente/servidor.py", "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh", "cogs/musica/runtime_telefone/termux/integracao-worker.sh", "tts_transport.py"} & updated_names:
        try:
            music_agent_restart = _run_service_action("music-agent", "restart")
        except Exception as exc:
            errors.append(f"music-agent restart: {type(exc).__name__}: {_short_text(exc, limit=100)}")
    boot_status = _repair_termux_boot_script() if any(item.get("target") in {"start-phone-worker.sh", "watch-phone-worker.sh", "bootstrap-phone-worker.sh", "install.sh"} for item in updated) else _termux_boot_status_snapshot()
    shell_status = _repair_termux_shell_autostart()
    update_status = {
        "ok": True,
        "updated_at": time.time(),
        "previous_runtime_version": PHONE_WORKER_VERSION,
        "applied_file_version": applied_version,
        "applied_music_agent_version": applied_music_agent_version,
        "target_version": target_version,
        "source_hash": applied_source_hash,
        "update_transport": update_transport,
        "restart_requested": bool(payload.get("restart", True)),
        "files": [item.get("target") for item in updated],
        "boot_ok": bool(boot_status.get("ok")),
        "shell_autostart_ok": bool(shell_status.get("ok")),
    }
    with contextlib.suppress(Exception):
        _write_json_file_atomic(_phone_worker_update_status_file(), update_status)

    result: dict[str, Any] = {
        "ok": True,
        "summary": f"update aplicado: {len(updated)} arquivo(s); reinício pelo watchdog",
        "updated": updated,
        "total_bytes": total,
        "current_version": PHONE_WORKER_VERSION,
        "applied_file_version": applied_version,
        "applied_music_agent_version": applied_music_agent_version,
        "target_version": target_version,
        "source_hash": applied_source_hash,
        "update_transport": update_transport,
        "music_agent_restart": music_agent_restart,
        "boot": {"ok": bool(boot_status.get("ok")), "mode": boot_status.get("mode"), "summary": boot_status.get("summary")},
        "shell_autostart": {"ok": bool(shell_status.get("ok")), "summary": shell_status.get("summary"), "changed": bool(shell_status.get("changed"))},
        "update_status_file": str(_phone_worker_update_status_file()),
    }
    if _env_bool("PHONE_WORKER_UPDATE_RESTART", bool(payload.get("restart", True))):
        result.update({
            "deferred_restart": True,
            "deferred_restart_mode": "watchdog",
            "_deferred_phone_worker_action": "restart",
            "_deferred_phone_worker_session": str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker"),
            "_deferred_start_script": str(_best_script("start-phone-worker.sh")),
            "_deferred_watch_script": str(_best_script("watch-phone-worker.sh")),
        })
    return result


def _launch_deferred_phone_worker_action(result: dict[str, Any]) -> None:
    action = str(result.pop("_deferred_phone_worker_action", "") or "").strip().lower()
    if action not in {"stop", "restart"}:
        return
    session = str(result.pop("_deferred_phone_worker_session", "") or os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker")
    start_script = Path(str(result.pop("_deferred_start_script", "") or _best_script("start-phone-worker.sh"))).expanduser()
    watch_script = Path(str(result.pop("_deferred_watch_script", "") or _best_script("watch-phone-worker.sh"))).expanduser()
    worker_dir = _phone_worker_dir()
    script = worker_dir / f".core-worker-deferred-{action}.sh"
    pid_file = _phone_worker_pid_file()
    quoted_pid_file = shlex.quote(str(pid_file))
    lines = [
        '#!/data/data/com.termux/files/usr/bin/bash',
        'set +e',
        'sleep 1',
        'termux-wake-lock >/dev/null 2>&1 || true',
        f"PID_FILE={quoted_pid_file}",
        "pid=''",
        'if [ -r "$PID_FILE" ]; then pid=$(head -n1 "$PID_FILE" 2>/dev/null | tr -cd \'0-9\'); fi',
        'if [ -n "$pid" ] && [ "$pid" -gt 1 ] 2>/dev/null && kill -0 "$pid" 2>/dev/null; then',
        '  cmd=$(tr \'\\000\' \' \' < "/proc/$pid/cmdline" 2>/dev/null || true)',
        '  case "$cmd" in *phone_worker.py*) kill "$pid" >/dev/null 2>&1 || true ;; esac',
        'fi',
        f"tmux kill-session -t {shlex.quote(session)} >/dev/null 2>&1 || true",
        'sleep 1',
        'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then',
        '  cmd=$(tr \'\\000\' \' \' < "/proc/$pid/cmdline" 2>/dev/null || true)',
        '  case "$cmd" in *phone_worker.py*) kill -9 "$pid" >/dev/null 2>&1 || true ;; esac',
        'fi',
    ]
    if action == "restart":
        lines.extend([
            "sleep 1",
            f"bash {shlex.quote(str(start_script))} >/dev/null 2>&1 &",
            f"nohup bash {shlex.quote(str(watch_script))} >> {shlex.quote(str(_phone_worker_watch_log_file()))} 2>&1 &",
        ])
    try:
        worker_dir.mkdir(parents=True, exist_ok=True)
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o700)
        subprocess.Popen(["bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        print(f"[core-worker-service] falha ao agendar {action}: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)


def _assist_readiness_snapshot(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    battery = _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot())
    network = _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"})
    system = _safe_telemetry("system", _system_status, {"ok": False})
    battery_values = battery if isinstance(battery, dict) else {}
    level = None
    raw_level = battery_values.get("level")
    if raw_level is None:
        raw_level = battery_values.get("percent")
    try:
        level = float(raw_level)
    except (TypeError, ValueError):
        pass
    charging = battery_values.get("charging")
    if not isinstance(charging, bool):
        status = str(battery_values.get("status") or "").strip().lower()
        plugged = str(battery_values.get("plugged") or "").strip().lower()
        charging = status in {"charging", "full"} if status else plugged not in {"", "unplugged", "none", "unknown", "battery"}
    heavy_ok = True
    reasons: list[str] = []
    if level is not None and level < float(payload.get("min_battery_for_heavy") or 25) and not charging:
        heavy_ok = False
        reasons.append("bateria baixa para tarefa pesada")
    if not bool((network or {}).get("vps_reachable", True)) and _heartbeat_configured():
        reasons.append("VPS instável vista do worker")
    caps = set(capabilities) | set(roles)
    recommended: list[str] = ["log_summary", "zip_validate", "hash_batch", "endpoint_probe"]
    if "maintenance-plan" in caps or "cache-worker" in caps:
        recommended.append("maintenance_plan")
    if "ffprobe" in caps:
        recommended.append("media_probe")
    if "ffmpeg" in caps or "tts-convert" in caps:
        recommended.append("audio_convert")
    return {
        "ok": True,
        "summary": "worker auxiliar pronto" if heavy_ok else "worker auxiliar só para tarefas leves",
        "assist_enabled": _env_bool("CORE_WORKER_ASSIST_ENABLED", True),
        "heavy_ok": heavy_ok,
        "reasons": reasons[:6],
        "profile": _current_core_worker_profile(),
        "roles": roles,
        "capabilities": capabilities,
        "recommended_tasks": recommended,
        "battery": battery,
        "network": network,
        "system": {
            "uptime_seconds": system.get("uptime_seconds"),
            "disk_home": system.get("disk_home"),
            "loadavg": system.get("loadavg"),
            "ffmpeg": system.get("ffmpeg"),
            "ffprobe": system.get("ffprobe"),
        },
    }

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
                    "sshd": _safe_telemetry("sshd", _sshd_snapshot, {"ok": False}),
                },
                "ffmpeg": _command_version("ffmpeg"),
                "ffprobe": _command_version("ffprobe"),
                "roles": _env_list("CORE_WORKER_ROLES", []),
                "capabilities": _env_list("CORE_WORKER_CAPABILITIES", []),
            }
        elif kind == "network_probe":
            result = {"ok": True, "summary": "rede testada", "network": _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}), "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"})}
        elif kind == "ssh_connection_diagnostic":
            result = {
                "ok": True, "summary": "conexão SSH e acesso HTTP à VPS verificados",
                "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
                "runtime_kind": "termux", "pid": os.getpid(), "http_port": _EFFECTIVE_HTTP_PORT,
                "connection": _ssh_connection_snapshot(),
                "vps_http": _vps_tcp_ping_snapshot(cache_ttl=0.5) if _ssh_transport_enabled() else {"available": False, "source": "ssh_not_configured"},
                "note": "A resposta HTTP comprova Phone→VPS. O painel verifica separadamente VPS→Phone.",
            }
        elif kind == "vps_assist_probe":
            result = _assist_readiness_snapshot(payload)
        elif kind == "tailscale_status":
            result = {"ok": True, "summary": "status Tailscale coletado", "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"})}
        elif kind == "worker_logs":
            result = _worker_logs_snapshot(payload)
            result.setdefault("summary", "logs do phone-worker coletadas" if result.get("ok") else "falha ao coletar logs")
        elif kind == "worker_update":
            result = _apply_worker_update(payload)
            result.setdefault("summary", "arquivos do phone-worker atualizados")
        elif kind == "boot_status":
            result = _termux_boot_status_snapshot()
            result["shell_autostart"] = _termux_shell_autostart_status_snapshot()
            result.setdefault("summary", "status do boot/shell automático coletado")
        elif kind == "boot_repair":
            result = _repair_termux_boot_script()
            result["shell_autostart"] = _repair_termux_shell_autostart()
            result["scripts"] = _script_inventory()
            result["supervisor"] = _runtime_supervisor_snapshot()
            result.setdefault("summary", "boot/shell automático reparado")
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
        elif kind in {"music_agent_status", "music_agent_command"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_music_agent_proxy(payload)
            result.setdefault("summary", "Music Agent consultado pelo worker")
        elif kind == "music_ytdlp_resolve":
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_music_ytdlp_resolve(payload)
            result.setdefault("summary", "música resolvida pelo worker")
        elif kind == "emoji_recolor":
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_emoji_recolor(payload)
            result.setdefault("summary", "emojis recoloridos pelo worker")
        elif kind in {"media_probe", "ffprobe_media"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_ffprobe_media(payload)
            result.setdefault("summary", "mídia analisada pelo worker")
        elif kind in {"audio_convert", "ffmpeg_convert"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_ffmpeg_convert(payload)
            result.setdefault("summary", "áudio convertido pelo worker")
        elif kind in {"zip_validate", "zip_audit", "log_summary", "log_digest", "text_stats", "maintenance_plan", "hash_batch", "endpoint_probe"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            if kind == "zip_validate":
                result = runner._task_zip_validate(payload)
            elif kind in {"log_summary", "log_digest"}:
                result = runner._task_log_summary(payload)
                result.setdefault("summary", "logs resumidos pelo worker")
            elif kind == "zip_audit":
                result = runner._task_zip_validate(payload)
                result.setdefault("summary", "ZIP auditado pelo worker")
            elif kind == "hash_batch":
                result = runner._task_hash_batch(payload)
            elif kind == "endpoint_probe":
                result = runner._task_endpoint_probe(payload)
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
    ok, code, data = _post_core_worker_job_result_payload_status(payload, timeout=timeout)
    if not ok:
        if _job_result_rejection_is_permanent(code, data):
            _archive_pending_core_job_result(job_id, payload, reason=f"VPS não reconhece mais este job HTTP {code}", response=data)
        else:
            _store_pending_core_job_result(payload)
    return ok


def _poll_core_worker_job_once(*, host: str, port: int, max_body_bytes: int, max_output_bytes: int, job_timeout: int, timeout: float = 8.0) -> bool:
    _base_url, _token, worker_id = _core_worker_auth_parts()
    if not worker_id:
        return False
    _flush_pending_core_worker_job_results(timeout=timeout)
    # Não aceite outro job enquanto o resultado final do anterior ainda não foi
    # confirmado pela VPS. Isso impede que um build terminado/falhado continue
    # `running` no registry enquanto worker_update/restart toma o processo.
    if _pending_core_job_result_count():
        return False
    payload = _core_worker_payload(host=host, port=port)
    request_started = time.perf_counter()
    code, data = _post_core_worker_json("/core-worker/jobs/poll", payload, timeout=timeout, respect_network_backoff=True)
    request_ms = (time.perf_counter() - request_started) * 1000.0
    if code == 0 and str(data.get("error") or "") == "network_backoff":
        return False
    if code:
        # Qualquer resposta HTTP comprova conectividade, inclusive 4xx/5xx.
        _audit_core_worker_network_success("jobs", elapsed_ms=request_ms, transport_recorded=True)
    if not (200 <= code < 300):
        _audit_core_worker_http_failure("jobs", code, data.get("error") or data, elapsed_ms=request_ms)
        return False
    _remember_core_worker_http_success("jobs")
    job = data.get("job") if isinstance(data.get("job"), dict) else None
    if not job:
        return False
    job_id = str(job.get("job_id") or "").strip()
    kind = str(job.get("type") or "").strip()
    if not job_id:
        return False
    print(f"[core-worker-jobs] executando {job_id} ({kind})", flush=True)
    _set_core_job_active(job)
    try:
        result = _execute_core_worker_job(job, max_body_bytes=max_body_bytes, max_output_bytes=max_output_bytes, job_timeout=job_timeout)
        result_ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        final_status = "succeeded" if result_ok else "failed"
        summary = str(result.get("summary") or ("ok" if result_ok else "ação falhou"))
        ok = _send_core_worker_job_result(job_id=job_id, status=final_status, result=result, error="" if result_ok else summary, timeout=timeout)
        _finish_core_job(job_id, kind, final_status, summary=summary, sent_ok=ok)
        # Self-update/restart must happen even when the result could not be sent
        # because the exact issue we are fixing is route/VPN failure during update.
        # The result is persisted and retried after restart/reconnect.
        if result_ok:
            _launch_deferred_phone_worker_action(result)
    except Exception as exc:
        summary = f"{type(exc).__name__}: {exc}"
        ok = _send_core_worker_job_result(job_id=job_id, status="failed", result={}, error=summary, timeout=timeout)
        _finish_core_job(job_id, kind, "failed", summary=summary, sent_ok=ok)
    return True


def _start_core_worker_jobs(*, host: str, port: int, max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> None:
    if not _core_worker_jobs_configured():
        print("[core-worker-jobs] desativado ou incompleto; habilite CORE_WORKER_HEARTBEAT_ENABLED/JOBS e configure URL, ID e TOKEN", flush=True)
        return
    interval = max(3.0, min(120.0, _env_float("CORE_WORKER_JOB_POLL_INTERVAL_SECONDS", DEFAULT_JOB_POLL_INTERVAL_SECONDS)))
    http_timeout = max(2.0, min(15.0, _env_float("CORE_WORKER_JOB_HTTP_TIMEOUT_SECONDS", 5.0)))

    def loop() -> None:
        was_backing_off = False
        while True:
            delay = _core_worker_network_retry_delay()
            if delay > 0:
                if not was_backing_off:
                    snapshot = _core_worker_network_runtime_snapshot()
                    print(
                        f"[core-worker-jobs] polling pausado por backoff de rede; kind={snapshot.get('last_error_kind') or 'unknown'} "
                        f"retry_in={round(delay, 1)}s streak={snapshot.get('failure_streak') or 0}",
                        flush=True,
                    )
                was_backing_off = True
                time.sleep(max(0.5, min(delay, interval)))
                continue
            if was_backing_off:
                print("[core-worker-jobs] backoff encerrado; retomando polling", flush=True)
                was_backing_off = False
            try:
                started = time.perf_counter()
                ran_job = _poll_core_worker_job_once(
                    host=host,
                    port=port,
                    max_body_bytes=max_body_bytes,
                    max_output_bytes=max_output_bytes,
                    job_timeout=job_timeout,
                    timeout=http_timeout,
                )
                time.sleep(0.5 if ran_job else interval)
            except Exception as exc:
                # O helper HTTP já atualizou o breaker compartilhado.
                _audit_core_worker_network_failure("jobs", exc, elapsed_ms=(time.perf_counter() - started) * 1000.0)
                time.sleep(max(interval, min(_core_worker_network_retry_delay(), _core_worker_network_backoff_max_seconds())))

    thread = threading.Thread(target=loop, name="core-worker-jobs", daemon=True)
    thread.start()
    print(f"[core-worker-jobs] polling ativo; intervalo={int(interval)}s", flush=True)

def main() -> int:
    # Importing the facade must not read private configuration or create tokens.
    # Keep the original precedence when starting the physical worker process.
    _load_phone_worker_runtime_env()
    _load_env_file()
    _load_persisted_pending_core_job_results()
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
    parser.add_argument("--vps-url", default=os.getenv("CORE_WORKER_VPS_URL", ""), help="URL base da VPS/Tailscale para pareamento, ex: http://100.x.x.x:10000")
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
    # Load the small pure policy before jobs/HTTP so the first synthesis does
    # not pay source-file IO. Missing modules must not block recovery startup.
    with contextlib.suppress(Exception):
        _phone_worker_tts_policy_module()
    with contextlib.suppress(Exception):
        _phone_worker_tts_cache_module()
    with contextlib.suppress(Exception):
        _phone_worker_tts_android_module()
    with contextlib.suppress(Exception):
        _phone_worker_tts_providers_module()
    with contextlib.suppress(Exception):
        _phone_worker_pcm_io_module()
    # termux-battery-status can take close to a second on Android. Keep that
    # IPC probe warm in a daemon so Teto admission only reads memory/cache.
    with contextlib.suppress(Exception):
        _start_teto_battery_monitor()
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

    # Releases imutáveis são autoritativas. Repare os launchers persistentes
    # antes de iniciar threads para que Termux:Boot/watchdogs antigos não
    # voltem a executar scripts/música de uma release anterior.
    try:
        entrypoints = _repair_runtime_entrypoint_wrappers()
        if entrypoints.get("changed"):
            print(f"[phone-worker-runtime] launchers reparados: {','.join(entrypoints['changed'])}", flush=True)
        if not entrypoints.get("ok") and entrypoints.get("errors"):
            print(f"[phone-worker-runtime] aviso reparando launchers: {entrypoints.get('errors')}", flush=True)
    except Exception as exc:
        print(f"[phone-worker-runtime] aviso reparando launchers: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)

    # Limpeza de housekeeping é feita antes de receber novos jobs. Mantemos
    # apenas os APKs recentes e nunca removemos o artifact atual durante build.
    # O control plane é exclusivamente de saída e precisa sobreviver mesmo sem
    # nenhuma porta HTTP local disponível. Inicie-o antes de tentar bind.
    _write_runtime_status(control_plane_alive=True, heartbeat_ok=None, reason="control_plane_starting")
    _ensure_edge_tts_runtime_dependency()
    _start_core_worker_heartbeat(host=args.host, port=args.port)
    _start_core_worker_jobs(
        host=args.host,
        port=args.port,
        max_body_bytes=max_body_bytes,
        max_output_bytes=max_output_bytes,
        job_timeout=job_timeout,
    )
    server = _bind_phone_worker_http_server(
        args.host,
        args.port,
        token=args.token,
        max_body_bytes=max_body_bytes,
        max_output_bytes=max_output_bytes,
        job_timeout=job_timeout,
    )
    _write_runtime_status(control_plane_alive=True, heartbeat_ok=None, reason=_DIRECT_HTTP_STATE)
    print(f"[phone-worker] control plane ativo; token_http={'sim' if args.token else 'não'}; versão={PHONE_WORKER_VERSION}; http_state={_DIRECT_HTTP_STATE}; http_port={_EFFECTIVE_HTTP_PORT}", flush=True)
    if server is not None:
        server.serve_forever()
    else:
        # Sem endpoint direto o processo principal permanece vivo; heartbeat/jobs
        # continuam em threads daemon e o supervisor usa runtime-status + PID.
        while True:
            _write_runtime_status(control_plane_alive=True, heartbeat_ok=None, reason="control_plane_only")
            time.sleep(15.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
