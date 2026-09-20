from __future__ import annotations

import logging
import time

from cogs.musica import configuracao as config

from ..busca import ranquear_faixas
from ..nucleo.erros import MusicExtractionError
from ..nucleo.modelos import ExtractedBatch
from .cache_resolucao import (
    armazenar_cache_resolucao,
    chave_cache_resolucao,
    copiar_lote_para_requisicao,
    obter_cache_resolucao,
)
from .conversao_resolucao import converter_resposta_resolucao
from .modelos import MUSIC_WORKER_UNAVAILABLE_MESSAGE, MusicWorkerUnavailable
from .selecao import require_music_worker_available_async
from .roteamento import destino_vinculado, resolver_destino_worker
from .solicitacao_resolucao import (
    limite_resolucao as _limite_resolucao,
    montar_tarefa_resolucao as _montar_tarefa_resolucao,
    timeout_resolucao as _timeout_resolucao,
)
from .transporte_resolucao import executar_tarefa_resolucao

logger = logging.getLogger(__name__)



async def resolve_music_tracks_on_worker(
    query: str,
    *,
    requester_id: int = 0,
    requester_name: str = "",
    limit: int = 5,
    timeout_seconds: float | None = None,
    metadata_only: bool | None = None,
    allow_playlist: bool = False,
    guild_id: int = 0,
) -> ExtractedBatch:
    """Resolve pesquisa/link usando o yt-dlp do Phone Worker.

    A VPS atua somente como plano de controle. Ela não executa yt-dlp local e
    não reproduz áudio; a resolução e a sessão de voz pertencem ao telefone.
    """
    destino = destino_vinculado(guild_id)
    selection = None
    if destino is None:
        selection = await require_music_worker_available_async()
        destino = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
    if destino is None:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    base = destino.base
    token = destino.token

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
        worker_scope=(destino.worker_id or destino.base),
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
            destino.worker_id or destino.name,
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
            destino.worker_id or destino.name,
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
    if busca_textual and len(batch.tracks) > 1:
        batch.tracks, ranking = ranquear_faixas(clean_query, batch.tracks)
        if ranking:
            top = ranking[0]
            logger.info(
                "[music/search] ranking aplicado | query=%r tracks=%s top_score=%.4f confidence=%.4f original_index=%s",
                clean_query,
                len(batch.tracks),
                top.score,
                top.confianca,
                top.indice_original,
            )
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    logger.info(
        "[music/worker] yt-dlp remoto ok | worker=%s query=%r tracks=%s metadata_only=%s elapsed_ms=%.1f js=%s search=%s cli_rc=%s cli_error=%r",
        destino.worker_id or destino.name,
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
