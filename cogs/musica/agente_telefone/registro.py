from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from .utilitarios import _as_bool, _nested

logger = logging.getLogger(__name__)


def carregar_workers_publicos() -> list[Mapping[str, Any]]:
    try:
        from utility.commands.workers_registry import (
            CoreWorkersRegistry,
            _compact_worker_public,
            _public_worker_sort_key,
        )
    except Exception:
        logger.debug("[music/worker] não consegui importar registro de workers", exc_info=True)
        return []

    try:
        registry = CoreWorkersRegistry()
        with registry._lock:  # type: ignore[attr-defined]
            data = registry._load_unlocked()  # type: ignore[attr-defined]
            raw_workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            now = time.time()
            workers = [
                _compact_worker_public(record, now=now)
                for record in raw_workers.values()
                if isinstance(record, Mapping)
            ]
        workers.sort(key=_public_worker_sort_key)
        return workers
    except Exception:
        logger.debug("[music/worker] falha ao ler registro de workers", exc_info=True)
        return []


def perfil_worker(worker: Mapping[str, Any]) -> str:
    for value in (
        worker.get("profile"),
        _nested(worker, "status", "profile"),
        _nested(worker, "status", "runtime", "profile"),
        _nested(worker, "health", "profile"),
    ):
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def worker_e_turbo(worker: Mapping[str, Any], papeis_capacidades: set[str]) -> bool:
    if perfil_worker(worker) == "turbo":
        return True
    if "turbo" in papeis_capacidades or "turbo-worker" in papeis_capacidades:
        return True
    turbo_deps = _nested(worker, "status", "turbo_dependencies")
    return isinstance(turbo_deps, Mapping) and _as_bool(turbo_deps.get("turbo"), False)
