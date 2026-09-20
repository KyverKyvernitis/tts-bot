from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SnapshotHedgeBusca:
    api_amostras: int
    api_sucessos: int
    api_ewma_ms: float
    worker_amostras: int
    worker_ewma_ms: float


_API_AMOSTRAS = 0
_API_SUCESSOS = 0
_API_EWMA_MS = 0.0
_WORKER_AMOSTRAS = 0
_WORKER_EWMA_MS = 0.0
_ALPHA = 0.25


def _ewma(atual: float, valor: float, amostras: int) -> float:
    novo = max(0.0, float(valor or 0.0))
    if amostras <= 0:
        return novo
    return (atual * (1.0 - _ALPHA)) + (novo * _ALPHA)


def limpar_latencia_busca() -> None:
    global _API_AMOSTRAS, _API_SUCESSOS, _API_EWMA_MS
    global _WORKER_AMOSTRAS, _WORKER_EWMA_MS
    _API_AMOSTRAS = 0
    _API_SUCESSOS = 0
    _API_EWMA_MS = 0.0
    _WORKER_AMOSTRAS = 0
    _WORKER_EWMA_MS = 0.0


def registrar_latencia_api(*, elapsed_ms: float, suficiente: bool) -> None:
    global _API_AMOSTRAS, _API_SUCESSOS, _API_EWMA_MS
    _API_EWMA_MS = _ewma(_API_EWMA_MS, elapsed_ms, _API_AMOSTRAS)
    _API_AMOSTRAS += 1
    if suficiente:
        _API_SUCESSOS += 1


def registrar_latencia_worker(*, elapsed_ms: float) -> None:
    global _WORKER_AMOSTRAS, _WORKER_EWMA_MS
    _WORKER_EWMA_MS = _ewma(_WORKER_EWMA_MS, elapsed_ms, _WORKER_AMOSTRAS)
    _WORKER_AMOSTRAS += 1


def snapshot_latencia_busca() -> SnapshotHedgeBusca:
    return SnapshotHedgeBusca(
        api_amostras=_API_AMOSTRAS,
        api_sucessos=_API_SUCESSOS,
        api_ewma_ms=round(_API_EWMA_MS, 2),
        worker_amostras=_WORKER_AMOSTRAS,
        worker_ewma_ms=round(_WORKER_EWMA_MS, 2),
    )


def headstart_adaptativo(
    base_seconds: float,
    *,
    min_seconds: float = 0.03,
) -> float:
    """Calcula a vantagem curta da API sem deixar uma API lenta atrasar o worker.

    O estado e global e agregado: nao guarda query, guild ou usuario. Antes de
    haver amostras suficientes, preserva o valor configurado. Depois, uma API
    lenta/instavel perde vantagem; uma API consistentemente rapida recebe apenas
    o tempo necessario para ter chance real de concluir antes do Phone Worker.
    """
    base = max(0.0, float(base_seconds or 0.0))
    minimo = max(0.0, min(base, float(min_seconds or 0.0)))
    if base <= 0.0 or _API_AMOSTRAS < 3:
        return base

    taxa_sucesso = _API_SUCESSOS / max(1, _API_AMOSTRAS)
    api_seconds = _API_EWMA_MS / 1000.0
    worker_seconds = _WORKER_EWMA_MS / 1000.0 if _WORKER_AMOSTRAS else 0.0

    if taxa_sucesso < 0.40:
        return minimo
    if worker_seconds > 0.0 and api_seconds >= worker_seconds * 0.65:
        return minimo
    if api_seconds >= 0.30:
        return max(minimo, min(base, 0.05))
    if api_seconds >= 0.18:
        return max(minimo, min(base, 0.08))

    alvo = max(minimo, api_seconds * 1.15)
    return min(base, alvo)
