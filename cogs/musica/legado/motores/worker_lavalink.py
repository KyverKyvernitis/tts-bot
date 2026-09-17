"""Compatibilidade entre o gerenciador Lavalink legado e snapshots de workers.

Este módulo fica deliberadamente em ``legado``. A seleção/reprodução atual do
Phone Worker não depende de Lavalink; este parser existe só enquanto o backend
antigo ainda não foi removido por completo.
"""
from __future__ import annotations

from typing import Any, Mapping

from ...agente_telefone.utilitarios import _as_bool, _nested


def worker_music_summary(worker: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(worker, Mapping):
        return {"available": False, "mode": "", "state": "unknown"}
    status = worker.get("status") if isinstance(worker.get("status"), Mapping) else {}
    candidates = [
        status.get("music_node") if isinstance(status, Mapping) else None,
        status.get("lavalink") if isinstance(status, Mapping) else None,
        _nested(status, "services", "lavalink") if isinstance(status, Mapping) else None,
    ]
    node = next((item for item in candidates if isinstance(item, Mapping)), None)
    if not isinstance(node, Mapping):
        return {"available": False, "mode": "", "state": "unknown"}
    state = str(node.get("state") or "").strip().lower()
    online = _as_bool(node.get("ok"), False) or _as_bool(node.get("online"), False) or state in {
        "ok",
        "online",
        "healthy",
    }
    return {
        "available": bool(online),
        "mode": str(node.get("mode") or node.get("kind") or "lavalink").strip().lower(),
        "state": state or ("healthy" if online else "offline"),
        "host": str(node.get("host") or ""),
        "port": node.get("port"),
        "error": str(node.get("error") or ""),
    }
