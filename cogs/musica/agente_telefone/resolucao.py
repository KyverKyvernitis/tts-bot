from __future__ import annotations

import asyncio
import logging
import time

from cogs.musica import configuracao as config

from ..busca import avaliar_busca_profunda, fundir_resultados, ranquear_faixas
from ..busca.resiliencia import liberar_busca_profunda, tentar_reservar_busca_profunda
from ..busca.fontes import buscar_candidatos_multifonte
from ..nucleo.erros import MusicExtractionError
from ..nucleo.modelos import ExtractedBatch
from .busca_profunda import executar_passagem_profunda
from .coalescencia_resolucao import executar_resolucao_compartilhada, resolucoes_em_voo
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


async def _cancelar_tarefa_busca(task: asyncio.Task | None) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


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

    metadata_task: asyncio.Task | None = None
    if busca_textual and somente_metadados:
        metadata_task = asyncio.create_task(
            buscar_candidatos_multifonte(clean_query, limit=max_limit)
        )

    started = time.monotonic()
    try:
        data = await executar_resolucao_compartilhada(
            executar_tarefa_resolucao,
            base=base,
            token=token,
            payload=payload,
            timeout_seconds=total_timeout,
        )
    except MusicWorkerUnavailable:
        await _cancelar_tarefa_busca(metadata_task)
        raise
    except Exception as exc:
        await _cancelar_tarefa_busca(metadata_task)
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
        await _cancelar_tarefa_busca(metadata_task)
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
    worker_tracks_fast = list(batch.tracks)
    api_candidates = []
    if metadata_task is not None:
        try:
            api_candidates = await metadata_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[music/search] provider metadata falhou | query=%r erro=%s", clean_query, exc)

    if busca_textual and somente_metadados and api_candidates:
        batch.tracks, fusao = fundir_resultados(
            clean_query,
            batch.tracks,
            api_candidates,
            requester_id=requester_id,
            requester_name=requester_name,
            limit=max_limit,
        )
        logger.info(
            "[music/search] fusao multi-provider | query=%r entradas=%s grupos=%s duplicatas=%s fontes=%s",
            clean_query,
            fusao.entradas,
            fusao.grupos,
            fusao.duplicatas,
            ",".join(fusao.fontes),
        )

    ranking = []
    if busca_textual and batch.tracks:
        batch.tracks, ranking = ranquear_faixas(
            clean_query, batch.tracks, guild_id=guild_id, requester_id=requester_id
        )
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

    if busca_textual and somente_metadados:
        decisao = avaliar_busca_profunda(
            clean_query,
            batch.tracks,
            ranking,
            requested_limit=max_limit,
            enabled=bool(getattr(config, "MUSIC_SEARCH_DEEP_ENABLED", True)),
            deep_limit=int(getattr(config, "MUSIC_SEARCH_DEEP_LIMIT", 10) or 10),
            min_results=int(getattr(config, "MUSIC_SEARCH_DEEP_MIN_RESULTS", 3) or 3),
            score_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_SCORE_THRESHOLD", 0.70) or 0.70),
            confidence_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_CONFIDENCE_THRESHOLD", 0.60) or 0.60),
            margin_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_MARGIN_THRESHOLD", 0.045) or 0.045),
        )
        if decisao.executar:
            max_deep = max(
                0,
                int(getattr(config, "MUSIC_SEARCH_DEEP_MAX_CONCURRENT", 2) or 0),
            )
            max_worker_inflight = max(
                1,
                int(getattr(config, "MUSIC_SEARCH_DEEP_MAX_WORKER_INFLIGHT", 4) or 4),
            )
            worker_inflight = resolucoes_em_voo()
            reservado = (
                worker_inflight < max_worker_inflight
                and tentar_reservar_busca_profunda(limite=max_deep)
            )
            if not reservado:
                logger.info(
                    "[music/search] deep pass suprimido por carga | query=%r motivo=%s worker_inflight=%s limite_worker=%s limite_deep=%s",
                    clean_query,
                    decisao.motivo,
                    worker_inflight,
                    max_worker_inflight,
                    max_deep,
                )
            else:
                try:
                    deep_timeout = min(
                        total_timeout,
                        float(getattr(config, "MUSIC_SEARCH_DEEP_TIMEOUT_SECONDS", 7.0) or 7.0),
                    )
                    logger.info(
                        "[music/search] deep pass iniciado | query=%r deep_query=%r motivo=%s limit=%s score=%.4f confidence=%.4f margin=%.4f",
                        clean_query,
                        decisao.query,
                        decisao.motivo,
                        decisao.limit,
                        decisao.top_score,
                        decisao.top_confianca,
                        decisao.margem,
                    )
                    profundo = await executar_passagem_profunda(
                        base=base,
                        token=token,
                        query=decisao.query,
                        limit=decisao.limit,
                        timeout_seconds=max(3.0, deep_timeout),
                        requester_id=requester_id,
                        requester_name=requester_name,
                        executar_worker=executar_tarefa_resolucao,
                        buscar_metadata=buscar_candidatos_multifonte,
                    )
                    if profundo.tracks or profundo.api_candidates:
                        batch.tracks, fusao_profunda = fundir_resultados(
                            clean_query,
                            [*worker_tracks_fast, *profundo.tracks],
                            [*api_candidates, *profundo.api_candidates],
                            requester_id=requester_id,
                            requester_name=requester_name,
                            limit=decisao.limit,
                        )
                        batch.tracks, ranking = ranquear_faixas(
                            clean_query,
                            batch.tracks,
                            guild_id=guild_id,
                            requester_id=requester_id,
                        )
                        batch.tracks = batch.tracks[:max_limit]
                        top_final = ranking[0] if ranking else None
                        logger.info(
                            "[music/search] deep pass aplicado | query=%r motivo=%s tracks_worker=%s tracks_api=%s grupos=%s duplicatas=%s elapsed_ms=%.1f top_score=%s confidence=%s worker_error=%r api_error=%r",
                            clean_query,
                            decisao.motivo,
                            len(profundo.tracks),
                            len(profundo.api_candidates),
                            fusao_profunda.grupos,
                            fusao_profunda.duplicatas,
                            profundo.elapsed_ms,
                            f"{top_final.score:.4f}" if top_final else "",
                            f"{top_final.confianca:.4f}" if top_final else "",
                            profundo.worker_error[:160],
                            profundo.api_error[:160],
                        )
                    else:
                        logger.info(
                            "[music/search] deep pass sem ganho | query=%r motivo=%s elapsed_ms=%.1f worker_error=%r api_error=%r",
                            clean_query,
                            decisao.motivo,
                            profundo.elapsed_ms,
                            profundo.worker_error[:160],
                            profundo.api_error[:160],
                        )
                finally:
                    liberar_busca_profunda()

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
