from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SnapshotTelemetriaBusca:
    buscas: int
    cache_hits: int
    deep_solicitadas: int
    deep_aplicadas: int
    deep_rejeitadas: int
    deep_suprimidas: int
    deep_sem_resultado: int
    selecoes: int
    selecoes_primeiro: int
    selecoes_top3: int
    latencia_media_ms: float
    latencia_max_ms: float
    score_medio: float
    confianca_media: float
    fontes: tuple[tuple[str, int], ...]
    motivos_deep: tuple[tuple[str, int], ...]


_BUSCAS = 0
_CACHE_HITS = 0
_DEEP_SOLICITADAS = 0
_DEEP_APLICADAS = 0
_DEEP_REJEITADAS = 0
_DEEP_SUPRIMIDAS = 0
_DEEP_SEM_RESULTADO = 0
_SELECOES = 0
_SELECOES_PRIMEIRO = 0
_SELECOES_TOP3 = 0
_LATENCIA_TOTAL_MS = 0.0
_LATENCIA_MAX_MS = 0.0
_SCORE_TOTAL = 0.0
_CONFIANCA_TOTAL = 0.0
_RANKINGS_COM_SCORE = 0
_FONTES: dict[str, int] = {}
_MOTIVOS_DEEP: dict[str, int] = {}
_ULTIMO_RESUMO_EM = 0
_MAX_CHAVES = 16


def limpar_telemetria_busca() -> None:
    global _BUSCAS, _CACHE_HITS, _DEEP_SOLICITADAS, _DEEP_APLICADAS
    global _DEEP_REJEITADAS, _DEEP_SUPRIMIDAS, _DEEP_SEM_RESULTADO
    global _SELECOES, _SELECOES_PRIMEIRO, _SELECOES_TOP3
    global _LATENCIA_TOTAL_MS, _LATENCIA_MAX_MS, _SCORE_TOTAL, _CONFIANCA_TOTAL
    global _RANKINGS_COM_SCORE, _ULTIMO_RESUMO_EM
    _BUSCAS = 0
    _CACHE_HITS = 0
    _DEEP_SOLICITADAS = 0
    _DEEP_APLICADAS = 0
    _DEEP_REJEITADAS = 0
    _DEEP_SUPRIMIDAS = 0
    _DEEP_SEM_RESULTADO = 0
    _SELECOES = 0
    _SELECOES_PRIMEIRO = 0
    _SELECOES_TOP3 = 0
    _LATENCIA_TOTAL_MS = 0.0
    _LATENCIA_MAX_MS = 0.0
    _SCORE_TOTAL = 0.0
    _CONFIANCA_TOTAL = 0.0
    _RANKINGS_COM_SCORE = 0
    _ULTIMO_RESUMO_EM = 0
    _FONTES.clear()
    _MOTIVOS_DEEP.clear()


def _incrementar_mapa(mapa: dict[str, int], chave: str) -> None:
    nome = str(chave or "desconhecida").strip().lower()[:40] or "desconhecida"
    if nome not in mapa and len(mapa) >= _MAX_CHAVES:
        nome = "outros"
    mapa[nome] = mapa.get(nome, 0) + 1


def _normalizar_fonte(fonte: str) -> str:
    raw = str(fonte or "").strip().lower()
    if not raw:
        return "desconhecida"
    for nome in ("youtube", "spotify", "deezer", "soundcloud", "apple"):
        if nome in raw:
            return nome
    return raw[:24]


