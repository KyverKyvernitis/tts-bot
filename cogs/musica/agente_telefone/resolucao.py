from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from cogs.musica import configuracao as config

from ..busca import (
    avaliar_busca_profunda,
    avaliar_ganho_busca_profunda,
    fundir_resultados,
    ranquear_faixas,
    registrar_busca_telemetria,
)
from ..busca.resiliencia import liberar_busca_profunda, tentar_reservar_busca_profunda
from ..busca.latencia import headstart_adaptativo, registrar_latencia_api, registrar_latencia_worker
from ..busca.chaves import chave_semantica_busca
from ..busca.fontes import buscar_candidatos_multifonte, buscar_candidatos_youtube_fast
from ..metadados.quota_youtube import snapshot_quota_youtube
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


def _fontes_tracks(tracks) -> tuple[str, ...]:
    fontes: list[str] = []
    for track in tracks:
        fonte = str(getattr(track, "display_source", "") or getattr(track, "source", "") or "").strip()
        if fonte:
            fontes.append(fonte)
    return tuple(fontes)


def _registrar_telemetria_busca(
    *,
    tracks,
    ranking,
    elapsed_ms: float,
    cache_hit: bool = False,
    deep_estado: str = "nao_necessario",
    deep_motivo: str = "",
) -> None:
    if not bool(getattr(config, "MUSIC_SEARCH_TELEMETRY_ENABLED", True)):
        return
    top = ranking[0] if ranking else None
    resumo = registrar_busca_telemetria(
        cache_hit=cache_hit,
        deep_estado=deep_estado,
        deep_motivo=deep_motivo,
        top_score=(top.score if top is not None else None),
        top_confianca=(top.confianca if top is not None else None),
        elapsed_ms=elapsed_ms,
        fontes=_fontes_tracks(tracks),
        resumo_cada=int(getattr(config, "MUSIC_SEARCH_TELEMETRY_SUMMARY_EVERY", 25) or 25),
    )
    if resumo is not None:
        fontes = ",".join(f"{nome}:{quantidade}" for nome, quantidade in resumo.fontes[:6])
        motivos = ",".join(f"{nome}:{quantidade}" for nome, quantidade in resumo.motivos_deep[:6])
        quota_youtube = snapshot_quota_youtube(
            limite_diario=int(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", 80) or 0)
        )
        logger.info(
            "[music/search] telemetria agregada | buscas=%s cache_hits=%s deep=%s aplicadas=%s rejeitadas=%s suprimidas=%s sem_resultado=%s selecoes=%s primeiro=%s top3=%s lat_media_ms=%.1f lat_max_ms=%.1f score_medio=%.4f confidence_media=%.4f fontes=%s motivos=%s yt_api_calls=%s yt_api_blocked=%s yt_api_left=%s",
            resumo.buscas,
            resumo.cache_hits,
            resumo.deep_solicitadas,
            resumo.deep_aplicadas,
            resumo.deep_rejeitadas,
            resumo.deep_suprimidas,
            resumo.deep_sem_resultado,
            resumo.selecoes,
            resumo.selecoes_primeiro,
            resumo.selecoes_top3,
            resumo.latencia_media_ms,
            resumo.latencia_max_ms,
            resumo.score_medio,
            resumo.confianca_media,
            fontes,
            motivos,
            quota_youtube.chamadas,
            quota_youtube.bloqueadas,
            quota_youtube.restantes,
        )


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


def _avaliar_api_first(
    query: str,
    candidatos,
    *,
    requester_id: int,
    requester_name: str,
    guild_id: int,
    limit: int,
):
    if not candidatos:
        return None
    tracks, fusao = fundir_resultados(
        query,
        [],
        candidatos,
        requester_id=requester_id,
        requester_name=requester_name,
        limit=limit,
    )
    tracks, ranking = ranquear_faixas(
        query,
        tracks,
        guild_id=guild_id,
        requester_id=requester_id,
    )
    min_results = min(
        limit,
        max(1, int(getattr(config, "MUSIC_SEARCH_API_FIRST_MIN_RESULTS", 3) or 3)),
    )
    decisao = avaliar_busca_profunda(
        query,
        tracks,
        ranking,
        requested_limit=limit,
        enabled=True,
        deep_limit=int(getattr(config, "MUSIC_SEARCH_DEEP_LIMIT", 5) or 5),
        min_results=min_results,
        score_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_SCORE_THRESHOLD", 0.66) or 0.66),
        confidence_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_CONFIDENCE_THRESHOLD", 0.55) or 0.55),
        margin_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_MARGIN_THRESHOLD", 0.030) or 0.030),
    )
    if len(tracks) < min_results or decisao.executar:
        return None
    return (
        ExtractedBatch(tracks=tracks[:limit], query=query, is_playlist=False),
        ranking[:limit],
        fusao,
    )


def _fast_worker_suficiente(
    query: str,
    tracks,
    ranking,
    *,
    requested_limit: int,
) -> bool:
    if len(tracks) < max(1, int(getattr(config, "MUSIC_SEARCH_DEEP_MIN_RESULTS", 3) or 3)):
        return False
    decisao = avaliar_busca_profunda(
        query,
        tracks,
        ranking,
        requested_limit=requested_limit,
        enabled=bool(getattr(config, "MUSIC_SEARCH_DEEP_ENABLED", True)),
        deep_limit=int(getattr(config, "MUSIC_SEARCH_DEEP_LIMIT", 5) or 5),
        min_results=int(getattr(config, "MUSIC_SEARCH_DEEP_MIN_RESULTS", 3) or 3),
        score_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_SCORE_THRESHOLD", 0.66) or 0.66),
        confidence_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_CONFIDENCE_THRESHOLD", 0.55) or 0.55),
        margin_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_MARGIN_THRESHOLD", 0.030) or 0.030),
    )
    return not decisao.executar


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

    cache_query = clean_query
    if busca_textual and somente_metadados and bool(
        getattr(config, "MUSIC_SEARCH_SEMANTIC_CACHE_ENABLED", True)
    ):
        cache_query = chave_semantica_busca(clean_query)
    cache_key = chave_cache_resolucao(
        cache_query,
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
        if busca_textual and somente_metadados:
            _registrar_telemetria_busca(
                tracks=cached_batch.tracks,
                ranking=(),
                elapsed_ms=0.0,
                cache_hit=True,
            )
        return cached_batch

    started = time.monotonic()
    api_first_candidates = []
    api_first_task: asyncio.Task | None = None
    api_first_attempted = bool(
        busca_textual
        and somente_metadados
        and bool(getattr(config, "MUSIC_SEARCH_API_FIRST_ENABLED", True))
    )
    if api_first_attempted:
        api_first_task = asyncio.create_task(
            buscar_candidatos_youtube_fast(clean_query, limit=max_limit)
        )
        headstart_base = max(
            0.0,
            float(getattr(config, "MUSIC_SEARCH_API_FIRST_HEADSTART_SECONDS", 0.15) or 0.0),
        )
        if bool(getattr(config, "MUSIC_SEARCH_API_FIRST_ADAPTIVE_HEDGE", True)):
            headstart = headstart_adaptativo(
                headstart_base,
                min_seconds=float(
                    getattr(config, "MUSIC_SEARCH_API_FIRST_HEADSTART_MIN_SECONDS", 0.03) or 0.0
                ),
            )
        else:
            headstart = headstart_base
        # Se ja existe resolucao textual em voo, nao introduza nova janela de
        # head-start: entrar no worker imediatamente preserva o singleflight.
        if resolucoes_em_voo() > 0:
            headstart = 0.0
        if headstart > 0.0:
            try:
                api_first_candidates = await asyncio.wait_for(
                    asyncio.shield(api_first_task),
                    timeout=headstart,
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "[music/search] api-first falhou; seguindo para worker | query=%r erro=%s",
                    clean_query,
                    exc,
                )
        elif api_first_task.done():
            with contextlib.suppress(Exception):
                api_first_candidates = api_first_task.result()

        aceite = _avaliar_api_first(
            clean_query,
            api_first_candidates,
            requester_id=requester_id,
            requester_name=requester_name,
            guild_id=guild_id,
            limit=max_limit,
        )
        if api_first_task is not None and api_first_task.done():
            registrar_latencia_api(
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                suficiente=aceite is not None,
            )
        if aceite is not None:
            batch, api_ranking, api_fusao = aceite
            api_elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
            logger.info(
                "[music/search] api-first hit | worker=%s query=%r tracks=%s elapsed_ms=%.1f grupos=%s score=%.4f confidence=%.4f",
                destino.worker_id or destino.name,
                clean_query,
                len(batch.tracks),
                api_elapsed_ms,
                api_fusao.grupos,
                api_ranking[0].score if api_ranking else 0.0,
                api_ranking[0].confianca if api_ranking else 0.0,
            )
            _registrar_telemetria_busca(
                tracks=batch.tracks,
                ranking=api_ranking,
                elapsed_ms=api_elapsed_ms,
                deep_estado="api_first",
                deep_motivo="primeira_passagem_suficiente",
            )
            armazenar_cache_resolucao(cache_key, batch, somente_metadados=somente_metadados)
            return copiar_lote_para_requisicao(
                batch, requester_id=requester_id, requester_name=requester_name
            )

    payload = _montar_tarefa_resolucao(
        query=clean_query,
        limit=max_limit,
        timeout_seconds=total_timeout,
        somente_metadados=somente_metadados,
        permitir_playlist=allow_playlist,
        busca_textual=busca_textual,
        fast_search=bool(busca_textual and somente_metadados),
    )

    metadata_task: asyncio.Task | None = None
    if busca_textual and somente_metadados:
        metadata_task = asyncio.create_task(
            buscar_candidatos_multifonte(
                clean_query,
                limit=max_limit,
                incluir_youtube=not api_first_attempted,
            )
        )

    worker_started = time.monotonic()
    worker_task = asyncio.create_task(
        executar_resolucao_compartilhada(
            executar_tarefa_resolucao,
            base=base,
            token=token,
            payload=payload,
            timeout_seconds=total_timeout,
        )
    )

    try:
        if api_first_task is not None and not api_first_task.done():
            done, _ = await asyncio.wait(
                {worker_task, api_first_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if api_first_task in done:
                try:
                    api_first_candidates = list(api_first_task.result())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug(
                        "[music/search] api-first terminou com falha | query=%r erro=%s",
                        clean_query,
                        exc,
                    )
                    api_first_candidates = []
                aceite = _avaliar_api_first(
                    clean_query,
                    api_first_candidates,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    guild_id=guild_id,
                    limit=max_limit,
                )
                registrar_latencia_api(
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                    suficiente=aceite is not None,
                )
                if aceite is not None:
                    batch, api_ranking, api_fusao = aceite
                    await _cancelar_tarefa_busca(worker_task)
                    await _cancelar_tarefa_busca(metadata_task)
                    api_elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                    logger.info(
                        "[music/search] api-first hedge hit | worker=%s query=%r tracks=%s elapsed_ms=%.1f grupos=%s score=%.4f confidence=%.4f",
                        destino.worker_id or destino.name,
                        clean_query,
                        len(batch.tracks),
                        api_elapsed_ms,
                        api_fusao.grupos,
                        api_ranking[0].score if api_ranking else 0.0,
                        api_ranking[0].confianca if api_ranking else 0.0,
                    )
                    _registrar_telemetria_busca(
                        tracks=batch.tracks,
                        ranking=api_ranking,
                        elapsed_ms=api_elapsed_ms,
                        deep_estado="api_first",
                        deep_motivo="hedge_primeira_passagem_suficiente",
                    )
                    armazenar_cache_resolucao(cache_key, batch, somente_metadados=somente_metadados)
                    return copiar_lote_para_requisicao(
                        batch, requester_id=requester_id, requester_name=requester_name
                    )
        data = await worker_task
        registrar_latencia_worker(elapsed_ms=(time.monotonic() - worker_started) * 1000.0)
    except MusicWorkerUnavailable:
        await _cancelar_tarefa_busca(metadata_task)
        await _cancelar_tarefa_busca(api_first_task)
        raise
    except Exception as exc:
        await _cancelar_tarefa_busca(metadata_task)
        await _cancelar_tarefa_busca(api_first_task)
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
    if api_first_task is not None and api_first_task.done() and not api_first_candidates:
        with contextlib.suppress(Exception):
            api_first_candidates = list(api_first_task.result())
    elif api_first_task is not None and not api_first_task.done():
        await _cancelar_tarefa_busca(api_first_task)

    worker_ranking_fast = []
    worker_suficiente = False
    if busca_textual and somente_metadados and worker_tracks_fast:
        worker_ranked, worker_ranking_fast = ranquear_faixas(
            clean_query,
            list(worker_tracks_fast),
            guild_id=guild_id,
            requester_id=requester_id,
        )
        worker_suficiente = _fast_worker_suficiente(
            clean_query,
            worker_ranked,
            worker_ranking_fast,
            requested_limit=max_limit,
        )

    api_candidates = list(api_first_candidates)
    if metadata_task is not None:
        metadata_result = None
        if metadata_task.done():
            try:
                metadata_result = metadata_task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[music/search] provider metadata falhou | query=%r erro=%s", clean_query, exc)
        elif (
            worker_suficiente
            and bool(getattr(config, "MUSIC_SEARCH_SKIP_PENDING_METADATA_WHEN_WORKER_SUFFICIENT", True))
        ):
            await _cancelar_tarefa_busca(metadata_task)
            logger.debug(
                "[music/search] metadata pendente descartada; worker ja suficiente | query=%r tracks=%s",
                clean_query,
                len(worker_tracks_fast),
            )
        else:
            grace = max(
                0.0,
                float(getattr(config, "MUSIC_SEARCH_METADATA_AFTER_WORKER_GRACE_SECONDS", 0.08) or 0.0),
            )
            try:
                if grace <= 0.0:
                    raise asyncio.TimeoutError
                metadata_result = await asyncio.wait_for(asyncio.shield(metadata_task), timeout=grace)
            except asyncio.TimeoutError:
                await _cancelar_tarefa_busca(metadata_task)
                logger.debug(
                    "[music/search] metadata excedeu grace pos-worker | query=%r grace_ms=%.0f",
                    clean_query,
                    grace * 1000.0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[music/search] provider metadata falhou | query=%r erro=%s", clean_query, exc)
        if metadata_result:
            api_candidates.extend(metadata_result)

    if busca_textual and somente_metadados and api_candidates:
        batch.tracks, fusao = fundir_resultados(
            clean_query,
            batch.tracks,
            api_candidates,
            requester_id=requester_id,
            requester_name=requester_name,
            limit=max_limit,
        )
        logger.debug(
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
            logger.debug(
                "[music/search] ranking aplicado | query=%r tracks=%s top_score=%.4f confidence=%.4f original_index=%s",
                clean_query,
                len(batch.tracks),
                top.score,
                top.confianca,
                top.indice_original,
            )

    fast_tracks_ranked = list(batch.tracks)
    fast_ranking = list(ranking)
    deep_estado = "nao_necessario"
    deep_motivo = ""

    if busca_textual and somente_metadados:
        decisao = avaliar_busca_profunda(
            clean_query,
            batch.tracks,
            ranking,
            requested_limit=max_limit,
            enabled=bool(getattr(config, "MUSIC_SEARCH_DEEP_ENABLED", True)),
            deep_limit=int(getattr(config, "MUSIC_SEARCH_DEEP_LIMIT", 5) or 5),
            min_results=int(getattr(config, "MUSIC_SEARCH_DEEP_MIN_RESULTS", 3) or 3),
            score_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_SCORE_THRESHOLD", 0.66) or 0.66),
            confidence_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_CONFIDENCE_THRESHOLD", 0.55) or 0.55),
            margin_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_MARGIN_THRESHOLD", 0.030) or 0.030),
        )
        deep_motivo = decisao.motivo
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
                deep_estado = "suprimido"
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
                        float(getattr(config, "MUSIC_SEARCH_DEEP_TIMEOUT_SECONDS", 5.0) or 5.0),
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
                    avaliar_parcial = None
                    if bool(getattr(config, "MUSIC_SEARCH_DEEP_EARLY_EXIT_ENABLED", True)):
                        def _avaliar_parcial_deep(tracks_parciais, api_parciais) -> bool:
                            if not tracks_parciais and not api_parciais:
                                return False
                            candidatos_parciais, _ = fundir_resultados(
                                clean_query,
                                [*worker_tracks_fast, *tracks_parciais],
                                [*api_candidates, *api_parciais],
                                requester_id=requester_id,
                                requester_name=requester_name,
                                limit=decisao.limit,
                            )
                            candidatos_parciais, ranking_parcial = ranquear_faixas(
                                clean_query,
                                candidatos_parciais,
                                guild_id=guild_id,
                                requester_id=requester_id,
                            )
                            candidatos_visiveis = candidatos_parciais[:max_limit]
                            ranking_visivel = ranking_parcial[:max_limit]
                            if len(candidatos_visiveis) < max_limit:
                                return False
                            ainda_precisa_deep = avaliar_busca_profunda(
                                clean_query,
                                candidatos_visiveis,
                                ranking_visivel,
                                requested_limit=max_limit,
                                enabled=True,
                                deep_limit=decisao.limit,
                                min_results=max_limit,
                                score_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_SCORE_THRESHOLD", 0.66) or 0.66),
                                confidence_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_CONFIDENCE_THRESHOLD", 0.55) or 0.55),
                                margin_threshold=float(getattr(config, "MUSIC_SEARCH_DEEP_MARGIN_THRESHOLD", 0.030) or 0.030),
                            )
                            if ainda_precisa_deep.executar:
                                return False
                            ganho_parcial = avaliar_ganho_busca_profunda(
                                fast_tracks_ranked,
                                fast_ranking,
                                candidatos_visiveis,
                                ranking_visivel,
                                requested_limit=max_limit,
                            )
                            return bool(ganho_parcial.aplicar)

                        avaliar_parcial = _avaliar_parcial_deep

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
                        avaliar_parcial=avaliar_parcial,
                    )
                    if profundo.early_exit:
                        logger.info(
                            "[music/search] deep early-exit | query=%r motivo=%s fonte=%s elapsed_ms=%.1f",
                            clean_query,
                            decisao.motivo,
                            profundo.early_exit,
                            profundo.elapsed_ms,
                        )
                    if profundo.tracks or profundo.api_candidates:
                        candidatos_tracks, fusao_profunda = fundir_resultados(
                            clean_query,
                            [*worker_tracks_fast, *profundo.tracks],
                            [*api_candidates, *profundo.api_candidates],
                            requester_id=requester_id,
                            requester_name=requester_name,
                            limit=decisao.limit,
                        )
                        candidatos_tracks, ranking_profundo = ranquear_faixas(
                            clean_query,
                            candidatos_tracks,
                            guild_id=guild_id,
                            requester_id=requester_id,
                        )
                        candidatos_tracks = candidatos_tracks[:max_limit]
                        ranking_profundo = ranking_profundo[:max_limit]
                        ganho = avaliar_ganho_busca_profunda(
                            fast_tracks_ranked,
                            fast_ranking,
                            candidatos_tracks,
                            ranking_profundo,
                            requested_limit=max_limit,
                        )
                        if ganho.aplicar:
                            batch.tracks = candidatos_tracks
                            ranking = ranking_profundo
                            deep_estado = "aplicado"
                            top_final = ranking[0] if ranking else None
                            logger.info(
                                "[music/search] deep pass aplicado | query=%r motivo=%s ganho=%s delta_score=%.4f delta_confidence=%.4f delta_top3=%.4f delta_resultados=%s tracks_worker=%s tracks_api=%s grupos=%s duplicatas=%s elapsed_ms=%.1f top_score=%s confidence=%s worker_error=%r api_error=%r",
                                clean_query,
                                decisao.motivo,
                                ganho.motivo,
                                ganho.delta_score,
                                ganho.delta_confianca,
                                ganho.delta_media_top,
                                ganho.delta_resultados,
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
                            batch.tracks = fast_tracks_ranked
                            ranking = fast_ranking
                            deep_estado = "rejeitado"
                            logger.info(
                                "[music/search] deep pass rejeitado pelo gain gate | query=%r motivo=%s ganho=%s delta_score=%.4f delta_confidence=%.4f delta_top3=%.4f delta_resultados=%s elapsed_ms=%.1f",
                                clean_query,
                                decisao.motivo,
                                ganho.motivo,
                                ganho.delta_score,
                                ganho.delta_confianca,
                                ganho.delta_media_top,
                                ganho.delta_resultados,
                                profundo.elapsed_ms,
                            )
                    else:
                        deep_estado = "sem_resultado"
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
    if busca_textual and somente_metadados:
        _registrar_telemetria_busca(
            tracks=batch.tracks,
            ranking=ranking,
            elapsed_ms=elapsed_ms,
            deep_estado=deep_estado,
            deep_motivo=deep_motivo,
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
