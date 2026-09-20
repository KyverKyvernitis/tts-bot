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
) -> ResultadoPassagemProfunda:
    """Executa worker + metadata em paralelo e nunca invalida o fast pass."""
    started = time.monotonic()
    payload = montar_tarefa_resolucao(
        query=query,
        limit=limit,
        timeout_seconds=timeout_seconds,
        somente_metadados=True,
        permitir_playlist=False,
        busca_textual=True,
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

    worker_result, metadata_result = await asyncio.gather(
        worker_task,
        metadata_task,
        return_exceptions=True,
    )
    resultado = ResultadoPassagemProfunda()

    if isinstance(worker_result, BaseException):
        resultado.worker_error = str(worker_result)
    elif isinstance(worker_result, Mapping):
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

    if isinstance(metadata_result, BaseException):
        resultado.api_error = str(metadata_result)
    elif isinstance(metadata_result, list):
        resultado.api_candidates = metadata_result

    resultado.elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    return resultado
