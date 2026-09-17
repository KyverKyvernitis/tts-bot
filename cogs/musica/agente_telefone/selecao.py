from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Mapping

from cogs.musica import configuracao as config

from .configuracao import carregar_configuracao_selecao, music_worker_only_enabled
from .modelos import MUSIC_WORKER_UNAVAILABLE_MESSAGE, MusicWorkerSelection, MusicWorkerUnavailable
from .registro import carregar_workers_publicos, worker_e_turbo
from .saude import (
    bootstrap_agente_permitido,
    consultar_saude_worker_configurado,
    resumo_agente_musica,
)
from .utilitarios import _as_bool, _csv, _nested, _phone_worker_base_url

logger = logging.getLogger(__name__)
_SELECTION_CACHE: dict[str, Any] = {"at": 0.0, "selection": None}


def _configured_phone_worker_selection(reason: str = "phone_worker_configurado") -> MusicWorkerSelection | None:
    """Cria a seleção direta do Phone Worker configurado.

    O worker de música é validado pelo próprio Music Agent. Lavalink não é
    requisito nem é sintetizado aqui: a reprodução real é yt-dlp/FFmpeg no
    telefone e Lavalink fica restrito a metadados.
    """
    endpoint = _phone_worker_base_url()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not endpoint or not token:
        return None

    health = consultar_saude_worker_configurado(endpoint, token)
    if health is None:
        return None

    cfg = carregar_configuracao_selecao()
    agent = health.get("music_agent") if isinstance(health, Mapping) else None
    if cfg.agente_ativo:
        summary = resumo_agente_musica({"status": {"music_agent": agent}}, configuracao=cfg)
        if not summary.get("available"):
            if bootstrap_agente_permitido(summary.get("reason"), configuracao=cfg):
                logger.info(
                    "[music/worker] phone worker configurado será usado para preparar Music Agent: %s",
                    summary.get("reason"),
                )
            else:
                logger.info(
                    "[music/worker] phone worker configurado sem Music Agent elegível: %s",
                    summary.get("reason"),
                )
                return None

    host = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
    roles = sorted(set(cfg.papeis_obrigatorios) | {"phone-worker", "music", "music-agent", "music-ytdlp"})
    capabilities = sorted(
        set(cfg.capacidades_obrigatorias)
        | {
            "phone-worker",
            "ffmpeg",
            "ffprobe",
            "music",
            "music-agent",
            "music-ytdlp",
            "music-ytdlp-resolve",
            "service-control",
        }
    )
    worker_id = str(
        os.getenv("CORE_WORKER_ID")
        or getattr(config, "PHONE_WORKER_ID", "")
        or "phone-worker-configured"
    ).strip()
    worker = {
        "worker_id": worker_id,
        "name": str(os.getenv("CORE_WORKER_NAME") or "Phone Worker Turbo"),
        "online": True,
        "enabled": True,
        "profile": str(health.get("profile") or "turbo") if isinstance(health, Mapping) else "turbo",
        "roles": roles,
        "capabilities": capabilities,
        "endpoint": endpoint,
        "remote_addr": host,
        "status": {
            "profile": str(health.get("profile") or "turbo") if isinstance(health, Mapping) else "turbo",
            "music_agent": agent if isinstance(agent, Mapping) else {},
        },
    }
    return MusicWorkerSelection(
        True,
        worker_id=worker_id,
        name=str(worker["name"]),
        worker=worker,
        reason=reason,
    )


def _selection_cache_seconds() -> float:
    return carregar_configuracao_selecao().cache_segundos


def select_music_worker() -> MusicWorkerSelection:
    ttl = _selection_cache_seconds()
    now = time.monotonic()
    cached = _SELECTION_CACHE.get("selection")
    if (
        ttl > 0
        and isinstance(cached, MusicWorkerSelection)
        and now - float(_SELECTION_CACHE.get("at") or 0.0) <= ttl
    ):
        return cached
    selection = _select_music_worker_uncached()
    _SELECTION_CACHE["selection"] = selection
    _SELECTION_CACHE["at"] = now
    return selection


