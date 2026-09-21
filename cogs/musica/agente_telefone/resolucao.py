from __future__ import annotations

import asyncio
import logging
import time

from cogs.musica import configuracao as config

from ..busca import obter_escolha_busca
from ..busca.fontes import buscar_candidatos_youtube_fast
from ..busca.simples import converter_youtube_api_minimo, filtrar_faixas_minimas
from ..nucleo.erros import MusicExtractionError
from ..nucleo.modelos import ExtractedBatch
from .cache_resolucao import (
    armazenar_cache_resolucao,
    chave_cache_resolucao,
    copiar_lote_para_requisicao,
    obter_cache_resolucao,
)
from .coalescencia_resolucao import executar_resolucao_compartilhada
from .conversao_resolucao import converter_resposta_resolucao
from .modelos import MUSIC_WORKER_UNAVAILABLE_MESSAGE, MusicWorkerUnavailable
from .roteamento import destino_vinculado, resolver_destino_worker
from .selecao import require_music_worker_available_async
from .solicitacao_resolucao import (
    limite_resolucao as _limite_resolucao,
    montar_tarefa_resolucao as _montar_tarefa_resolucao,
    timeout_resolucao as _timeout_resolucao,
)
from .transporte_resolucao import executar_tarefa_resolucao

logger = logging.getLogger(__name__)


async def _resolver_busca_textual_simplificada(
    query: str,
    *,
    requester_id: int,
    requester_name: str,
    guild_id: int,
    limit: int,
    timeout_seconds: float | None,
) -> ExtractedBatch:
    """Resolve um miss com uma unica fonte por vez.

    Ordem fixa: YouTube Data API -> ytsearch3 no Phone Worker. Nao existe cache
    de resultado, ranking, fusao multifonte ou deep pass. A unica reutilizacao
    entre pesquisas concluidas e a memoria persistente de escolhas; chamadas
    simultaneas ainda podem compartilhar a mesma operacao em voo.
    """
    started = time.monotonic()
    limite = max(1, min(3, int(limit or 3)))

    if bool(getattr(config, "MUSIC_SEARCH_API_FIRST_ENABLED", True)):
        try:
            candidatos = await buscar_candidatos_youtube_fast(query, limit=limite)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            candidatos = []
            logger.debug(
                "[music/search] api-first simples falhou; usando worker | query=%r erro=%s",
                query,
                exc,
            )
        tracks_api = converter_youtube_api_minimo(
            candidatos,
            requester_id=requester_id,
            requester_name=requester_name,
            query=query,
            limit=limite,
        )
        if tracks_api:
            logger.info(
                "[music/search] api-first simples | query=%r tracks=%s elapsed_ms=%.1f",
                query,
                len(tracks_api),
                (time.monotonic() - started) * 1000.0,
            )
            return ExtractedBatch(tracks=tracks_api, query=query, is_playlist=False)

    destino = destino_vinculado(guild_id)
    if destino is None:
        selection = await require_music_worker_available_async()
        destino = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
    if destino is None:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)

    total_timeout = _timeout_resolucao(
        somente_metadados=True,
        timeout_seconds=timeout_seconds,
    )
    payload = _montar_tarefa_resolucao(
        query=query,
        limit=limite,
        timeout_seconds=total_timeout,
        somente_metadados=True,
        permitir_playlist=False,
        busca_textual=True,
        fast_search=True,
    )

    try:
        data = await executar_resolucao_compartilhada(
            executar_tarefa_resolucao,
            base=destino.base,
            token=destino.token,
            payload=payload,
            timeout_seconds=total_timeout,
        )
    except MusicWorkerUnavailable:
        raise
    except Exception as exc:
        logger.warning(
            "[music/worker] ytsearch3 simples falhou | worker=%s query=%r erro=%s",
            destino.worker_id or destino.name,
            query,
            exc,
        )
        raise MusicExtractionError(
            "`⚠️` Não consegui pesquisar essa música no worker agora. Tente novamente em alguns segundos.",
            detail=str(exc),
        ) from exc

    if data.get("ok") is False:
        message = str(data.get("message") or data.get("error") or "worker retornou erro ao pesquisar música")
        raise MusicExtractionError(f"`⚠️` {message[:220]}", detail=message)

    batch = converter_resposta_resolucao(
        data,
        base=destino.base,
        query=query,
        limit=limite,
        requester_id=requester_id,
        requester_name=requester_name,
    )
    batch.tracks = filtrar_faixas_minimas(batch.tracks, limit=limite)
    batch.query = query
    batch.is_playlist = False
    logger.info(
        "[music/search] worker simples | worker=%s query=%r tracks=%s elapsed_ms=%.1f search=%s warm=%s init_ms=%s extract_ms=%s",
        destino.worker_id or destino.name,
        query,
        len(batch.tracks),
        (time.monotonic() - started) * 1000.0,
        data.get("default_search") or "",
        bool(data.get("warm_ytdlp")),
        data.get("ytdlp_init_ms"),
        data.get("ytdlp_extract_ms"),
    )
    return batch


