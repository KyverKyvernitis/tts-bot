from __future__ import annotations

from typing import Any, Mapping

import config

def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default

def _csv(value: object) -> set[str]:
    import re

    items: set[str] = set()
    for item in re.split(r"[,;\s]+", str(value or "")):
        clean = item.strip().lower().replace("_", "-")
        clean = re.sub(r"[^a-z0-9_.:-]+", "-", clean).strip("-._:")
        if clean:
            items.add(clean)
    return items

def _version_tuple(value: object) -> tuple[int, ...]:
    import re

    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple(parts or [0])

def _version_at_least(value: object, minimum: object) -> bool:
    current = _version_tuple(value)
    wanted = _version_tuple(minimum)
    size = max(len(current), len(wanted))
    current = current + (0,) * (size - len(current))
    wanted = wanted + (0,) * (size - len(wanted))
    return current >= wanted

def _nested(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    current: Any = mapping or {}
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current

def _phone_worker_base_url() -> str:
    if not _as_bool(getattr(config, "PHONE_WORKER_ENABLED", False), False):
        return ""
    host = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not host or not token:
        return ""
    scheme = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    try:
        port = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
    except Exception:
        port = 8766
    return f"{scheme}://{host}:{port}"
