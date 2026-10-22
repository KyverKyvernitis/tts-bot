#!/usr/bin/env python3
"""Phone Worker Termux release distribution and automatic update tasks.
Core Worker Android APK builds and publication have been removed.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
import contextlib
import fcntl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from utility.commands.workers_registry import CoreWorkerRegistryError, get_core_workers_registry

ROOT = Path(__file__).resolve().parents[1]


PHONE_WORKER_FILES: tuple[tuple[str, int], ...] = (
    ("phone_worker.py", 0o755),
    ("phone_worker_runtime/__init__.py", 0o644),
    ("phone_worker_runtime/config.py", 0o644),
    ("phone_worker_runtime/telemetry.py", 0o644),
    ("phone_worker_runtime/control_plane.py", 0o644),
    ("phone_worker_runtime/voice_state.py", 0o644),
    ("phone_worker_runtime/tts_policy.py", 0o644),
    ("phone_worker_runtime/tts_cache.py", 0o644),
    ("phone_worker_runtime/tts_android.py", 0o644),
    ("phone_worker_runtime/tts_providers.py", 0o644),
    ("phone_worker_runtime/pcm_io.py", 0o644),
    ("phone_worker_runtime/ssh_transport.py", 0o644),
    ("phone_worker_bootstrap.py", 0o755),
    ("tts_transport.py", 0o644),
    ("cogs/__init__.py", 0o644),
    ("cogs/musica/__init__.py", 0o644),
    ("cogs/musica/runtime_telefone/__init__.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/__init__.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/configuracao.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/ciclo_vida.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/correspondencia.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/utilitarios.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/estado.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/efeitos.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/mixer_pcm.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/buffer_pcm.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/preparacao_audio.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/validade_stream.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/archive_manifest.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/archive_ogg.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/archive_pipeline.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/archive_staging.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/audio_segmentado.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/resolucao.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/ytdlp_quente.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/reproducao.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/tts.py", 0o644),
    ("cogs/musica/runtime_telefone/agente/servidor.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/__init__.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/configuracao.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/control_plane.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/streams.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/resolucao.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/proxy.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/telemetria.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/voz_compartilhada.py", 0o644),
    ("cogs/musica/runtime_telefone/ponte_worker/servico.py", 0o644),
    ("cogs/musica/runtime_telefone/termux/__init__.py", 0o644),
    ("cogs/musica/runtime_telefone/termux/integracao-worker.sh", 0o755),
    ("cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh", 0o755),
    ("start-phone-worker.sh", 0o755),
    ("watch-phone-worker.sh", 0o755),
    ("pair-phone-worker.sh", 0o755),
    ("teto_renderer/__init__.py", 0o644),
    ("teto_renderer/errors.py", 0o644),
    ("teto_renderer/cache.py", 0o644),
    ("teto_renderer/voicebank.py", 0o644),
    ("teto_renderer/phonemizer.py", 0o644),
    ("teto_renderer/ptbr_g2p.py", 0o644),
    ("teto_renderer/prosody.py", 0o644),
    ("teto_renderer/renderer.py", 0o644),
    ("teto_renderer/worldline.py", 0o644),
    ("teto_renderer/worldline_native.py", 0o644),
)


PHONE_WORKER_INSTALLATION_FILES = (
    ("repair-phone-worker.sh", 0o755),
    ("install.sh", 0o755),
    ("README.md", 0o644),
    ("phone-worker.env.example", 0o600),
    ("bootstrap-phone-worker.sh", 0o755),
    ("teto_renderer/OPENUTAU_LICENSE.txt", 0o644),
    ("scripts/validate-teto-assets.py", 0o755),
    ("cogs/musica/runtime_telefone/termux/musica.env.example", 0o600),
)


PHONE_WORKER_BOOTSTRAP_MIN_VERSION = "1.0.0"


PHONE_WORKER_RELEASE_MAX_MEMBERS = 64


PHONE_WORKER_RELEASE_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


PHONE_WORKER_RELEASE_MAX_EXPANDED_BYTES = 32 * 1024 * 1024


AGENT_RELEASE_ROOT = ROOT / "data" / "core_worker_agent"


PHONE_WORKER_CANONICAL_ROOT = (ROOT / "deploy" / "termux" / "phone-worker").resolve()


PHONE_WORKER_DOMAIN_SOURCE_TARGETS = frozenset({
    "cogs/__init__.py",
    "cogs/musica/__init__.py",
    "cogs/musica/runtime_telefone/__init__.py",
    "cogs/musica/runtime_telefone/agente/__init__.py",
    "cogs/musica/runtime_telefone/agente/configuracao.py",
    "cogs/musica/runtime_telefone/agente/ciclo_vida.py",
    "cogs/musica/runtime_telefone/agente/correspondencia.py",
    "cogs/musica/runtime_telefone/agente/utilitarios.py",
    "cogs/musica/runtime_telefone/agente/estado.py",
    "cogs/musica/runtime_telefone/agente/efeitos.py",
    "cogs/musica/runtime_telefone/agente/mixer_pcm.py",
    "cogs/musica/runtime_telefone/agente/buffer_pcm.py",
    "cogs/musica/runtime_telefone/agente/preparacao_audio.py",
    "cogs/musica/runtime_telefone/agente/validade_stream.py",
    "cogs/musica/runtime_telefone/agente/archive_manifest.py",
    "cogs/musica/runtime_telefone/agente/archive_ogg.py",
    "cogs/musica/runtime_telefone/agente/archive_pipeline.py",
    "cogs/musica/runtime_telefone/agente/archive_staging.py",
    "cogs/musica/runtime_telefone/agente/audio_segmentado.py",
    "cogs/musica/runtime_telefone/agente/resolucao.py",
    "cogs/musica/runtime_telefone/agente/ytdlp_quente.py",
    "cogs/musica/runtime_telefone/agente/reproducao.py",
    "cogs/musica/runtime_telefone/agente/tts.py",
    "cogs/musica/runtime_telefone/agente/servidor.py",
    "cogs/musica/runtime_telefone/ponte_worker/__init__.py",
    "cogs/musica/runtime_telefone/ponte_worker/configuracao.py",
    "cogs/musica/runtime_telefone/ponte_worker/control_plane.py",
    "cogs/musica/runtime_telefone/ponte_worker/streams.py",
    "cogs/musica/runtime_telefone/ponte_worker/resolucao.py",
    "cogs/musica/runtime_telefone/ponte_worker/proxy.py",
    "cogs/musica/runtime_telefone/ponte_worker/telemetria.py",
    "cogs/musica/runtime_telefone/ponte_worker/voz_compartilhada.py",
    "cogs/musica/runtime_telefone/ponte_worker/servico.py",
    "cogs/musica/runtime_telefone/termux/__init__.py",
    "cogs/musica/runtime_telefone/termux/integracao-worker.sh",
    "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh",
    "cogs/musica/runtime_telefone/termux/musica.env.example",
})


PHONE_WORKER_SOURCE_HASH_EXCLUDED = frozenset(name for name, _mode in PHONE_WORKER_INSTALLATION_FILES)


PHONE_WORKER_SOURCE_FILES = tuple(
    item for item in PHONE_WORKER_FILES if item[0] not in PHONE_WORKER_SOURCE_HASH_EXCLUDED
)


PENDING_PATH = ROOT / "data" / "core_worker_automation_pending.json"


STATUS_PATH = ROOT / "data" / "core_worker_automation_status.json"


STATE_PATH = ROOT / "data" / "core_worker_automation_state.json"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _short(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def _changed_files_from_env() -> list[str]:
    raw = os.getenv("CORE_WORKER_CHANGED_FILES") or ""
    items = []
    for line in raw.splitlines():
        clean = line.strip()
        if clean and clean not in items:
            items.append(clean)
    return items


def _has_changed(changed_files: Iterable[str], prefix: str) -> bool:
    return any(str(item).startswith(prefix) for item in changed_files)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _phone_worker_source_path(root: Path, name: str) -> Path:
    parts = name.split("/")
    if not name or name.startswith("/") or "\\" in name or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("caminho fora da raiz do phone-worker")
    if root.is_symlink():
        raise ValueError("link na raiz do phone-worker recusado")

    if name in PHONE_WORKER_DOMAIN_SOURCE_TARGETS:
        project_root = ROOT.resolve()
        path = project_root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"link no fonte de domínio recusado: {name}")
        if not path.resolve().is_relative_to(project_root):
            raise ValueError(f"fonte de domínio fora do projeto: {name}")
        return path

    root = root.resolve()
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"link no fonte do phone-worker recusado: {name}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"fonte fora da raiz do phone-worker: {name}")
    return path


def _hash_phone_worker_files(root: Path) -> str:
    """Hash estável do runtime instalado; documentação/exemplo não bloqueiam boot."""
    digest = hashlib.sha256()
    for name, _mode in sorted(PHONE_WORKER_SOURCE_FILES):
        path = _phone_worker_source_path(root, name)
        if not path.is_file():
            return ""
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _read_phone_worker_version() -> str:
    path = ROOT / "deploy" / "termux" / "phone-worker" / "phone_worker.py"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    m = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1) if m else "desconhecida"


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) if parts else (0,)


def _public_base_url() -> str:
    if str(os.getenv("PHONE_WORKER_TRANSPORT") or "legacy").strip().lower() == "ssh":
        # The paired origin lives on the phone, through its persistent local
        # forward. Never discover an overlay-network address in SSH mode.
        try:
            port = int(os.getenv("CORE_WORKER_SSH_LOCAL_PORT") or 19000)
        except (TypeError, ValueError):
            port = 19000
        if not 1024 <= port <= 65535:
            raise ValueError("CORE_WORKER_SSH_LOCAL_PORT inválida")
        return f"http://127.0.0.1:{port}"
    explicit = str(os.getenv("CORE_WORKER_PUBLIC_BASE_URL") or os.getenv("CORE_WORKER_VPS_URL") or os.getenv("VPS_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit and "IP_TAILSCALE_DA_VPS" not in explicit:
        return explicit
    port = str(os.getenv("CORE_WORKER_PUBLIC_PORT") or os.getenv("PORT") or "10000").strip() or "10000"
    host = str(os.getenv("CORE_WORKER_PUBLIC_HOST") or os.getenv("VPS_TAILSCALE_HOST") or "").strip()
    if not host:
        try:
            proc = subprocess.run(["tailscale", "ip", "-4"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.5, check=False)
            for line in (proc.stdout or "").splitlines():
                candidate = line.strip()
                if re.fullmatch(r"100(?:\.\d{1,3}){3}", candidate):
                    host = candidate
                    break
        except Exception:
            host = ""
    if host:
        return f"http://{host}:{port}"
    return f"http://IP_TAILSCALE_DA_VPS:{port}"


def _canonical_phone_worker_root() -> Path:
    src = _phone_worker_source_path(ROOT, "deploy/termux/phone-worker").resolve()
    if src != PHONE_WORKER_CANONICAL_ROOT:
        raise RuntimeError("raiz não canônica do phone-worker recusada")
    nested = ROOT / "tts-bot-main" / "deploy" / "termux" / "phone-worker"
    # A árvore aninhada histórica pode existir na base por compatibilidade, mas
    # nunca é fonte instalável nem entra em pacote/manifesto do agent.
    if nested.resolve() == src:
        raise RuntimeError("raiz canônica do phone-worker ambígua")
    return src


def _build_worker_update_payload(*, scripts_only: bool = False) -> dict[str, Any]:
    src = _canonical_phone_worker_root()
    targets = PHONE_WORKER_FILES if not scripts_only else tuple(
        item for item in (*PHONE_WORKER_FILES, *PHONE_WORKER_INSTALLATION_FILES) if item[0].endswith(".sh")
    )
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, mode in targets:
        path = _phone_worker_source_path(src, name)
        if not path.is_file():
            missing.append(name)
            continue
        raw = path.read_bytes()
        files.append({
            "target": name,
            "mode": mode,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        })
    if missing:
        raise RuntimeError("arquivos obrigatórios do phone-worker ausentes: " + ", ".join(missing[:8]))
    if not files:
        raise RuntimeError("nenhum arquivo do phone-worker encontrado")
    source_hash = _hash_phone_worker_files(src) if not scripts_only else ""
    return {
        "version": _read_phone_worker_version(),
        "source_hash": source_hash,
        "restart": not scripts_only,
        "scripts_only": scripts_only,
        "auto": True,
        "source": "vps-updater",
        "update_transport": "inline-b64-v1",
        "ensure_apk_builder": _env_bool("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", True),
        "files": files,
    }


def _write_deterministic_zip_member(zf: zipfile.ZipFile, name: str, raw: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (int(mode) & 0xFFFF) << 16
    zf.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{time.time_ns()}")
    raw = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _publish_phone_worker_release(inline_payload: dict[str, Any]) -> dict[str, Any]:
    """Publica o target persistente do agent sem guardar o agent no registry.

    O ZIP é imutável por source_hash e `latest.json` é trocado atomicamente.
    A VPS apenas empacota bytes; nunca executa o runtime Android/Gradle.
    """
    files = inline_payload.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("payload do phone-worker sem arquivos")
    if len(files) + 1 > PHONE_WORKER_RELEASE_MAX_MEMBERS:
        raise ValueError("release excede limite de membros do bootstrap 1.0.0")
    source_hash = str(inline_payload.get("source_hash") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise RuntimeError("source_hash inválido ao publicar release do agent")
    version = str(inline_payload.get("version") or "").strip()
    if not version:
        raise RuntimeError("versão do agent ausente")

    decoded: list[tuple[str, int, bytes, str]] = []
    members: list[dict[str, Any]] = []
    seen: set[str] = {"phone-worker-release.json"}
    expanded_bytes = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("arquivo inválido no release do agent")
        target = str(item.get("target") or "").strip().replace("\\", "/")
        if target in seen or "\x00" in target or target.startswith("/") or any(part in {"", ".", ".."} for part in target.split("/")):
            raise ValueError(f"caminho inválido/duplicado no release: {target}")
        seen.add(target)
        raw = base64.b64decode(str(item.get("data_b64") or "").encode("ascii"), validate=True)
        expanded_bytes += len(raw)
        if expanded_bytes > PHONE_WORKER_RELEASE_MAX_EXPANDED_BYTES:
            raise ValueError("release expandido excede limite do bootstrap 1.0.0")
        sha = hashlib.sha256(raw).hexdigest()
        if sha != str(item.get("sha256") or "").strip().lower():
            raise ValueError(f"sha256 divergente antes de empacotar {target}")
        mode = int(item.get("mode") or 0o644)
        decoded.append((target, mode, raw, sha))
        members.append({"path": target, "mode": mode, "bytes": len(raw), "sha256": sha})

    inner_manifest = {
        "schema": "core-phone-worker-release-v2",
        "version": version,
        "source_hash": source_hash,
        "min_bootstrap_version": PHONE_WORKER_BOOTSTRAP_MIN_VERSION,
        "members": members,
    }
    inner_raw = json.dumps(inner_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if expanded_bytes + len(inner_raw) > PHONE_WORKER_RELEASE_MAX_EXPANDED_BYTES:
        raise ValueError("release expandido excede limite do bootstrap 1.0.0")
    release_root = AGENT_RELEASE_ROOT / "releases"
    release_root.mkdir(parents=True, exist_ok=True)
    tmp = release_root / f".{source_hash}.{os.getpid()}.{time.time_ns()}.tmp"
    final = release_root / f"{source_hash}.zip"
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            _write_deterministic_zip_member(zf, "phone-worker-release.json", inner_raw, 0o644)
            for target, mode, raw, _sha in sorted(decoded):
                _write_deterministic_zip_member(zf, target, raw, mode)
        if tmp.stat().st_size > PHONE_WORKER_RELEASE_MAX_ARCHIVE_BYTES:
            raise ValueError("ZIP do release excede limite do bootstrap 1.0.0")
        archive_sha = _sha256_file(tmp)
        if final.exists():
            if _sha256_file(final) != archive_sha:
                raise RuntimeError("release imutável existente diverge do novo pacote")
        else:
            os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)

    published_at = int(time.time())
    base = _public_base_url().rstrip("/")
    latest = {
        "schema": "core-phone-worker-release-v2",
        "release_id": f"agent-{version}-{source_hash[:12]}",
        "version": version,
        "source_hash": source_hash,
        "sha256": archive_sha,
        "bytes": final.stat().st_size,
        "members": members,
        "min_bootstrap_version": PHONE_WORKER_BOOTSTRAP_MIN_VERSION,
        "published_at": published_at,
        "url": f"{base}/core-worker/agent/releases/{source_hash}.zip",
    }
    _atomic_write_json(AGENT_RELEASE_ROOT / "latest.json", latest)
    _atomic_write_json(AGENT_RELEASE_ROOT / "releases" / f"{source_hash}.json", latest)
    return latest


def _build_worker_update_artifact_payload(inline_payload: dict[str, Any]) -> dict[str, Any]:
    """Payload pequeno de entrega rápida; o target persistente é autoritativo."""
    latest = _publish_phone_worker_release(inline_payload)
    return {
        "version": latest["version"],
        "source_hash": latest["source_hash"],
        "restart": bool(inline_payload.get("restart", True)),
        "auto": bool(inline_payload.get("auto", True)),
        "source": inline_payload.get("source") or "vps-updater",
        "update_transport": "bootstrap-manifest-v2",
        "bootstrap_manifest": True,
        "manifest_url": f"{_public_base_url().rstrip('/')}/core-worker/agent/latest",
        "release_url": latest["url"],
        "release_sha256": latest["sha256"],
        "release_bytes": latest["bytes"],
        "min_bootstrap_version": latest["min_bootstrap_version"],
    }


def _load_registry_snapshot() -> dict[str, Any]:
    try:
        timeout = float(os.getenv("CORE_WORKER_AUTOMATION_REGISTRY_LOCK_TIMEOUT_SECONDS", "0.25") or 0.25)
    except Exception:
        timeout = 0.25
    try:
        return get_core_workers_registry().snapshot(lock_timeout_seconds=max(0.0, min(3.0, timeout)))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "workers": [], "jobs": []}


def _load_pending() -> dict[str, Any]:
    if not PENDING_PATH.exists():
        return {}
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_pending(data: dict[str, Any]) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in (data or {}).items() if v}
    if not clean:
        with contextlib.suppress(Exception):
            PENDING_PATH.unlink()
        return
    tmp = PENDING_PATH.with_suffix(PENDING_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(PENDING_PATH)


def _worker_source_hash(worker: dict[str, Any] | None) -> str:
    if not isinstance(worker, dict):
        return ""
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    for value in (
        worker.get("source_hash"),
        status.get("source_hash"),
        status.get("phone_worker_source_hash"),
        health.get("source_hash"),
    ):
        clean = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", clean):
            return clean
    return ""


def _worker_needs_agent_update(
    worker: dict[str, Any] | None,
    target_version: str,
    target_source_hash: str = "",
    *,
    force: bool = False,
) -> bool:
    if not isinstance(worker, dict):
        return False
    expected_hash = str(target_source_hash or "").strip().lower()
    current_hash = _worker_source_hash(worker)
    current_version = str(worker.get("version") or "")
    # Nunca faça downgrade automático: force significa reaplicar a fonte da
    # mesma versão, não substituir um agent que já está adiante do servidor.
    if current_version and _version_tuple(current_version) > _version_tuple(target_version):
        return False
    if not current_version or _version_tuple(current_version) < _version_tuple(target_version):
        return True
    # A partir do target que implementa hash, ausência também significa estado
    # incompleto/desconhecido. Isso mantém o segundo salto do bootstrap pendente
    # mesmo se o resultado antigo já tiver gravado a nova versão no registry.
    if expected_hash:
        return current_hash != expected_hash
    return bool(force)


def _worker_has_recovery_bootstrap(worker: dict[str, Any] | None) -> bool:
    """Confirma que o runtime já possui o bootstrap pull independente.

    Agents legados só aceitavam ``phone_worker.py`` na allowlist. Eles não
    podem receber o novo bootstrap com segurança pelo próprio protocolo antigo;
    a migração inicial usa ``repair-phone-worker.sh`` uma única vez.
    """
    if not isinstance(worker, dict):
        return False
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    update_status = status.get("worker_update") if isinstance(status.get("worker_update"), dict) else {}
    updater = update_status.get("updater") if isinstance(update_status.get("updater"), dict) else {}
    bootstrap_version = str(updater.get("bootstrap_version") or "").strip()
    declared = (
        worker.get("worker_update_transports")
        or status.get("worker_update_transports")
        or update_status.get("transports")
        or []
    )
    transports = _task_set(declared)
    return bool(
        "bootstrap_manifest_v2" in transports
        and bootstrap_version
        and _version_tuple(bootstrap_version) >= _version_tuple(PHONE_WORKER_BOOTSTRAP_MIN_VERSION)
    )


def _workers_need_agent_version(
    snapshot: dict[str, Any],
    target_version: str,
    target_source_hash: str = "",
    *,
    force: bool = False,
) -> bool:
    """Retorna True só para workers ativos/online fora da versão/fonte alvo.

    Workers antigos offline não devem manter o painel preso em "agent pendente".
    Quando um celular voltar online, o heartbeat/process-pending roda de novo e
    cria o update se a versão real ainda estiver antiga.
    """
    if not str(target_version or "").strip():
        return False
    workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict) or not worker.get("online") or worker.get("enabled") is False:
            continue
        if not _is_termux_bootstrap_worker(worker):
            continue
        if not _worker_supports(worker, "worker_update", "phone-worker"):
            continue
        if _worker_needs_agent_update(worker, target_version, target_source_hash, force=force):
            return True
    return False


def _registered_workers_need_agent_version(
    snapshot: dict[str, Any],
    target_version: str,
    target_source_hash: str = "",
    *,
    force: bool = False,
) -> bool:
    """Mantém a pendência para um Termux offline que ainda precisa migrar."""
    workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict) or worker.get("enabled") is False:
            continue
        if not _is_termux_bootstrap_worker(worker):
            continue
        roles = {str(item).strip().lower() for item in worker.get("roles") or []}
        caps = roles | {str(item).strip().lower() for item in worker.get("capabilities") or []}
        tasks = {str(item).replace("-", "_") for item in worker.get("supported_tasks") or []}
        if "phone-worker" not in caps or (tasks and "worker_update" not in tasks):
            continue
        if _worker_needs_agent_update(worker, target_version, target_source_hash, force=force):
            return True
    return False


def _registry_raw() -> dict[str, Any]:
    try:
        path = get_core_workers_registry().path
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _active_job_exists(*, job_type: str, target_worker_id: str = "", summary_contains: str = "") -> bool:
    # snapshot() limpa leases expirados antes da leitura. Se o lock estiver ocupado,
    # caímos no JSON cru, mas ainda ignoramos jobs obviamente velhos. Isso evita
    # build APK ficar travado por horas quando o phone worker caiu depois do Gradle.
    with contextlib.suppress(Exception):
        get_core_workers_registry().snapshot(lock_timeout_seconds=0.4)
    data = _registry_raw()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    wanted = str(job_type or "").replace("-", "_")
    target_worker_id = str(target_worker_id or "")
    summary_contains = str(summary_contains or "")
    now = time.time()
    apk_running_grace = max(300, int(os.getenv("CORE_WORKER_APK_BUILD_STALE_RUNNING_SECONDS", "1500") or 1500))
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "queued")
        if status not in {"queued", "running"}:
            continue
        kind = str(job.get("type") or "").replace("-", "_")
        if kind != wanted:
            continue
        expires_at = float(job.get("expires_at") or 0.0)
        lease_until = float(job.get("lease_until") or 0.0)
        updated_at = float(job.get("updated_at") or job.get("started_at") or job.get("created_at") or 0.0)
        if expires_at and expires_at <= now:
            continue
        if status == "running" and lease_until and lease_until <= now:
            continue
        if kind == "apk_build_debug" and status == "running" and updated_at and now - updated_at > apk_running_grace:
            continue
        if target_worker_id and str(job.get("target_worker_id") or job.get("worker_id") or "") not in {target_worker_id, ""}:
            continue
        if summary_contains and summary_contains not in str(job.get("summary") or ""):
            continue
        return True
    return False


def _worker_declares_support(worker: dict[str, Any], task: str, required_capability: str = "phone-worker") -> bool:
    roles = {str(item) for item in worker.get("roles") or []}
    caps = {str(item) for item in worker.get("capabilities") or []} | roles
    tasks = {str(item).replace("-", "_") for item in worker.get("supported_tasks") or []}
    if required_capability and required_capability not in caps:
        return False
    return not tasks or task in tasks


def _worker_supports(worker: dict[str, Any], task: str, required_capability: str = "phone-worker") -> bool:
    if not worker.get("online"):
        return False
    return _worker_declares_support(worker, task, required_capability)


def _worker_recent_update_delivery_matches(
    worker: dict[str, Any], target_version: str, target_source_hash: str, *, cooldown_seconds: float = 300.0
) -> bool:
    try:
        delivered_at = float(worker.get("updater_last_delivery_at") or 0.0)
    except (TypeError, ValueError):
        delivered_at = 0.0
    if delivered_at <= 0 or (time.time() - delivered_at) > max(30.0, float(cooldown_seconds)):
        return False
    delivered_version = str(worker.get("updater_last_delivery_target_version") or "").strip()
    delivered_hash = str(worker.get("updater_last_delivery_target_hash") or "").strip().lower()
    wanted_hash = str(target_source_hash or "").strip().lower()
    return delivered_version == str(target_version or "").strip() and (not wanted_hash or delivered_hash == wanted_hash)


def _is_termux_bootstrap_worker(worker: dict[str, Any]) -> bool:
    source = str(worker.get("source") or "").strip().lower()
    runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
    runtime_mode = str(worker.get("runtime_mode") or "").strip().lower()
    roles = {str(item).strip().lower() for item in worker.get("roles") or []}
    return (
        runtime_kind == "termux"
        or source.startswith("termux-")
        or "termux" in source
        or runtime_mode == "termux"
        or ("phone-worker" in roles and not source.startswith("core-worker-apk"))
    )


def _bootstrap_worker_id_for_runtime(snapshot: dict[str, Any], worker_id: str) -> str:
    """Mapeia o runtime APK filho de volta ao Termux do mesmo celular."""
    wanted = str(worker_id or "").strip()
    if not wanted:
        return ""
    workers = [item for item in snapshot.get("workers") or [] if isinstance(item, dict)]
    selected = next((item for item in workers if str(item.get("worker_id") or "") == wanted), None)
    if isinstance(selected, dict):
        parent = str(selected.get("parent_worker_id") or selected.get("physical_worker_id") or "").strip()
        if parent:
            parent_record = next((item for item in workers if str(item.get("worker_id") or "") == parent), None)
            if isinstance(parent_record, dict) and _is_termux_bootstrap_worker(parent_record):
                return parent
        if _is_termux_bootstrap_worker(selected):
            return wanted
    return wanted


def _task_set(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.replace(';', ',').split(',')
    else:
        raw_items = []
    result: set[str] = set()
    for item in raw_items:
        clean = re.sub(r"[^a-z0-9_]+", "_", str(item or "").strip().lower().replace('-', '_')).strip('_')
        if clean:
            result.add(clean)
    return result


def _direct_phone_worker_config() -> dict[str, str]:
    enabled = _env_bool("PHONE_WORKER_ENABLED", True)
    host = str(os.getenv("PHONE_WORKER_HOST") or os.getenv("CORE_WORKER_PHONE_HOST") or "").strip()
    port = str(os.getenv("PHONE_WORKER_PORT") or "8766").strip() or "8766"
    scheme = str(os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
    token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
    return {"enabled": "1" if enabled else "0", "host": host, "port": port, "scheme": scheme, "token": token}


def _direct_phone_worker_request(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    cfg = _direct_phone_worker_config()
    if cfg["enabled"] != "1" or not cfg["host"]:
        return {"ok": False, "skipped": True, "summary": "phone-worker direto não configurado"}
    url = f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}{path}"
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=max(0.8, timeout)) as resp:
            raw = resp.read()
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        return parsed if isinstance(parsed, dict) else {"ok": False, "summary": "resposta direta não é JSON object"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        return {"ok": False, "status": exc.code, "summary": f"HTTP {exc.code}: {_short(body, 160)}"}
    except Exception as exc:
        return {"ok": False, "summary": f"{type(exc).__name__}: {_short(exc, 160)}"}


def _direct_phone_worker_update_if_needed(payload: dict[str, Any], target_version: str, *, force: bool = False) -> dict[str, Any]:
    status = _direct_phone_worker_request("/status", timeout=2.5)
    if not bool(status.get("ok", True)):
        return {"ok": False, "skipped": True, "summary": f"phone-worker direto indisponível: {_short(status.get('summary') or status.get('error'), 160)}"}
    source = str(status.get("source") or "").strip().lower()
    runtime_mode = str(status.get("runtime_mode") or "").strip().lower()
    if source.startswith("core-worker-apk") or runtime_mode.startswith("apk-"):
        return {
            "ok": False,
            "skipped": True,
            "port_conflict": True,
            "summary": "porta direta responde ao APK; worker_update continua reservado ao Termux bootstrap",
        }
    current_version = str(status.get("version") or "")
    worker_id = str(status.get("worker_id") or "").strip()
    target_source_hash = str(payload.get("source_hash") or "").strip().lower()
    current_source_hash = _worker_source_hash(status)
    supported = _task_set(status.get("supported_tasks"))
    if supported and "worker_update" not in supported:
        return {"ok": False, "skipped": True, "current_version": current_version, "summary": "phone-worker direto não declara worker_update"}
    if not _worker_needs_agent_update(status, target_version, target_source_hash, force=force):
        return {
            "ok": True,
            "skipped": True,
            "current_version": current_version,
            "current_source_hash": current_source_hash,
            "worker_id": worker_id,
            "target_version": target_version,
            "target_source_hash": target_source_hash,
            "summary": f"phone-worker direto já está em {current_version} com a fonte esperada",
        }
    if not _worker_has_recovery_bootstrap(status):
        return {
            "ok": False,
            "skipped": True,
            "bootstrap_required": True,
            "current_version": current_version,
            "worker_id": worker_id,
            "summary": "bootstrap_required: execute repair-phone-worker.sh uma vez",
        }
    body = _build_worker_update_artifact_payload(payload)
    body["task"] = "worker_update"
    body.setdefault("source", "vps-updater-direct")
    result = _direct_phone_worker_request("/task", payload=body, timeout=45.0)
    result.setdefault("current_version", current_version)
    result.setdefault("current_source_hash", current_source_hash)
    result.setdefault("worker_id", worker_id)
    result.setdefault("target_version", target_version)
    result.setdefault("target_source_hash", target_source_hash)
    if result.get("ok") is False:
        result.setdefault("summary", "update direto falhou")
    else:
        result.setdefault("summary", f"update direto enviado para {target_version}")
    return result


def _worker_needs_boot_repair(worker: dict[str, Any]) -> bool:
    if not worker.get("online"):
        return False
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    boot = status.get("boot") if isinstance(status.get("boot"), dict) else {}
    if not boot and isinstance(health.get("boot"), dict):
        boot = health.get("boot")
    if boot and boot.get("ok") is False:
        return True
    if not boot and health.get("boot_ok") is False:
        return True
    scripts = status.get("scripts") if isinstance(status.get("scripts"), dict) else {}
    installs = scripts.get("installations") if isinstance(scripts.get("installations"), dict) else {}
    if installs.get("has_active_duplicates"):
        return True
    return False


def queue_boot_repairs(*, only_worker_id: str = "") -> dict[str, Any]:
    if not _env_bool("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", True):
        return {"ok": True, "skipped": "termux_bootstrap_builder_disabled", "queued": 0, "workers": []}
    registry = get_core_workers_registry()
    snapshot = _load_registry_snapshot()
    workers = [w for w in snapshot.get("workers") or [] if isinstance(w, dict)]
    queued: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id)
        if not worker_id:
            continue
        if only_worker_id and worker_id != only_worker_id:
            continue
        if not _worker_needs_boot_repair(worker):
            skipped.append(f"{name}: boot ok")
            continue
        if not _worker_supports(worker, "boot_repair", "phone-worker"):
            skipped.append(f"{name}: sem suporte/offline")
            continue
        if _active_job_exists(job_type="boot_repair", target_worker_id=worker_id):
            skipped.append(f"{name}: boot_repair já pendente")
            continue
        try:
            result = registry.create_job(
                job_type="boot_repair",
                payload={"auto": True, "source": "vps-updater", "reason": "boot incompleto ou duplicata ativa"},
                created_by_id=0,
                created_by_name="VPS updater",
                target_worker_id=worker_id,
                required_capabilities=["phone-worker"],
                ttl_seconds=900,
                lease_seconds=120,
                max_attempts=2,
                summary="auto-repair boot Core Worker",
            )
            job = result.get("job") if isinstance(result, dict) else {}
            queued.append(f"{name}:{job.get('job_id') or 'job'}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {_short(exc, 120)}")
    return {"ok": True, "queued": queued, "skipped": skipped[:16], "errors": errors[:10]}


def queue_agent_updates(*, force: bool = False, only_worker_id: str = "") -> dict[str, Any]:
    if not _env_bool("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", True):
        pending = _load_pending()
        pending.pop("agent_update", None)
        _save_pending(pending)
        return {"ok": True, "skipped": "termux_bootstrap_builder_disabled", "queued": 0, "workers": []}
    registry = get_core_workers_registry()
    snapshot = _load_registry_snapshot()
    workers = [w for w in snapshot.get("workers") or [] if isinstance(w, dict)]
    requested_worker_id = str(only_worker_id or "").strip()
    bootstrap_worker_id = _bootstrap_worker_id_for_runtime(snapshot, requested_worker_id) if requested_worker_id else ""
    payload = _build_worker_update_payload()
    # O target persistente é publicado antes de olhar workers/jobs. Assim um
    # telefone offline durante o deploy sempre encontra a fonte correta ao voltar.
    published_release = _publish_phone_worker_release(payload)
    target_version = str(payload.get("version") or "desconhecida")
    target_source_hash = str(payload.get("source_hash") or "").strip().lower()
    artifact_payload: dict[str, Any] | None = None

    def payload_for(worker: dict[str, Any]) -> dict[str, Any]:
        nonlocal artifact_payload
        if not _worker_has_recovery_bootstrap(worker):
            raise RuntimeError("bootstrap_required: execute repair-phone-worker.sh uma vez")
        if artifact_payload is None:
            artifact_payload = {
                "version": published_release["version"],
                "source_hash": published_release["source_hash"],
                "restart": bool(payload.get("restart", True)),
                "auto": bool(payload.get("auto", True)),
                "source": payload.get("source") or "vps-updater",
                "update_transport": "bootstrap-manifest-v2",
                "bootstrap_manifest": True,
                "manifest_url": f"{_public_base_url().rstrip('/')}/core-worker/agent/latest",
                "release_url": published_release["url"],
                "release_sha256": published_release["sha256"],
                "release_bytes": published_release["bytes"],
                "min_bootstrap_version": published_release["min_bootstrap_version"],
            }
        return artifact_payload

    direct_update = {}
    direct_target = next((w for w in workers if str(w.get("worker_id") or "") == bootstrap_worker_id), None) if bootstrap_worker_id else None
    if not requested_worker_id or (isinstance(direct_target, dict) and _is_termux_bootstrap_worker(direct_target)):
        direct_update = _direct_phone_worker_update_if_needed(payload, target_version, force=force)

    pending = _load_pending()
    pending["agent_update"] = {
        "type": "worker_update",
        "target_version": target_version,
        "target_source_hash": target_source_hash,
        "pending": True,
        "force_same_version": bool(force),
        "created_at": float((pending.get("agent_update") or {}).get("created_at") or time.time()) if isinstance(pending.get("agent_update"), dict) else time.time(),
        "updated_at": time.time(),
        "message": "agent update pendente; será aplicado quando workers compatíveis aparecerem",
    }
    _save_pending(pending)

    queued: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id)
        if not worker_id:
            continue
        if bootstrap_worker_id and worker_id != bootstrap_worker_id:
            continue
        if not _is_termux_bootstrap_worker(worker):
            skipped.append(f"{name}: runtime APK não recebe worker_update")
            continue
        if not _worker_has_recovery_bootstrap(worker):
            skipped.append(f"{name}: bootstrap_required; execute repair-phone-worker.sh uma vez")
            continue
        if not _worker_supports(worker, "worker_update", "phone-worker"):
            skipped.append(f"{name}: incompatível/offline")
            continue
        current_version = str(worker.get("version") or "")
        if not _worker_needs_agent_update(worker, target_version, target_source_hash, force=force):
            skipped.append(f"{name}: já em {current_version} com a fonte esperada")
            continue
        if not force and _worker_recent_update_delivery_matches(worker, target_version, target_source_hash):
            skipped.append(f"{name}: update entregue; aguardando heartbeat confirmar runtime")
            continue
        direct_worker_id = str(direct_update.get("worker_id") or "").strip()
        if (
            bool(direct_update.get("ok"))
            and not bool(direct_update.get("skipped"))
            and direct_worker_id == worker_id
        ):
            skipped.append(f"{name}: update direto enviado")
            continue
        if _active_job_exists(job_type="worker_update", target_worker_id=worker_id, summary_contains=target_version):
            skipped.append(f"{name}: job já pendente")
            continue
        try:
            result = registry.create_job(
                job_type="worker_update",
                payload=payload_for(worker),
                created_by_id=0,
                created_by_name="VPS updater",
                target_worker_id=worker_id,
                required_capabilities=["phone-worker"],
                ttl_seconds=1800,
                lease_seconds=240,
                max_attempts=2,
                summary=f"auto-update agent {target_version}",
            )
            job = result.get("job") if isinstance(result, dict) else {}
            queued.append(f"{name}:{job.get('job_id') or 'job'}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {_short(exc, 120)}")
    # Só limpe quando nenhum Termux registrado precisar da fonte. Um celular
    # offline mantém a pendência sem job; ao voltar, o primeiro poll retoma o
    # bootstrap em vez de depender do scan ocioso de 15 minutos.
    refreshed_snapshot = _load_registry_snapshot()
    direct_sent = bool(direct_update.get("ok")) and not bool(direct_update.get("skipped"))
    current_pending = _load_pending()
    pending_item = current_pending.get("agent_update") if isinstance(current_pending.get("agent_update"), dict) else {}
    if pending_item:
        bootstrap_required = bool(direct_update.get("bootstrap_required")) or any("bootstrap_required" in item for item in skipped)
        pending_item["phase"] = (
            "waiting_device"
            if bootstrap_required
            else (
                "job_queued"
                if queued or direct_sent or any("job já pendente" in item for item in skipped)
                else "waiting_worker"
            )
        )
        if bootstrap_required:
            pending_item["requires_manual_bootstrap"] = True
            pending_item["block_reason"] = "initial_bootstrap_required"
            pending_item["message"] = "device requer o resgate inicial por repair-phone-worker.sh; depois o pull automático assume"
        else:
            pending_item.pop("requires_manual_bootstrap", None)
            pending_item.pop("block_reason", None)
        pending_item["updated_at"] = time.time()
        current_pending["agent_update"] = pending_item
        _save_pending(current_pending)
    if (
        not queued
        and not errors
        and not _registered_workers_need_agent_version(
            refreshed_snapshot,
            target_version,
            target_source_hash,
            force=force,
        )
    ):
        pending = _load_pending()
        pending.pop("agent_update", None)
        _save_pending(pending)
        return {"ok": True, "target_version": target_version, "target_source_hash": target_source_hash, "queued": [], "skipped": skipped[:16], "errors": [], "pending": False, "direct_update": direct_update, "message": "todos os agents ativos já estão atualizados"}
    return {"ok": True, "target_version": target_version, "target_source_hash": target_source_hash, "queued": queued, "skipped": skipped[:16], "errors": errors[:10], "pending": True, "direct_update": direct_update}


def _automation_time_budget_seconds() -> float:
    try:
        return max(5.0, min(180.0, float(os.getenv("CORE_WORKER_AUTOMATION_TIME_BUDGET_SECONDS", "45") or 45)))
    except Exception:
        return 45.0


def _budget_exceeded(started: float) -> bool:
    return (time.monotonic() - started) >= _automation_time_budget_seconds()


def write_status(status: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def process_pending(*, worker_id: str = "") -> dict[str, Any]:
    started = time.monotonic()
    pending = _load_pending()
    if "apk_build" in pending:
        pending.pop("apk_build", None)
        _save_pending(pending)
    result: dict[str, Any] = {"ok": True, "worker_id": worker_id, "processed_at": time.time()}
    snapshot = _load_registry_snapshot()
    if _env_bool("CORE_WORKER_AUTO_BOOT_REPAIR_ON_PENDING", False) and not _budget_exceeded(started):
        result["boot_repair"] = queue_boot_repairs(only_worker_id=worker_id)
    else:
        result["boot_repair"] = {"ok": True, "skipped": "disabled_by_default"}
    current_version = _read_phone_worker_version()
    current_hash = _hash_phone_worker_files(ROOT / "deploy" / "termux" / "phone-worker")
    agent_pending = pending.get("agent_update") if isinstance(pending.get("agent_update"), dict) else {}
    force = bool(agent_pending.get("force_same_version"))
    needed = False if _budget_exceeded(started) else _workers_need_agent_version(snapshot, current_version, current_hash, force=force)
    if not _budget_exceeded(started) and (agent_pending or needed):
        result["agent_update"] = queue_agent_updates(force=force, only_worker_id=worker_id)
    elif agent_pending or needed:
        result["agent_update"] = {"ok": True, "skipped": "time_budget_exceeded"}
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    write_status({"process_pending": result, "pending": _load_pending(), "finished_at": time.time()})
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return result

def after_update(force_agent: bool = False) -> int:
    changed = _changed_files_from_env()
    snapshot = _load_registry_snapshot()
    current = {
        "phone_worker_version": _read_phone_worker_version(),
        "phone_worker_hash": _hash_phone_worker_files(ROOT / "deploy" / "termux" / "phone-worker"),
    }
    previous = _load_state()
    phone_source_changed = (
        _has_changed(changed, "deploy/termux/phone-worker/")
        or any(path in PHONE_WORKER_DOMAIN_SOURCE_TARGETS for path in changed)
        or force_agent
        or bool(previous.get("phone_worker_hash") and previous.get("phone_worker_hash") != current["phone_worker_hash"])
    )
    workers_need_agent = _workers_need_agent_version(snapshot, current["phone_worker_version"], current["phone_worker_hash"])
    phone_changed = bool(phone_source_changed or workers_need_agent)
    status = {"ok": True, "changed_files": changed[:80], "fingerprints": current,
              "phone_worker_changed": phone_changed, "workers_need_agent": workers_need_agent,
              "started_at": time.time(), "base_url": _public_base_url()}
    if phone_changed and _env_bool("CORE_WORKER_AUTO_AGENT_UPDATE_ENABLED", True):
        status["agent_update"] = queue_agent_updates(force=bool(force_agent or phone_source_changed))
    elif phone_changed:
        status["agent_update"] = {"ok": True, "skipped": ["agent update disabled"]}
    status["boot_repair"] = queue_boot_repairs()
    status["failed_components"] = [key for key in ("agent_update", "boot_repair")
                                   if isinstance(status.get(key), dict) and status[key].get("ok") is False
                                   and not status[key].get("pending")]
    status["ok"] = not status["failed_components"]
    status["finished_at"] = time.time()
    write_status(status)
    _save_state({**current, "updated_at": status["finished_at"]})
    pending = _load_pending()
    if "apk_build" in pending:
        pending.pop("apk_build", None)
        _save_pending(pending)
    print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
    return 0 if status["ok"] else 2

def main() -> int:
    parser = argparse.ArgumentParser(description="Automação pós-update do Phone Worker Termux.")
    sub = parser.add_subparsers(dest="command", required=True)
    after = sub.add_parser("after-update")
    after.add_argument("--force-agent", action="store_true")
    agent = sub.add_parser("queue-agent-update")
    agent.add_argument("--worker-id", default="")
    sub.add_parser("queue-boot-repair")
    process = sub.add_parser("process-pending")
    process.add_argument("--worker-id", default="")
    args = parser.parse_args()
    if args.command == "after-update":
        return after_update(force_agent=args.force_agent)
    if args.command == "queue-agent-update":
        result = queue_agent_updates(force=True, only_worker_id=args.worker_id)
    elif args.command == "queue-boot-repair":
        result = queue_boot_repairs()
    else:
        result = process_pending(worker_id=args.worker_id)
    if args.command != "process-pending":
        write_status({"manual": True, args.command.replace("-", "_"): result, "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", False) else 2

if __name__ == "__main__":
    raise SystemExit(main())