def _select_music_worker_uncached() -> MusicWorkerSelection:
    cfg = carregar_configuracao_selecao()
    if not cfg.worker_only:
        return MusicWorkerSelection(True, reason="worker-only desativado")

    workers = carregar_workers_publicos()
    online = [w for w in workers if bool(w.get("enabled", True)) and bool(w.get("online"))]
    if not online:
        configured = _configured_phone_worker_selection(reason="phone_worker_configurado_sem_registry_online")
        if configured is not None:
            logger.info("[music/worker] usando phone worker configurado direto; registry sem worker online")
            return configured
        return MusicWorkerSelection(False, reason="nenhum worker online")

    rejected: list[str] = []
    required_roles = set(cfg.papeis_obrigatorios)
    required_caps = set(cfg.capacidades_obrigatorias)

    for worker in online:
        roles_caps = (
            _csv(worker.get("roles"))
            | _csv(worker.get("capabilities"))
            | _csv(_nested(worker, "status", "profile"))
        )
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id or "Core Worker")
        missing_roles = sorted(required_roles - roles_caps)
        missing_caps = sorted(required_caps - roles_caps)

        if cfg.exigir_turbo and not worker_e_turbo(worker, roles_caps):
            rejected.append(f"{worker_id or name}:não_turbo")
            continue
        if missing_roles or missing_caps:
            rejected.append(f"{worker_id or name}:sem_capacidade")
            continue

        # A seleção do reprodutor não depende de Lavalink/music_node. O agente do
        # telefone é a autoridade da reprodução; Lavalink serve apenas metadata.
        if cfg.agente_ativo:
            agent_summary = resumo_agente_musica(worker, configuracao=cfg)
            if not agent_summary.get("available"):
                reason = str(agent_summary.get("reason") or "music_agent_indisponivel")
                live = _configured_phone_worker_selection(reason=f"live_status_registry_stale:{reason}")
                if live is not None:
                    live_summary = resumo_agente_musica(live.worker, configuracao=cfg)
                    if live_summary.get("available"):
                        logger.info(
                            "[music/worker] registry stale; usando status live do phone worker | worker=%s reason=%s",
                            live.worker_id or live.name,
                            reason,
                        )
                        return live
                if not bootstrap_agente_permitido(reason, configuracao=cfg):
                    rejected.append(f"{worker_id or name}:{reason}")
                    continue
                logger.info(
                    "[music/worker] selecionando worker turbo para preparar Music Agent | worker=%s reason=%s",
                    worker_id or name,
                    reason,
                )
                return MusicWorkerSelection(
                    True,
                    worker_id=worker_id,
                    name=name,
                    worker=worker,
                    reason=f"bootstrap:{reason}",
                )

        return MusicWorkerSelection(True, worker_id=worker_id, name=name, worker=worker, reason="ok")

    reason = "; ".join(rejected[:3]) if rejected else "nenhum worker compatível"
    configured = _configured_phone_worker_selection(reason=f"phone_worker_configurado_fallback:{reason}")
    if configured is not None:
        logger.info("[music/worker] usando phone worker configurado direto; recusas_registry=%s", reason)
        return configured
    return MusicWorkerSelection(False, reason=reason)


def _raise_unavailable(selection: MusicWorkerSelection) -> None:
    logger.info("[music/worker] indisponível: %s", selection.reason or "sem motivo")
    raise MusicWorkerUnavailable(selection.message or MUSIC_WORKER_UNAVAILABLE_MESSAGE)


async def select_music_worker_async() -> MusicWorkerSelection:
    return await asyncio.to_thread(select_music_worker)


async def ensure_music_worker_available() -> MusicWorkerSelection:
    return await select_music_worker_async()


def require_music_worker_available() -> MusicWorkerSelection:
    selection = select_music_worker()
    if not selection.available:
        _raise_unavailable(selection)
    return selection


async def require_music_worker_available_async() -> MusicWorkerSelection:
    selection = await select_music_worker_async()
    if not selection.available:
        _raise_unavailable(selection)
    return selection


# Compatibilidade de imports durante a modularização.
worker_music_agent_summary = resumo_agente_musica
_load_public_workers = carregar_workers_publicos