async def resolve_music_tracks_on_worker(
    query: str,
    *,
    requester_id: int = 0,
    requester_name: str = "",
    limit: int = 3,
    timeout_seconds: float | None = None,
    metadata_only: bool | None = None,
    allow_playlist: bool = False,
    guild_id: int = 0,
) -> ExtractedBatch:
    """Resolve pesquisa/link usando o yt-dlp do Phone Worker.

    Pesquisa textual metadata-only usa sempre o caminho simplificado. Links e
    resolucoes de stream continuam no worker e preservam o cache direto curto.
    """
    clean_query = str(query or "").strip()
    if not clean_query:
        return ExtractedBatch(tracks=[], query="", is_playlist=False)

    max_limit, busca_textual = _limite_resolucao(
        clean_query,
        limit,
        permitir_playlist=allow_playlist,
    )
    somente_metadados = bool(busca_textual) if metadata_only is None else bool(metadata_only)

    if busca_textual and somente_metadados and bool(
        getattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_ENABLED", True)
    ):
        escolhida = obter_escolha_busca(
            clean_query,
            requester_id=requester_id,
            requester_name=requester_name,
        )
        if escolhida is not None:
            logger.info(
                "[music/search] choice memory hit | query=%r track=%r",
                clean_query,
                escolhida.display_title or escolhida.title,
            )
            return ExtractedBatch(tracks=[escolhida], query=clean_query, is_playlist=False)

    if busca_textual and somente_metadados:
        return await _resolver_busca_textual_simplificada(
            clean_query,
            requester_id=requester_id,
            requester_name=requester_name,
            guild_id=guild_id,
            limit=max_limit,
            timeout_seconds=timeout_seconds,
        )

    destino = destino_vinculado(guild_id)
    if destino is None:
        selection = await require_music_worker_available_async()
        destino = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
    if destino is None:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)

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
        query_override=clean_query,
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
        fast_search=False,
    )
    started = time.monotonic()
    try:
        data = await executar_resolucao_compartilhada(
            executar_tarefa_resolucao,
            base=destino.base,
            token=destino.token,
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
        message = str(data.get("message") or data.get("error") or "worker retornou erro ao resolver música")
        raise MusicExtractionError(f"`⚠️` {message[:220]}", detail=message)

    batch = converter_resposta_resolucao(
        data,
        base=destino.base,
        query=clean_query,
        limit=max_limit,
        requester_id=requester_id,
        requester_name=requester_name,
    )
    logger.info(
        "[music/worker] yt-dlp remoto ok | worker=%s query=%r tracks=%s metadata_only=%s elapsed_ms=%.1f js=%s search=%s warm=%s init_ms=%s extract_ms=%s cli_rc=%s cli_error=%r",
        destino.worker_id or destino.name,
        clean_query,
        len(batch.tracks),
        bool(data.get("metadata_only") or somente_metadados),
        (time.monotonic() - started) * 1000.0,
        data.get("js_runtime") or "",
        data.get("default_search") or "",
        bool(data.get("warm_ytdlp")),
        data.get("ytdlp_init_ms"),
        data.get("ytdlp_extract_ms"),
        data.get("cli_rc"),
        str(data.get("cli_error") or data.get("api_error") or "")[:220],
    )
    armazenar_cache_resolucao(cache_key, batch, somente_metadados=somente_metadados)
    return copiar_lote_para_requisicao(
        batch,
        requester_id=requester_id,
        requester_name=requester_name,
    )