def snapshot_telemetria_busca() -> SnapshotTelemetriaBusca:
    divisor = max(1, _BUSCAS)
    divisor_score = max(1, _RANKINGS_COM_SCORE)
    return SnapshotTelemetriaBusca(
        buscas=_BUSCAS,
        cache_hits=_CACHE_HITS,
        deep_solicitadas=_DEEP_SOLICITADAS,
        deep_aplicadas=_DEEP_APLICADAS,
        deep_rejeitadas=_DEEP_REJEITADAS,
        deep_suprimidas=_DEEP_SUPRIMIDAS,
        deep_sem_resultado=_DEEP_SEM_RESULTADO,
        selecoes=_SELECOES,
        selecoes_primeiro=_SELECOES_PRIMEIRO,
        selecoes_top3=_SELECOES_TOP3,
        latencia_media_ms=round(_LATENCIA_TOTAL_MS / divisor, 2),
        latencia_max_ms=round(_LATENCIA_MAX_MS, 2),
        score_medio=round(_SCORE_TOTAL / divisor_score, 4),
        confianca_media=round(_CONFIANCA_TOTAL / divisor_score, 4),
        fontes=tuple(sorted(_FONTES.items(), key=lambda item: (-item[1], item[0]))),
        motivos_deep=tuple(sorted(_MOTIVOS_DEEP.items(), key=lambda item: (-item[1], item[0]))),
    )


def registrar_busca_telemetria(
    *,
    cache_hit: bool = False,
    deep_estado: str = "nao_necessario",
    deep_motivo: str = "",
    top_score: float | None = None,
    top_confianca: float | None = None,
    elapsed_ms: float = 0.0,
    fontes: Iterable[str] = (),
    resumo_cada: int = 25,
) -> SnapshotTelemetriaBusca | None:
    global _BUSCAS, _CACHE_HITS, _DEEP_SOLICITADAS, _DEEP_APLICADAS
    global _DEEP_REJEITADAS, _DEEP_SUPRIMIDAS, _DEEP_SEM_RESULTADO
    global _LATENCIA_TOTAL_MS, _LATENCIA_MAX_MS, _SCORE_TOTAL, _CONFIANCA_TOTAL
    global _RANKINGS_COM_SCORE, _ULTIMO_RESUMO_EM

    _BUSCAS += 1
    if cache_hit:
        _CACHE_HITS += 1

    estado = str(deep_estado or "nao_necessario").strip().lower()
    if estado in {"aplicado", "rejeitado", "suprimido", "sem_resultado"}:
        _DEEP_SOLICITADAS += 1
    if estado == "aplicado":
        _DEEP_APLICADAS += 1
    elif estado == "rejeitado":
        _DEEP_REJEITADAS += 1
    elif estado == "suprimido":
        _DEEP_SUPRIMIDAS += 1
    elif estado == "sem_resultado":
        _DEEP_SEM_RESULTADO += 1

    if deep_motivo:
        _incrementar_mapa(_MOTIVOS_DEEP, deep_motivo)

    latencia = max(0.0, float(elapsed_ms or 0.0))
    _LATENCIA_TOTAL_MS += latencia
    _LATENCIA_MAX_MS = max(_LATENCIA_MAX_MS, latencia)

    if top_score is not None or top_confianca is not None:
        _SCORE_TOTAL += max(0.0, min(1.0, float(top_score or 0.0)))
        _CONFIANCA_TOTAL += max(0.0, min(1.0, float(top_confianca or 0.0)))
        _RANKINGS_COM_SCORE += 1

    vistas: set[str] = set()
    for fonte in fontes:
        nome = _normalizar_fonte(fonte)
        if nome in vistas:
            continue
        vistas.add(nome)
        _incrementar_mapa(_FONTES, nome)

    intervalo = max(1, int(resumo_cada or 1))
    if _BUSCAS - _ULTIMO_RESUMO_EM >= intervalo:
        _ULTIMO_RESUMO_EM = _BUSCAS
        return snapshot_telemetria_busca()
    return None


def registrar_selecao_telemetria(*, posicao: int = 0, total: int = 0, fonte: str = "") -> None:
    global _SELECOES, _SELECOES_PRIMEIRO, _SELECOES_TOP3
    _SELECOES += 1
    pos = max(0, int(posicao or 0))
    if pos == 1:
        _SELECOES_PRIMEIRO += 1
    if 1 <= pos <= 3:
        _SELECOES_TOP3 += 1
    if fonte:
        _incrementar_mapa(_FONTES, _normalizar_fonte(fonte))
