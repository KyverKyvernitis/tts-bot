from __future__ import annotations

import logging
from typing import Any, Mapping

from .utilitarios import _as_bool, _nested

logger = logging.getLogger(__name__)


def carregar_workers_publicos() -> list[Mapping[str, Any]]:
    """Lê exatamente o mesmo snapshot público usado pelo painel Core Workers.

    Evita manter uma segunda leitura privada do arquivo do registry, que podia
    divergir do singleton exibido pelo painel em caso de path/configuração/lock.
    Um snapshot marcado como stale ainda contém a última leitura atômica válida
    e continua útil para seleção.
    """
    try:
        from utility.commands.workers_registry import get_core_workers_registry
    except Exception:
        logger.debug("[music/worker] não consegui importar registro de workers", exc_info=True)
        return []

    try:
        snapshot = get_core_workers_registry().snapshot(lock_timeout_seconds=0.35)
        if not isinstance(snapshot, Mapping):
            return []
        raw_workers = snapshot.get("workers")
        if not isinstance(raw_workers, list):
            return []
        if snapshot.get("error"):
            logger.info(
                "[music/worker] snapshot do registry marcado como stale: %s",
                snapshot.get("error"),
            )
        return [worker for worker in raw_workers if isinstance(worker, Mapping)]
    except Exception:
        logger.debug("[music/worker] falha ao ler snapshot oficial de workers", exc_info=True)
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
