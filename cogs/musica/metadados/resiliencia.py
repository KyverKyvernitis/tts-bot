from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import logging
import time
from typing import Awaitable, Callable, TypeVar

from .modelos import ApiTrackCandidate

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(slots=True)
class EstadoCircuitoProvider:
    falhas_consecutivas: int = 0
    aberto_ate: float = 0.0
    ultima_falha: str = ""


_CACHE_METADATA: dict[tuple[str, str, int], tuple[float, list[ApiTrackCandidate]]] = {}
_EM_VOO_METADATA: dict[tuple[str, str, int], asyncio.Task[list[ApiTrackCandidate]]] = {}
_CIRCUITOS: dict[str, EstadoCircuitoProvider] = {}
_MAX_CIRCUITOS = 16


def _agora() -> float:
    return time.monotonic()


def _chave_metadata(query: str, limit: int, namespace: str = "all") -> tuple[str, str, int]:
    return (
        str(namespace or "all").strip().lower() or "all",
        " ".join(str(query or "").lower().split()),
        max(1, min(10, int(limit or 3))),
    )


def _copiar_candidatos(candidatos: list[ApiTrackCandidate]) -> list[ApiTrackCandidate]:
    # ApiTrackCandidate e ``extra`` sao mutaveis. O cache nunca entrega a
    # mesma instancia para dois requests para evitar contaminacao de score.
    return copy.deepcopy(list(candidatos))


def limpar_resiliencia_metadata() -> None:
    for task in tuple(_EM_VOO_METADATA.values()):
        if not task.done():
            task.cancel()
    _EM_VOO_METADATA.clear()
    _CACHE_METADATA.clear()
    _CIRCUITOS.clear()


def buscas_metadata_em_voo() -> int:
    return sum(1 for task in _EM_VOO_METADATA.values() if not task.done())


def estado_circuito_provider(nome: str) -> EstadoCircuitoProvider:
    estado = _CIRCUITOS.get(str(nome or "").strip().lower())
    if estado is None:
        return EstadoCircuitoProvider()
    return EstadoCircuitoProvider(
        falhas_consecutivas=estado.falhas_consecutivas,
        aberto_ate=estado.aberto_ate,
        ultima_falha=estado.ultima_falha,
    )


def _podar_cache(*, max_itens: int) -> None:
    limite = max(1, int(max_itens or 1))
    while len(_CACHE_METADATA) > limite:
        chave = min(_CACHE_METADATA.items(), key=lambda item: item[1][0])[0]
        _CACHE_METADATA.pop(chave, None)


def _limpar_task_metadata(chave: tuple[str, str, int], task: asyncio.Task[list[ApiTrackCandidate]]) -> None:
    if _EM_VOO_METADATA.get(chave) is task:
        _EM_VOO_METADATA.pop(chave, None)
    # Consumir excecao evita warning caso todos os callers tenham sido
    # cancelados enquanto a operacao compartilhada terminava.
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


async def buscar_metadata_compartilhada(
    query: str,
    *,
    limit: int,
    produtor: Callable[[], Awaitable[list[ApiTrackCandidate]]],
    ttl_seconds: float,
    max_itens: int = 64,
    namespace: str = "all",
) -> list[ApiTrackCandidate]:
    """Cache curto + singleflight para a busca multi-provider.

    Cancelar um caller nao cancela a busca compartilhada dos demais. O cache
    aceita inclusive resultado vazio por um TTL curto para impedir rajadas de
    chamadas repetidas quando nenhum provider encontra a faixa.
    """
    chave = _chave_metadata(query, limit, namespace)
    ttl = max(0.0, float(ttl_seconds or 0.0))
    now = _agora()
    cached = _CACHE_METADATA.get(chave)
    if ttl > 0.0 and cached is not None:
        criado, candidatos = cached
        if now - criado <= ttl:
            return _copiar_candidatos(candidatos)
        _CACHE_METADATA.pop(chave, None)

    task = _EM_VOO_METADATA.get(chave)
    if task is None or task.done():
        async def _executar() -> list[ApiTrackCandidate]:
            resultado = list(await produtor())
            if ttl > 0.0:
                _CACHE_METADATA[chave] = (_agora(), _copiar_candidatos(resultado))
                _podar_cache(max_itens=max_itens)
            return resultado

        task = asyncio.create_task(_executar())
        _EM_VOO_METADATA[chave] = task
        task.add_done_callback(lambda done, key=chave: _limpar_task_metadata(key, done))

    resultado = await asyncio.shield(task)
    return _copiar_candidatos(resultado)


def _estado_mutavel(nome: str) -> EstadoCircuitoProvider:
    chave = str(nome or "provider").strip().lower() or "provider"
    estado = _CIRCUITOS.get(chave)
    if estado is None:
        if len(_CIRCUITOS) >= _MAX_CIRCUITOS:
            # Numero de providers e pequeno; esta poda so protege contra uso
            # acidental de nomes dinamicos sem deixar o mapa crescer.
            _CIRCUITOS.pop(next(iter(_CIRCUITOS)), None)
        estado = EstadoCircuitoProvider()
        _CIRCUITOS[chave] = estado
    return estado


async def executar_provider_resiliente(
    nome: str,
    operacao: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float,
    falhas_para_abrir: int,
    cooldown_seconds: float,
    fallback: T,
) -> T:
    """Aplica timeout e circuit breaker leve a um provider de metadata."""
    chave = str(nome or "provider").strip().lower() or "provider"
    estado = _estado_mutavel(chave)
    now = _agora()
    if estado.aberto_ate > now:
        logger.debug(
            "[music/search] provider circuit aberto | provider=%s restante_ms=%.0f",
            chave,
            (estado.aberto_ate - now) * 1000.0,
        )
        return copy.deepcopy(fallback)

    timeout = max(0.05, float(timeout_seconds or 0.05))
    limite_falhas = max(1, int(falhas_para_abrir or 1))
    cooldown = max(0.1, float(cooldown_seconds or 0.1))
    try:
        resultado = await asyncio.wait_for(operacao(), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        estado.falhas_consecutivas += 1
        estado.ultima_falha = f"{type(exc).__name__}: {exc}"[:240]
        if estado.falhas_consecutivas >= limite_falhas:
            estado.aberto_ate = _agora() + cooldown
            logger.warning(
                "[music/search] provider circuit aberto | provider=%s falhas=%s cooldown=%.1fs erro=%s",
                chave,
                estado.falhas_consecutivas,
                cooldown,
                estado.ultima_falha,
            )
        else:
            logger.debug(
                "[music/search] provider falhou | provider=%s falhas=%s/%s erro=%s",
                chave,
                estado.falhas_consecutivas,
                limite_falhas,
                estado.ultima_falha,
            )
        return copy.deepcopy(fallback)

    estado.falhas_consecutivas = 0
    estado.aberto_ate = 0.0
    estado.ultima_falha = ""
    return resultado
