from __future__ import annotations

import logging
import time

import config

from ..nucleo.erros import MusicExtractionError
from ..nucleo.modelos import ExtractedBatch
from .cache_resolucao import (
    armazenar_cache_resolucao,
    chave_cache_resolucao,
    copiar_lote_para_requisicao,
    obter_cache_resolucao,
)
from .conversao_resolucao import converter_resposta_resolucao, parece_url
from .modelos import MUSIC_WORKER_UNAVAILABLE_MESSAGE, MusicWorkerUnavailable
from .selecao import require_music_worker_available_async
from .transporte_resolucao import executar_tarefa_resolucao
from .utilitarios import _phone_worker_base_url

logger = logging.getLogger(__name__)


def _limite_resolucao(query: str, limit: int, *, permitir_playlist: bool) -> tuple[int, bool]:
    try:
        max_limit = max(1, min(10, int(limit or 5)))
    except Exception:
        max_limit = 5
    busca_textual = not parece_url(query)
    if not busca_textual and not permitir_playlist:
        # Link direto de faixa deve respeitar exatamente o URL enviado.
        max_limit = 1
    return max_limit, busca_textual


def _timeout_resolucao(*, somente_metadados: bool, timeout_seconds: float | None) -> float:
    if somente_metadados:
        default_timeout = float(
            getattr(config, "MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS", 12.0)
            or 12.0
        )
    else:
        default_timeout = float(
            getattr(config, "MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS", 28.0)
            or 28.0
        )
    return max(
        5.0,
        float(timeout_seconds if timeout_seconds is not None else default_timeout),
    )


def _montar_tarefa_resolucao(
    *,
    query: str,
    limit: int,
    timeout_seconds: float,
    somente_metadados: bool,
    permitir_playlist: bool,
    busca_textual: bool,
) -> dict[str, object]:
    return {
        "task": "music_ytdlp_resolve",
        "query": query,
        "limit": limit,
        "timeout_seconds": timeout_seconds,
        "metadata_only": bool(somente_metadados),
        "allow_playlist": bool(permitir_playlist),
        "js_runtimes": str(
            getattr(config, "MUSIC_WORKER_YTDLP_JS_RUNTIMES", "node") or "node"
        ),
        "default_search": f"ytsearch{limit}" if busca_textual else "auto",
    }


async def resolve_music_tracks_on_worker(
    query: str,
    *,
    requester_id: int = 0,
    requester_name: str = "",
    limit: int = 5,
    timeout_seconds: float | None = None,
    metadata_only: bool | None = None,
    allow_playlist: bool = False,
) -> ExtractedBatch:
    """Resolve pesquisa/link usando o yt-dlp do Phone Worker.

    A VPS atua somente como plano de controle. Ela não executa yt-dlp local e
    não reproduz áudio; a resolução e a sessão de voz pertencem ao telefone.
    """
    selection = await require_music_worker_available_async()
    base = _phone_worker_base_url()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not base or not token:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)

    clean_query = str(query or "").strip()
    if not clean_query:
        return ExtractedBatch(tracks=[], query="", is_playlist=False)

    max_limit, busca_textual = _limite_resolucao(
        clean_query,
        limit,
        permitir_playlist=allow_playlist,
    )
    somente_metadados = bool(busca_textual) if metadata_only is None else bool(metadata_only)
    total_timeout = _timeout_resolucao(
        somente_metadados=somente_metadados,
        timeout_seconds=timeout_seconds,
    )

    cache_key = chave_cache_resolucao(
        clean_query,
        max_limit,
        somente_metadados,
        allow_playlist,
    )
    cached_batch = obter_cache_resolucao(
        cache_key,
        requester_id=requester_id,
        requester_name=requester_name,
        somente_metadados=somente_metadados,
    )
    if cached_batch is not None:
        logger.info(
            "[music/worker] resolve cache hit | worker=%s query=%r tracks=%s metadata_only=%s",
            selection.worker_id or selection.name,
            clean_query,
            len(cached_batch.tracks),
            somente_metadados,
        )
        return cached_batch

    payload = _montar_tarefa_resolucao(
        query=clean_query,
        limit=max_limit,
        timeout_seconds=total_timeout,
        somente_metadados=somente_metadados,
        permitir_playlist=allow_playlist,
        busca_textual=busca_textual,
    )

    started = time.monotonic()
    try:
        data = await executar_tarefa_resolucao(
            base=base,
            token=token,
            payload=payload,
            timeout_seconds=total_timeout,
        )
    except MusicWorkerUnavailable:
        raise
    except Exception as exc:
        logger.warning(
            "[music/worker] yt-dlp remoto falhou | worker=%s query=%r erro=%s",
            selection.worker_id,
            clean_query,
            exc,
        )
        raise MusicExtractionError(
            "`⚠️` Não consegui resolver essa música no worker agora. Tente novamente em alguns segundos.",
            detail=str(exc),
        ) from exc

    if data.get("ok") is False:
        message = str(
            data.get("message")
            or data.get("error")
            or "worker retornou erro ao resolver música"
        )
        raise MusicExtractionError(f"`⚠️` {message[:220]}", detail=message)

    batch = converter_resposta_resolucao(
        data,
        base=base,
        query=clean_query,
        limit=max_limit,
        requester_id=requester_id,
        requester_name=requester_name,
    )
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    logger.info(
        "[music/worker] yt-dlp remoto ok | worker=%s query=%r tracks=%s metadata_only=%s elapsed_ms=%.1f js=%s search=%s cli_rc=%s cli_error=%r",
        selection.worker_id or selection.name,
        clean_query,
        len(batch.tracks),
        bool(data.get("metadata_only") or somente_metadados),
        elapsed_ms,
        data.get("js_runtime") or "",
        data.get("default_search") or "",
        data.get("cli_rc"),
        str(data.get("cli_error") or data.get("api_error") or "")[:220],
    )
    armazenar_cache_resolucao(
        cache_key,
        batch,
        somente_metadados=somente_metadados,
    )
    return copiar_lote_para_requisicao(
        batch,
        requester_id=requester_id,
        requester_name=requester_name,
    )
