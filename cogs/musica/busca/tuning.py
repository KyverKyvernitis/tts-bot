from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..nucleo.modelos import MusicTrack
from .modelos import ResultadoRanking


@dataclass(frozen=True, slots=True)
class DecisaoGanhoProfundo:
    aplicar: bool
    motivo: str
    delta_score: float = 0.0
    delta_confianca: float = 0.0
    delta_media_top: float = 0.0
    delta_resultados: int = 0


def _media_top(ranking: Sequence[ResultadoRanking], limite: int = 3) -> float:
    amostra = list(ranking[: max(1, int(limite or 1))])
    if not amostra:
        return 0.0
    return sum(float(item.score) for item in amostra) / len(amostra)


def avaliar_ganho_busca_profunda(
    fast_tracks: Sequence[MusicTrack],
    fast_ranking: Sequence[ResultadoRanking],
    deep_tracks: Sequence[MusicTrack],
    deep_ranking: Sequence[ResultadoRanking],
    *,
    requested_limit: int,
    min_score_gain: float = 0.012,
    min_confidence_gain: float = 0.030,
    min_average_gain: float = 0.015,
) -> DecisaoGanhoProfundo:
    """Aceita o deep pass somente quando ele produz ganho observável.

    A busca profunda nunca deve piorar um fast pass já útil só porque conseguiu
    retornar dados. O gate usa sinais que já existem em memória; não faz I/O.
    """
    fast_tracks = list(fast_tracks)
    deep_tracks = list(deep_tracks)
    fast_ranking = list(fast_ranking)
    deep_ranking = list(deep_ranking)
    limite = max(1, int(requested_limit or 1))

    if not deep_tracks:
        return DecisaoGanhoProfundo(False, "sem_resultados")
    if not fast_tracks:
        return DecisaoGanhoProfundo(True, "primeiro_resultado", delta_resultados=len(deep_tracks))

    fast_top = fast_ranking[0] if fast_ranking else None
    deep_top = deep_ranking[0] if deep_ranking else None
    fast_score = float(fast_top.score if fast_top is not None else 0.0)
    deep_score = float(deep_top.score if deep_top is not None else 0.0)
    fast_conf = float(fast_top.confianca if fast_top is not None else 0.0)
    deep_conf = float(deep_top.confianca if deep_top is not None else 0.0)
    delta_score = deep_score - fast_score
    delta_conf = deep_conf - fast_conf
    delta_media = _media_top(deep_ranking) - _media_top(fast_ranking)
    delta_resultados = min(limite, len(deep_tracks)) - min(limite, len(fast_tracks))

    if delta_score >= max(0.0, float(min_score_gain)):
        motivo = "top_score_melhor"
        aplicar = True
    elif delta_conf >= max(0.0, float(min_confidence_gain)) and deep_score >= fast_score - 0.002:
        motivo = "confianca_melhor"
        aplicar = True
    elif len(fast_tracks) < limite and delta_resultados > 0:
        motivo = "preencheu_resultados"
        aplicar = True
    elif delta_media >= max(0.0, float(min_average_gain)) and deep_score >= fast_score - 0.002:
        motivo = "top3_melhor"
        aplicar = True
    else:
        motivo = "sem_ganho_relevante"
        aplicar = False

    return DecisaoGanhoProfundo(
        aplicar,
        motivo,
        round(delta_score, 4),
        round(delta_conf, 4),
        round(delta_media, 4),
        int(delta_resultados),
    )
