from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from ..nucleo.modelos import MusicTrack
from .intencao import analisar_consulta
from .modelos import ResultadoRanking

_ATRIBUTO_PARA_BUSCA = {
    "live": "live",
    "remix": "remix",
    "cover": "cover",
    "karaoke": "karaoke",
    "instrumental": "instrumental",
    "slowed": "slowed",
    "sped_up": "sped up",
    "reverb": "reverb",
    "acoustic": "acoustic",
    "extended": "extended",
    "edit": "edit",
    "remaster": "remaster",
    "clean": "clean version",
    "explicit": "explicit",
}

_APRESENTACAO_PARA_BUSCA = {
    "official": "official",
    "lyrics": "lyrics",
    "audio": "audio",
    "video": "video",
    "visualizer": "visualizer",
}
_APRESENTACAO_ORDEM = ("official", "audio", "video", "lyrics", "visualizer")

_CANAL_TOPIC = re.compile(r"\s+-\s+topic\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DecisaoBuscaProfunda:
    executar: bool
    motivo: str
    query: str
    limit: int
    top_score: float = 0.0
    top_confianca: float = 0.0
    margem: float = 0.0


def _query_canonica(query: str, top_track: MusicTrack | None, top_score: float) -> str:
    consulta = analisar_consulta(query)
    raw = consulta.raw or str(query or "").strip()
    if consulta.artista and consulta.titulo:
        partes = [consulta.artista, consulta.titulo]
        partes.extend(nome for nome in consulta.colaboradores if nome)
        base = " ".join(parte for parte in partes if parte).strip()
        padded = f" {base} "
        for nome in sorted(consulta.atributos):
            frase = _ATRIBUTO_PARA_BUSCA.get(nome, "")
            if frase and f" {frase} " not in padded:
                partes.append(frase)
                padded = " " + " ".join(partes) + " "
        for nome in _APRESENTACAO_ORDEM:
            if nome not in consulta.apresentacao:
                continue
            frase = _APRESENTACAO_PARA_BUSCA.get(nome, "")
            if frase and f" {frase} " not in padded:
                partes.append(frase)
                padded = " " + " ".join(partes) + " "
        return " ".join(parte for parte in partes if parte).strip() or raw

    # Em typo/consulta livre, um candidato moderadamente coerente pode fornecer
    # a grafia canônica para a segunda passagem. O ranking final continua usando
    # a consulta original, então essa âncora não decide sozinha o resultado.
    if top_track is not None and top_score >= 0.60:
        artista = (top_track.display_uploader or top_track.uploader or "").strip()
        titulo = (top_track.display_title or top_track.title or "").strip()
        artista = _CANAL_TOPIC.sub("", artista).strip()
        if titulo and artista:
            return f"{artista} {titulo}".strip()
        if titulo:
            return titulo
    return raw


def avaliar_busca_profunda(
    query: str,
    tracks: Sequence[MusicTrack],
    ranking: Sequence[ResultadoRanking],
    *,
    requested_limit: int,
    enabled: bool = True,
    deep_limit: int = 5,
    min_results: int = 3,
    score_threshold: float = 0.66,
    confidence_threshold: float = 0.55,
    margin_threshold: float = 0.030,
) -> DecisaoBuscaProfunda:
    """Decide se uma busca textual merece uma segunda passagem mais ampla.

    A decisão usa somente sinais baratos já calculados pelo ranking. Não há
    rede, modelo externo ou dependência pesada nesta etapa.
    """
    try:
        limite_pedido = max(1, min(10, int(requested_limit or 3)))
    except Exception:
        limite_pedido = 3
    try:
        limite_profundo = max(limite_pedido, min(10, int(deep_limit or 5)))
    except Exception:
        limite_profundo = max(limite_pedido, 5)

    if not enabled:
        return DecisaoBuscaProfunda(False, "desativada", str(query or "").strip(), limite_profundo)

    top = ranking[0] if ranking else None
    top_score = float(top.score if top is not None else 0.0)
    top_confianca = float(top.confianca if top is not None else 0.0)
    segundo_score = float(ranking[1].score if len(ranking) > 1 else 0.0)
    margem = max(0.0, top_score - segundo_score)
    top_track = tracks[0] if tracks else None
    query_profunda = _query_canonica(query, top_track, top_score)

    if not tracks:
        motivo = "sem_resultados"
    elif len(tracks) < min(max(1, int(min_results or 3)), limite_pedido):
        motivo = "poucos_resultados"
    elif top is None:
        motivo = "sem_ranking"
    elif top_score < float(score_threshold):
        motivo = "score_baixo"
    elif top_confianca < float(confidence_threshold):
        motivo = "confianca_baixa"
    elif len(ranking) > 1 and margem < float(margin_threshold) and top_score < 0.94:
        motivo = "resultados_ambiguos"
    else:
        return DecisaoBuscaProfunda(
            False,
            "primeira_passagem_suficiente",
            query_profunda,
            limite_profundo,
            round(top_score, 4),
            round(top_confianca, 4),
            round(margem, 4),
        )

    return DecisaoBuscaProfunda(
        True,
        motivo,
        query_profunda,
        limite_profundo,
        round(top_score, 4),
        round(top_confianca, 4),
        round(margem, 4),
    )
