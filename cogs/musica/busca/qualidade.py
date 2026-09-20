from __future__ import annotations

from ..nucleo.modelos import MusicTrack
from .modelos import ConsultaNormalizada
from .normalizacao import texto_basico, tokens_texto

_FONTES_CATALOGO = ("spotify", "deezer", "apple music", "tidal")
_CONSULTA_LONGA = (
    "full album",
    "album completo",
    "playlist",
    "compilation",
    "compilacao",
    "mix",
    "dj set",
    "extended",
    "1 hour",
    "one hour",
    "10 hour",
    "10 hours",
)
_TITULOS_GENERICOS = {
    "music",
    "musica",
    "audio",
    "video",
    "song",
    "track",
    "unknown",
    "untitled",
}


def _consulta_aceita_conteudo_longo(intencao: ConsultaNormalizada) -> bool:
    raw = texto_basico(intencao.raw)
    padded = " " + raw + " "
    return any((" " + termo + " ") in padded for termo in _CONSULTA_LONGA)


def _fonte_catalogo(track: MusicTrack) -> bool:
    source = texto_basico(" ".join(part for part in (track.source, track.display_source) if part))
    return any(nome in source for nome in _FONTES_CATALOGO)


def sinal_qualidade(intencao: ConsultaNormalizada, track: MusicTrack) -> tuple[float, float]:
    """Retorna bônus e penalidade leves de qualidade do candidato.

    Estes sinais nunca tentam substituir a relevância semântica. Eles apenas
    desempatarão candidatos plausíveis usando metadata já disponível, sem nova
    chamada de rede e sem resolver áudio antecipadamente.
    """
    bonus = 0.0
    penalidade = 0.0

    titulo = (track.display_title or track.title or "").strip()
    uploader = (track.display_uploader or track.uploader or "").strip()
    titulo_tokens = tokens_texto(titulo, remover_ruido=True)

    if _fonte_catalogo(track):
        bonus += 0.018

    duracao = track.duration
    if duracao:
        seconds = float(duracao)
        if 45.0 <= seconds <= 900.0:
            bonus += 0.018
        elif seconds < 20.0:
            penalidade += 0.075
        elif seconds < 35.0:
            penalidade += 0.035
        elif seconds > 3600.0 and not _consulta_aceita_conteudo_longo(intencao):
            penalidade += 0.10
        elif seconds > 1200.0 and not _consulta_aceita_conteudo_longo(intencao):
            penalidade += 0.055

    titulo_basico = texto_basico(titulo)
    if not titulo_basico or titulo_basico in _TITULOS_GENERICOS:
        penalidade += 0.09
    elif len(titulo_tokens) >= 2:
        bonus += 0.008

    if not uploader:
        penalidade += 0.012

    # Metadados de live devem continuar válidos quando o usuário realmente
    # pediu "live"; a duração em si não é usada para despromover streams ao vivo.
    if track.is_live and "live" not in intencao.atributos:
        penalidade += 0.025

    return min(0.05, bonus), min(0.16, penalidade)
