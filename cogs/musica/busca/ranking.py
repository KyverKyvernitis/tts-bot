from __future__ import annotations

from difflib import SequenceMatcher
from math import exp
import re
from typing import Iterable, Sequence

from ..nucleo.modelos import MusicTrack
from .atributos import (
    FORMATOS_APRESENTACAO,
    VARIANTES_FORTES,
    VARIANTES_LEVES,
    detectar_apresentacao,
    detectar_variantes,
)
from .diversidade import diversificar_resultados
from .intencao import analisar_consulta
from .modelos import ConsultaNormalizada, ResultadoRanking, SinaisCandidato
from .normalizacao import limpar_apresentacao, tokens_texto
from .qualidade import sinal_qualidade

_OFICIALIDADE = (
    "official audio",
    "official video",
    "official music video",
    "audio oficial",
    "video oficial",
)
_CANAL_OFICIAL = (" vevo", "official", " - topic", "records", "recordings")



_FEAT_NO_TITULO = re.compile(r"\s+\b(?:feat(?:uring)?|ft)\.?\s+.*$", re.IGNORECASE)


def _titulo_identidade(track: MusicTrack) -> str:
    raw = track.display_title or track.title or ""
    titulo = limpar_apresentacao(raw)
    uploader = limpar_apresentacao(track.display_uploader or track.uploader or "")
    if uploader and titulo.startswith(uploader + " "):
        titulo = titulo[len(uploader) + 1 :].strip()
    titulo = _FEAT_NO_TITULO.sub("", titulo).strip()
    return titulo or limpar_apresentacao(raw)

