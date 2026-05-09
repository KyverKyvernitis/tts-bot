from __future__ import annotations

import json
import contextlib
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

from .lavalink import LavalinkConfig, _as_bool, _normalize_mode, _safe_int

logger = logging.getLogger(__name__)


DEFAULT_LAVALINK_CONFIG_PATH = Path(getattr(config, "BASE_DIR", ".")).resolve() / "data" / "music" / "lavalink_config.json"
_ALLOWED_MODES = {"off", "shadow", "lavalink", "auto"}


def _safe_float(value: object, default: float = 8.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_config() -> LavalinkConfig:
    return LavalinkConfig(
        enabled=_as_bool(getattr(config, "LAVALINK_ENABLED", False), False),
        mode=_normalize_mode(getattr(config, "LAVALINK_MODE", "off")),
        host=str(getattr(config, "LAVALINK_HOST", "") or "").strip(),
        port=max(1, _safe_int(getattr(config, "LAVALINK_PORT", 2333), 2333)),
        password=str(getattr(config, "LAVALINK_PASSWORD", "") or "").strip(),
        secure=_as_bool(getattr(config, "LAVALINK_SECURE", False), False),
        node_name=str(getattr(config, "LAVALINK_NODE_NAME", "main") or "main").strip() or "main",
        timeout_seconds=max(2.0, _safe_float(getattr(config, "LAVALINK_TIMEOUT_SECONDS", 8.0), 8.0)),
    )


def _config_from_mapping(raw: dict[str, Any], *, fallback: LavalinkConfig | None = None) -> LavalinkConfig:
    fallback = fallback or _env_config()
    node = raw.get("node") if isinstance(raw.get("node"), dict) else raw
    mode = _normalize_mode(raw.get("mode", getattr(fallback, "mode", "off")))
    if mode not in _ALLOWED_MODES:
        mode = "off"
    enabled = _as_bool(raw.get("enabled"), mode != "off")
    return LavalinkConfig(
        enabled=enabled,
        mode=mode,
        host=str(node.get("host", fallback.host) or "").strip(),
        port=max(1, _safe_int(node.get("port", fallback.port), fallback.port or 2333)),
        password=str(node.get("password", fallback.password) or "").strip(),
        secure=_as_bool(node.get("secure"), fallback.secure),
        node_name=str(node.get("name", fallback.node_name) or "main").strip() or "main",
        timeout_seconds=max(2.0, _safe_float(raw.get("timeout_seconds", fallback.timeout_seconds), fallback.timeout_seconds or 8.0)),
    )


def _config_to_payload(cfg: LavalinkConfig, *, source: str = "panel") -> dict[str, Any]:
    return {
        "version": 1,
        "source": source,
        "enabled": bool(cfg.enabled),
        "mode": _normalize_mode(cfg.mode),
        "timeout_seconds": float(cfg.timeout_seconds or 8.0),
        "updated_at": _utc_now_iso(),
        "node": {
            "name": str(cfg.node_name or "main"),
            "host": str(cfg.host or ""),
            "port": int(cfg.port or 2333),
            "password": str(cfg.password or ""),
            "secure": bool(cfg.secure),
        },
    }


class LavalinkConfigStore:
    """Configuração persistida do painel `_musicnode`.

    O arquivo é deliberadamente fora do `.env` para permitir configuração pelo
    Discord/mobile. A senha é salva localmente; por isso o caminho deve ficar no
    `.gitignore` e nunca ser exibido nas mensagens do bot.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or DEFAULT_LAVALINK_CONFIG_PATH)

    def _read_payload(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except Exception:
            logger.warning("[music/lavalink] falha ao ler config persistida do Lavalink", exc_info=True)
            return None

    def load(self) -> LavalinkConfig:
        payload = self._read_payload()
        if payload is None:
            return _env_config()
        return _config_from_mapping(payload, fallback=_env_config())

    def source_label(self) -> str:
        payload = self._read_payload()
        if payload is None:
            return "env" if _env_config().configured else "padrão"
        return str(payload.get("source") or "painel")

    def save(self, cfg: LavalinkConfig, *, source: str = "panel") -> LavalinkConfig:
        payload = _config_to_payload(cfg, source=source)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                with contextlib.suppress(Exception):
                    os.unlink(tmp_name)
        return cfg

    def update_node(
        self,
        *,
        node_name: str,
        host: str,
        port: int,
        password: str | None,
        secure: bool,
    ) -> LavalinkConfig:
        current = self.load()
        kept_password = current.password if password is None else str(password or "").strip()
        mode = current.mode if current.mode and current.mode != "off" else "shadow"
        cfg = LavalinkConfig(
            enabled=mode != "off",
            mode=mode,
            host=str(host or "").strip(),
            port=max(1, int(port or 2333)),
            password=kept_password,
            secure=bool(secure),
            node_name=str(node_name or "main").strip() or "main",
            timeout_seconds=max(2.0, float(current.timeout_seconds or 8.0)),
        )
        return self.save(cfg, source="panel")

    def set_mode(self, mode: str) -> LavalinkConfig:
        current = self.load()
        normalized = _normalize_mode(mode)
        if normalized not in _ALLOWED_MODES:
            normalized = "off"
        cfg = LavalinkConfig(
            enabled=normalized != "off",
            mode=normalized,
            host=current.host,
            port=current.port,
            password=current.password,
            secure=current.secure,
            node_name=current.node_name,
            timeout_seconds=current.timeout_seconds,
        )
        return self.save(cfg, source="panel")

    def clear(self) -> LavalinkConfig:
        cfg = LavalinkConfig(
            enabled=False,
            mode="off",
            host="",
            port=2333,
            password="",
            secure=False,
            node_name="main",
            timeout_seconds=max(2.0, _safe_float(getattr(config, "LAVALINK_TIMEOUT_SECONDS", 8.0), 8.0)),
        )
        return self.save(cfg, source="panel")

    def summary(self) -> dict[str, Any]:
        cfg = self.load()
        return {
            "source": self.source_label(),
            "enabled": bool(cfg.enabled),
            "mode": cfg.mode,
            "configured": bool(cfg.configured),
            "host_defined": bool(cfg.host),
            "port": int(cfg.port or 2333),
            "password_defined": bool(cfg.password),
            "secure": bool(cfg.secure),
            "node_name": cfg.node_name or "main",
            "config_path": str(self.path),
        }
