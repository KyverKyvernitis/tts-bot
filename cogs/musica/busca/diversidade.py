from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence

from ..nucleo.modelos import MusicTrack
from .atributos import FORMATOS_APRESENTACAO, detectar_apresentacao, detectar_variantes
from .modelos import ConsultaNormalizada, ResultadoRanking
from .normalizacao import limpar_apresentacao, texto_basico


@dataclass(frozen=True, slots=True)
class _Descritor:
    titulo: str
    uploader: str
    variantes: frozenset[str]
    apresentacao: frozenset[str]
    duracao: float | None


def _duracao_proxima(a: float | None, b: float | None) -> bool:
    if not a or not b:
        return True
    limite = max(7.0, min(float(a), float(b)) * 0.05)
    return abs(float(a) - float(b)) <= limite


def _descritor(track: MusicTrack) -> _Descritor:
    raw_titulo = track.display_title or track.title or ""
    titulo = limpar_apresentacao(raw_titulo)
    uploader = texto_basico(track.display_uploader or track.uploader or "")
    uploader_identidade = limpar_apresentacao(track.display_uploader or track.uploader or "")
    if uploader_identidade and titulo.startswith(uploader_identidade + " "):
        titulo = titulo[len(uploader_identidade) + 1 :].strip()
    return _Descritor(
        titulo=titulo,
        uploader=uploader,
        variantes=detectar_variantes(raw_titulo),
        apresentacao=frozenset(set(detectar_apresentacao(raw_titulo)) & set(FORMATOS_APRESENTACAO)),
        duracao=track.duration,
    )


def _redundancia(a: _Descritor, b: _Descritor) -> float:
    if not a.titulo or not b.titulo:
        return 0.0
    if a.variantes != b.variantes and (a.variantes or b.variantes):
        return 0.0
    if a.apresentacao and b.apresentacao and a.apresentacao != b.apresentacao:
        return 0.0

    semelhanca = SequenceMatcher(None, a.titulo, b.titulo, autojunk=False).ratio()
    if semelhanca < 0.92 or not _duracao_proxima(a.duracao, b.duracao):
        return 0.0

    penalidade = 0.075 + 0.045 * max(0.0, (semelhanca - 0.92) / 0.08)
    if a.uploader and b.uploader and SequenceMatcher(None, a.uploader, b.uploader, autojunk=False).ratio() >= 0.9:
        penalidade += 0.025
    return min(0.15, penalidade)


def diversificar_resultados(
    tracks: Sequence[MusicTrack],
    avaliados: Sequence[ResultadoRanking],
    intencao: ConsultaNormalizada,
) -> list[ResultadoRanking]:
    """Reranqueia somente as alternativas após o melhor candidato.

    O primeiro colocado por relevância nunca muda. A partir do segundo, um
    candidato quase idêntico a algo já escolhido recebe uma pequena penalidade
    temporária, evitando que a lista seja dominada por reuploads equivalentes.
    ``intencao`` fica na assinatura para permitir políticas contextuais futuras
    sem alterar o contrato público do ranking.
    """
    del intencao
    if len(avaliados) <= 2:
        return list(avaliados)

    descritores = {item.indice_original: _descritor(tracks[item.indice_original]) for item in avaliados}
    restantes = list(avaliados)
    selecionados = [restantes.pop(0)]
    while restantes:
        melhor_pos = 0
        melhor_valor = float("-inf")
        for pos, item in enumerate(restantes):
            descritor = descritores[item.indice_original]
            redundancia = 0.0
            for escolhido in selecionados:
                redundancia = max(
                    redundancia,
                    _redundancia(descritor, descritores[escolhido.indice_original]),
                )
            valor = item.score - redundancia
            if valor > melhor_valor:
                melhor_valor = valor
                melhor_pos = pos
        selecionados.append(restantes.pop(melhor_pos))
    return selecionados