def _similaridade(a: str, b: str) -> float:
    a = limpar_apresentacao(a)
    b = limpar_apresentacao(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _cobertura(query_tokens: Iterable[str], candidate: str) -> float:
    q = set(query_tokens)
    c = set(tokens_texto(candidate, remover_ruido=True))
    if not q or not c:
        return 0.0
    return len(q & c) / len(q)


def _ordem(query: str, candidate: str) -> float:
    q = limpar_apresentacao(query)
    c = limpar_apresentacao(candidate)
    if not q or not c:
        return 0.0
    if q in c:
        return 1.0
    return _similaridade(q, c)


def _texto_candidato(track: MusicTrack) -> str:
    return " ".join(
        part for part in (track.title, track.display_title, track.uploader, track.display_uploader) if part
    )


def _sinal_versao(intencao: ConsultaNormalizada, track: MusicTrack) -> tuple[float, float]:
    presentes = set(detectar_variantes(_texto_candidato(track)))
    pedidos = set(intencao.atributos)
    bonus = 0.0
    penalidade = 0.0

    if pedidos:
        correspondentes = len(pedidos & presentes)
        ausentes = len(pedidos - presentes)
        conflitantes = len(presentes - pedidos)
        bonus += 0.14 * correspondentes
        penalidade += 0.08 * ausentes
        penalidade += 0.055 * conflitantes

        # Clean e explicit são mutuamente exclusivos e merecem uma separação
        # maior que um simples qualificador ausente.
        if ("clean" in pedidos and "explicit" in presentes) or (
            "explicit" in pedidos and "clean" in presentes
        ):
            penalidade += 0.10
    else:
        # Versões alteradas são úteis quando pedidas, mas costumam ser resultados
        # piores para uma consulta neutra pela faixa original. Clean/explicit e
        # remaster não recebem punição automática: podem ser a edição canônica.
        fortes = presentes & set(VARIANTES_FORTES)
        leves = presentes & set(VARIANTES_LEVES)
        penalidade += min(0.30, 0.12 * len(fortes) + 0.055 * len(leves))

    return min(0.28, bonus), min(0.42, penalidade)


def _sinal_apresentacao(intencao: ConsultaNormalizada, track: MusicTrack) -> tuple[float, float]:
    pedidos = set(intencao.apresentacao)
    if not pedidos:
        return 0.0, 0.0

    presentes = set(detectar_apresentacao(_texto_candidato(track)))
    correspondentes = len(pedidos & presentes)
    ausentes = len(pedidos - presentes)
    bonus = 0.055 * correspondentes
    penalidade = 0.025 * ausentes

    formato_pedido = pedidos & set(FORMATOS_APRESENTACAO)
    formato_presente = presentes & set(FORMATOS_APRESENTACAO)
    if formato_pedido and formato_presente and not (formato_pedido & formato_presente):
        penalidade += 0.085
    if "official" in pedidos and "official" not in presentes:
        penalidade += 0.035

    return min(0.16, bonus), min(0.20, penalidade)


def _oficialidade(track: MusicTrack) -> float:
    title = " ".join(tokens_texto(track.title or track.display_title))
    uploader = " ".join(tokens_texto(track.uploader or track.display_uploader))
    signal = 0.0
    if any(frase in title for frase in _OFICIALIDADE):
        signal += 0.035
    padded = f" {uploader} "
    if any(hint in padded for hint in _CANAL_OFICIAL):
        signal += 0.045
    return min(0.07, signal)


def _confidence(score: float, margin: float) -> float:
    # Confiança não é probabilidade estatística. É um sinal de separação útil
    # para fases posteriores decidirem se vale executar uma busca profunda.
    base = 1.0 / (1.0 + exp(-10.0 * (score - 0.57)))
    separation = min(1.0, max(0.0, margin) / 0.18)
    return round(min(1.0, 0.78 * base + 0.22 * separation), 4)


def pontuar_faixa(query: str | ConsultaNormalizada, track: MusicTrack, *, indice: int = 0) -> ResultadoRanking:
    intencao = query if isinstance(query, ConsultaNormalizada) else analisar_consulta(query)
    titulo = _titulo_identidade(track)
    artista = track.display_uploader or track.uploader
    combinado = " ".join(part for part in (artista, titulo) if part)
    texto_completo = _texto_candidato(track)

    alvo_titulo = intencao.titulo or intencao.texto
    titulo_score = _similaridade(alvo_titulo, titulo)
    artista_score = _similaridade(intencao.artista, artista) if intencao.artista else 0.0
    colaborador_score = 0.0
    if intencao.colaboradores:
        colaborador_score = max(
            _cobertura(tokens_texto(nome), texto_completo) for nome in intencao.colaboradores
        )
    cobertura = _cobertura(intencao.tokens, texto_completo)
    ordem = _ordem(intencao.texto, combinado)
    oficial = _oficialidade(track)
    versao, penalidade_versao = _sinal_versao(intencao, track)
    apresentacao, penalidade_apresentacao = _sinal_apresentacao(intencao, track)
    qualidade, penalidade_qualidade = sinal_qualidade(intencao, track)
    penalidade_semantica = penalidade_versao + penalidade_apresentacao
    penalidade = penalidade_semantica + penalidade_qualidade

    if intencao.artista:
        if intencao.colaboradores:
            identidade = (
                0.50 * titulo_score
                + 0.19 * artista_score
                + 0.08 * colaborador_score
                + 0.16 * cobertura
                + 0.07 * ordem
            )
        else:
            identidade = 0.54 * titulo_score + 0.20 * artista_score + 0.18 * cobertura + 0.08 * ordem
    else:
        # Consultas livres não devem depender de inferir qual termo é artista.
        identidade = 0.46 * max(titulo_score, _similaridade(intencao.texto, combinado)) + 0.36 * cobertura + 0.18 * ordem

    score_semantico = max(
        0.0,
        min(1.0, identidade + oficial + versao + apresentacao - penalidade_semantica),
    )
    # Qualidade só usa a folga restante até 1.0. Assim um sinal auxiliar não
    # transforma dois candidatos semanticamente diferentes em empate por
    # saturação e nunca apaga a preferência explícita por audio/video/lyrics.
    score = max(
        0.0,
        min(1.0, score_semantico + qualidade * (1.0 - score_semantico) - penalidade_qualidade),
    )
    sinais = SinaisCandidato(
        titulo=round(titulo_score, 4),
        artista=round(artista_score, 4),
        cobertura=round(cobertura, 4),
        ordem=round(ordem, 4),
        oficialidade=round(oficial, 4),
        versao=round(versao, 4),
        apresentacao=round(apresentacao, 4),
        qualidade=round(qualidade, 4),
        penalidade=round(penalidade, 4),
    )
    return ResultadoRanking(
        indice_original=indice,
        score=round(score, 4),
        confianca=0.0,
        sinais=sinais,
    )


def ranquear_faixas(query: str, tracks: Sequence[MusicTrack]) -> tuple[list[MusicTrack], list[ResultadoRanking]]:
    if len(tracks) <= 1:
        resultados = [pontuar_faixa(query, track, indice=i) for i, track in enumerate(tracks)]
        if resultados:
            unico = resultados[0]
            resultados[0] = ResultadoRanking(
                indice_original=unico.indice_original,
                score=unico.score,
                confianca=_confidence(unico.score, unico.score),
                sinais=unico.sinais,
            )
        return list(tracks), resultados

    avaliados = [pontuar_faixa(query, track, indice=i) for i, track in enumerate(tracks)]
    ordenados = sorted(avaliados, key=lambda item: (-item.score, item.indice_original))
    ordenados = diversificar_resultados(tracks, ordenados, analisar_consulta(query))
    enriched: list[ResultadoRanking] = []
    for pos, item in enumerate(ordenados):
        proximo = ordenados[pos + 1].score if pos + 1 < len(ordenados) else 0.0
        margin = item.score - proximo if pos == 0 else 0.0
        enriched.append(
            ResultadoRanking(
                indice_original=item.indice_original,
                score=item.score,
                confianca=_confidence(item.score, margin),
                sinais=item.sinais,
            )
        )
    return [tracks[item.indice_original] for item in enriched], enriched
