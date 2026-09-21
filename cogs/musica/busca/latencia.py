from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SnapshotHedgeBusca:
    api_amostras: int
    api_sucessos: int
    api_ewma_ms: float
    worker_amostras: int
    worker_ewma_ms: float
    metadata_amostras: int
    metadata_uteis: int
    metadata_ewma_ms: float
    deep_amostras: int
    deep_sucessos: int
    deep_ewma_ms: float


_API_AMOSTRAS = 0
_API_SUCESSOS = 0
_API_EWMA_MS = 0.0
_WORKER_AMOSTRAS = 0
_WORKER_EWMA_MS = 0.0
_METADATA_AMOSTRAS = 0
_METADATA_UTEIS = 0
_METADATA_EWMA_MS = 0.0
_DEEP_AMOSTRAS = 0
_DEEP_SUCESSOS = 0
_DEEP_EWMA_MS = 0.0
_ALPHA = 0.25


def _ewma(atual: float, valor: float, amostras: int) -> float:
    novo = max(0.0, float(valor or 0.0))
    if amostras <= 0:
        return novo
    return (atual * (1.0 - _ALPHA)) + (novo * _ALPHA)


def limpar_latencia_busca() -> None:
    global _API_AMOSTRAS, _API_SUCESSOS, _API_EWMA_MS
    global _WORKER_AMOSTRAS, _WORKER_EWMA_MS
    global _METADATA_AMOSTRAS, _METADATA_UTEIS, _METADATA_EWMA_MS
    global _DEEP_AMOSTRAS, _DEEP_SUCESSOS, _DEEP_EWMA_MS
    _API_AMOSTRAS = 0
    _API_SUCESSOS = 0
    _API_EWMA_MS = 0.0
    _WORKER_AMOSTRAS = 0
    _WORKER_EWMA_MS = 0.0
    _METADATA_AMOSTRAS = 0
    _METADATA_UTEIS = 0
    _METADATA_EWMA_MS = 0.0
    _DEEP_AMOSTRAS = 0
    _DEEP_SUCESSOS = 0
    _DEEP_EWMA_MS = 0.0


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


def registrar_latencia_metadata(*, elapsed_ms: float, util: bool) -> None:
    global _METADATA_AMOSTRAS, _METADATA_UTEIS, _METADATA_EWMA_MS
    _METADATA_EWMA_MS = _ewma(_METADATA_EWMA_MS, elapsed_ms, _METADATA_AMOSTRAS)
    _METADATA_AMOSTRAS += 1
    if util:
        _METADATA_UTEIS += 1


def registrar_latencia_deep(*, elapsed_ms: float, sucesso: bool) -> None:
    global _DEEP_AMOSTRAS, _DEEP_SUCESSOS, _DEEP_EWMA_MS
    _DEEP_EWMA_MS = _ewma(_DEEP_EWMA_MS, elapsed_ms, _DEEP_AMOSTRAS)
    _DEEP_AMOSTRAS += 1
    if sucesso:
        _DEEP_SUCESSOS += 1


def snapshot_latencia_busca() -> SnapshotHedgeBusca:
    return SnapshotHedgeBusca(
        api_amostras=_API_AMOSTRAS,
        api_sucessos=_API_SUCESSOS,
        api_ewma_ms=round(_API_EWMA_MS, 2),
        worker_amostras=_WORKER_AMOSTRAS,
        worker_ewma_ms=round(_WORKER_EWMA_MS, 2),
        metadata_amostras=_METADATA_AMOSTRAS,
        metadata_uteis=_METADATA_UTEIS,
        metadata_ewma_ms=round(_METADATA_EWMA_MS, 2),
        deep_amostras=_DEEP_AMOSTRAS,
        deep_sucessos=_DEEP_SUCESSOS,
        deep_ewma_ms=round(_DEEP_EWMA_MS, 2),
    )


def headstart_adaptativo(
    base_seconds: float,
    *,
    min_seconds: float = 0.03,
) -> float:
    """Calcula a vantagem curta da API sem deixar uma API lenta atrasar o worker."""
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


def grace_metadata_adaptativo(
    base_seconds: float,
    *,
    min_seconds: float = 0.015,
    min_samples: int = 4,
) -> float:
    """Encurta a espera pós-worker quando metadata costuma chegar tarde ou vazia.

    Nunca aumenta o grace configurado. Antes de haver histórico suficiente, o
    valor original é preservado. Assim um deploy/restart não começa agressivo.
    """
    base = max(0.0, float(base_seconds or 0.0))
    minimo = max(0.0, min(base, float(min_seconds or 0.0)))
    amostras_minimas = max(1, int(min_samples or 1))
    if base <= 0.0 or _METADATA_AMOSTRAS < amostras_minimas:
        return base

    taxa_util = _METADATA_UTEIS / max(1, _METADATA_AMOSTRAS)
    observado = _METADATA_EWMA_MS / 1000.0
    if taxa_util < 0.25:
        return minimo
    if observado >= base * 1.25:
        return minimo

    # Se costuma ser útil e termina dentro do grace, reserve só a folga que ela
    # demonstrou precisar; 25% cobre jitter sem voltar ao budget inteiro.
    alvo = observado * 1.25
    if taxa_util >= 0.70:
        alvo = observado * 1.35
    return max(minimo, min(base, alvo))


def timeout_deep_adaptativo(
    base_seconds: float,
    *,
    min_seconds: float = 3.0,
    min_samples: int = 4,
) -> float:
    """Reduz apenas a cauda do deep pass com base no tempo observado.

    O timeout nunca excede o valor configurado e nunca fica abaixo do piso de
    segurança. Passagens rápidas continuam retornando no mesmo instante; o ganho
    aparece quando um provider/worker trava depois de um histórico estável.
    """
    base = max(0.0, float(base_seconds or 0.0))
    minimo = max(0.0, min(base, float(min_seconds or 0.0)))
    amostras_minimas = max(1, int(min_samples or 1))
    if base <= 0.0 or _DEEP_AMOSTRAS < amostras_minimas:
        return base

    taxa_sucesso = _DEEP_SUCESSOS / max(1, _DEEP_AMOSTRAS)
    observado = _DEEP_EWMA_MS / 1000.0
    multiplicador = 2.4 if taxa_sucesso >= 0.50 else 1.8
    margem = 0.20 if taxa_sucesso >= 0.50 else 0.10
    alvo = (observado * multiplicador) + margem
    return max(minimo, min(base, alvo))
