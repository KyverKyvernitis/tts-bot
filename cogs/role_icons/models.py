from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

MAX_CONNECTIONS_PER_GUILD = 10
ROLE_ICON_UPDATE_DELAY_SECONDS = 10.0
DEFAULT_CONFIG: dict[str, Any] = {
    "connections": [],
    "revision": 0,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_hex(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not raw.startswith("#"):
        raw = f"#{raw}"
    if len(raw) != 7:
        return None
    try:
        int(raw[1:], 16)
    except Exception:
        return None
    return raw.lower()


def sanitize_connection(raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        user_id = int(raw.get("user_id") or 0)
        role_id = int(raw.get("role_id") or 0)
    except Exception:
        return None
    if role_id <= 0:
        return None
    connection_id = str(raw.get("id") or str(role_id))[:80]
    original_path = str(raw.get("original_icon_path") or "")[:500]
    return {
        "id": connection_id,
        "user_id": max(0, user_id),
        "role_id": role_id,
        "enabled": bool(raw.get("enabled", True)),
        "original_icon_path": original_path,
        "original_icon_hash": str(raw.get("original_icon_hash") or "")[:96],
        "last_color": normalize_hex(raw.get("last_color")) or "",
        "last_rendered_hash": str(raw.get("last_rendered_hash") or "")[:96],
        "last_status": str(raw.get("last_status") or "")[:220],
        "last_updated_at": str(raw.get("last_updated_at") or "")[:80],
        "created_at": str(raw.get("created_at") or now_iso())[:80],
        "updated_at": str(raw.get("updated_at") or now_iso())[:80],
    }


def sanitize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    try:
        cfg["revision"] = max(0, int(raw.get("revision") or 0))
    except Exception:
        cfg["revision"] = 0
    connections: list[dict[str, Any]] = []
    seen_roles: set[int] = set()
    for item in raw.get("connections") or []:
        if not isinstance(item, dict):
            continue
        conn = sanitize_connection(item)
        if not conn:
            continue
        if int(conn["role_id"]) in seen_roles:
            continue
        seen_roles.add(int(conn["role_id"]))
        connections.append(conn)
        if len(connections) >= MAX_CONNECTIONS_PER_GUILD:
            break
    cfg["connections"] = connections
    return cfg
