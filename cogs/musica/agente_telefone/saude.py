from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Mapping

import config

from .configuracao import ConfiguracaoSelecaoWorker, carregar_configuracao_selecao
from .utilitarios import _as_bool, _nested, _version_at_least

logger = logging.getLogger(__name__)


def _worker_music_agent(worker: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(worker, Mapping):
        return None
    status = worker.get("status") if isinstance(worker.get("status"), Mapping) else {}
    candidates = [
        status.get("music_agent") if isinstance(status, Mapping) else None,
        _nested(status, "services", "music_agent") if isinstance(status, Mapping) else None,
        worker.get("music_agent"),
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _sessoes_ativas_agente(agent: Mapping[str, Any] | None) -> int:
    if not isinstance(agent, Mapping):
        return 0
    guilds = agent.get("guilds") if isinstance(agent.get("guilds"), Mapping) else {}
    return sum(
        1
        for state in guilds.values()
        if isinstance(state, Mapping)
        and str(state.get("status") or "").strip().lower()
        in {"preparing", "starting", "playing", "paused", "queued"}
    )


def resumo_agente_musica(
    worker: Mapping[str, Any] | None,
    *,
    configuracao: ConfiguracaoSelecaoWorker | None = None,
) -> dict[str, Any]:
    cfg = configuracao or carregar_configuracao_selecao()
    agent = _worker_music_agent(worker)
    if not isinstance(agent, Mapping):
        return {
            "available": False,
            "reason": "music_agent_missing",
            "version": "",
            "active_sessions": 0,
            "max_sessions": cfg.maximo_sessoes,
        }

    version = str(agent.get("version") or agent.get("file_version") or "").strip()
    active_sessions = _sessoes_ativas_agente(agent)
    ok = _as_bool(agent.get("ok"), False) or _as_bool(agent.get("available"), False)
    ready = ok and _as_bool(agent.get("discord_ready"), False)
    deps = agent.get("voice_dependencies") if isinstance(agent.get("voice_dependencies"), Mapping) else {}
    missing_raw = agent.get("dependency_missing") or (deps.get("missing") if isinstance(deps, Mapping) else [])
    if isinstance(missing_raw, str):
        missing_deps = [missing_raw]
    elif isinstance(missing_raw, (list, tuple, set)):
        missing_deps = list(missing_raw)
    else:
        missing_deps = []

    common = {
        "version": version,
        "active_sessions": active_sessions,
        "max_sessions": cfg.maximo_sessoes,
    }
    if version and not _version_at_least(version, cfg.versao_minima_agente):
        return {
            "available": False,
            "reason": f"music_agent_old:{version}<{cfg.versao_minima_agente}",
            **common,
        }
    if missing_deps:
        return {
            "available": False,
            "reason": "dependency_missing:" + ",".join(str(x) for x in missing_deps[:4]),
            **common,
        }
    if not ready:
        return {"available": False, "reason": "music_agent_not_ready", **common}
    if active_sessions >= cfg.maximo_sessoes:
        return {"available": False, "reason": "music_agent_full", **common}
    return {"available": True, "reason": "ok", **common}


def bootstrap_agente_permitido(
    reason: object = "",
    *,
    configuracao: ConfiguracaoSelecaoWorker | None = None,
) -> bool:
    cfg = configuracao or carregar_configuracao_selecao()
    if not cfg.bootstrap_ao_tocar:
        return False
    return "music_agent_full" not in str(reason or "").lower()


def consultar_saude_worker_configurado(base_url: str, token: str) -> Mapping[str, Any] | None:
    if not base_url or not token:
        return None
    enabled = _as_bool(getattr(config, "MUSIC_WORKER_CONFIGURED_HEALTHCHECK_ENABLED", True), True)
    if not enabled:
        return {"ok": True, "profile": "turbo"}
    try:
        timeout = max(
            0.3,
            min(8.0, float(getattr(config, "MUSIC_WORKER_CONFIGURED_HEALTH_TIMEOUT_SECONDS", 3.5) or 3.5)),
        )
    except Exception:
        timeout = 3.5
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/health", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(384 * 1024).decode("utf-8", "replace")
        data = json.loads(raw or "{}")
        if isinstance(data, Mapping) and bool(data.get("ok")):
            return data
    except Exception as exc:
        logger.info("[music/worker] phone worker configurado não respondeu rápido: %s", exc)
    return None

# Alias de compatibilidade enquanto consumidores antigos ainda importam o nome inglês.
worker_music_agent_summary = resumo_agente_musica
