from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Awaitable, Callable, Mapping, Any

from ..busca.fontes import buscar_candidatos_multifonte
from ..metadados.modelos import ApiTrackCandidate
from ..nucleo.modelos import MusicTrack
from .coalescencia_resolucao import executar_resolucao_compartilhada
from .conversao_resolucao import converter_resposta_resolucao
from .solicitacao_resolucao import montar_tarefa_resolucao
from .transporte_resolucao import executar_tarefa_resolucao


@dataclass(slots=True)
class ResultadoPassagemProfunda:
    tracks: list[MusicTrack] = field(default_factory=list)
    api_candidates: list[ApiTrackCandidate] = field(default_factory=list)
    worker_error: str = ""
    api_error: str = ""
    early_exit: str = ""
    elapsed_ms: float = 0.0


async def executar_passagem_profunda(
    *,
    base: str,
    token: str,
    query: str,
    limit: int,
    timeout_seconds: float,
    requester_id: int,
    requester_name: str,
    executar_worker: Callable[..., Awaitable[Mapping[str, Any]]] = executar_tarefa_resolucao,
    buscar_metadata: Callable[..., Awaitable[list[ApiTrackCandidate]]] = buscar_candidatos_multifonte,
    avaliar_parcial: Callable[[list[MusicTrack], list[ApiTrackCandidate]], bool] | None = None,
) -> ResultadoPassagemProfunda:
    """Executa worker + metadata em paralelo e nunca invalida o fast pass.

    Quando ``avaliar_parcial`` confirma que o primeiro lado concluido ja produz
    ganho suficiente sobre o fast pass, o trabalho pendente e cancelado.
    """
    started = time.monotonic()
    payload = montar_tarefa_resolucao(
        query=query,
        limit=limit,
        timeout_seconds=timeout_seconds,
        somente_metadados=True,
        permitir_playlist=False,
        busca_textual=True,
        fast_search=False,
    )
    worker_task = asyncio.create_task(
        executar_resolucao_compartilhada(
            executar_worker,
            base=base,
            token=token,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
    )
    metadata_task = asyncio.create_task(buscar_metadata(query, limit=limit))

    resultado = ResultadoPassagemProfunda()
    pendentes: set[asyncio.Task] = {worker_task, metadata_task}

    async def _cancelar_pendentes() -> None:
        if not pendentes:
            return
        for task in pendentes:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pendentes, return_exceptions=True)
        pendentes.clear()

    try:
        while pendentes:
            concluidas, ainda_pendentes = await asyncio.wait(
                pendentes,
                return_when=asyncio.FIRST_COMPLETED,
            )
            pendentes = set(ainda_pendentes)

            for task in concluidas:
                if task is worker_task:
                    try:
                        worker_result = task.result()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        resultado.worker_error = str(exc)
                    else:
                        if isinstance(worker_result, Mapping):
                            if worker_result.get("ok") is False:
                                resultado.worker_error = str(
                                    worker_result.get("message")
                                    or worker_result.get("error")
                                    or "worker retornou erro"
                                )
                            else:
                                lote = converter_resposta_resolucao(
                                    worker_result,
                                    base=base,
                                    query=query,
                                    limit=limit,
                                    requester_id=requester_id,
                                    requester_name=requester_name,
                                )
                                resultado.tracks = lote.tracks
                elif task is metadata_task:
                    try:
                        metadata_result = task.result()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        resultado.api_error = str(exc)
                    else:
                        if isinstance(metadata_result, list):
                            resultado.api_candidates = metadata_result

            if avaliar_parcial is not None and pendentes and (resultado.tracks or resultado.api_candidates):
                try:
                    suficiente = bool(avaliar_parcial(resultado.tracks, resultado.api_candidates))
                except Exception:
                    suficiente = False
                if suficiente:
                    if resultado.tracks and not resultado.api_candidates:
                        resultado.early_exit = "worker"
                    elif resultado.api_candidates and not resultado.tracks:
                        resultado.early_exit = "metadata"
                    else:
                        resultado.early_exit = "combinado"
                    await _cancelar_pendentes()
                    break
    except asyncio.CancelledError:
        await _cancelar_pendentes()
        raise

    resultado.elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    return resultado
