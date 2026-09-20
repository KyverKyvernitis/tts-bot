from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from difflib import SequenceMatcher
import time
from typing import Sequence

from ..nucleo.modelos import MusicTrack
from .atributos import detectar_apresentacao, detectar_variantes
from .intencao import analisar_consulta
from .modelos import ConsultaNormalizada, ResultadoRanking
from .normalizacao import limpar_apresentacao, texto_basico, tokens_texto
from .telemetria import registrar_selecao_telemetria

_TTL_SECONDS = 20.0 * 60.0
_MAX_ENTRIES = 256
_MAX_SCORE_DELTA = 0.035


@dataclass(frozen=True, slots=True)
class PreferenciaBusca:
    consulta: str
    artista: str
    titulo: str
    atributos: frozenset[str]
    apresentacao: frozenset[str]
    track_titulo: str
    uploader: str
    url: str
    source: str
    duracao: float | None
    variantes_track: frozenset[str]
    apresentacao_track: frozenset[str]
    registrado_em: float


_memoria: OrderedDict[tuple[int, int], PreferenciaBusca] = OrderedDict()


def limpar_memoria_busca() -> None:
    """Limpa somente o cache efêmero de preferência de busca."""
    _memoria.clear()


def _chave(guild_id: int, requester_id: int) -> tuple[int, int] | None:
    guild = int(guild_id or 0)
    requester = int(requester_id or 0)
    if guild <= 0 or requester <= 0:
        return None
    return guild, requester


def _texto_track(track: MusicTrack) -> str:
    return " ".join(
        parte
        for parte in (
            track.display_uploader or track.uploader,
            track.display_title or track.title,
        )
        if parte
    ).strip()


def _titulo_track(track: MusicTrack) -> str:
    return limpar_apresentacao(track.display_title or track.title or "")


def _uploader_track(track: MusicTrack) -> str:
    return limpar_apresentacao(track.display_uploader or track.uploader or "")


def _url_track(track: MusicTrack) -> str:
    return str(track.webpage_url or track.original_url or "").strip()


def _source_track(track: MusicTrack) -> str:
    return texto_basico(" ".join(parte for parte in (track.source, track.display_source) if parte))


def _similaridade(a: str, b: str) -> float:
    a = limpar_apresentacao(a)
    b = limpar_apresentacao(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _jaccard(a: str, b: str) -> float:
    ta = set(tokens_texto(a, remover_ruido=True))
    tb = set(tokens_texto(b, remover_ruido=True))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def registrar_selecao_busca(
    query: str,
    track: MusicTrack,
    *,
    guild_id: int,
    requester_id: int,
    now: float | None = None,
    posicao: int = 0,
    total: int = 0,
) -> bool:
    """Guarda a última escolha textual por usuário/guild em memória local.

    Nada é persistido em disco ou banco. O cache é pequeno, possui TTL curto e
    só serve como desempate de buscas quase idênticas feitas logo depois.
    """
    registrar_selecao_telemetria(
        posicao=posicao,
        total=total,
        fonte=(track.display_source or track.source),
    )
    chave = _chave(guild_id, requester_id)
    clean_query = str(query or "").strip()
    if chave is None or not clean_query:
        return False

    intencao = analisar_consulta(clean_query)
    texto_track = _texto_track(track)
    stamp = float(time.monotonic() if now is None else now)
    preferencia = PreferenciaBusca(
        consulta=intencao.texto,
        artista=intencao.artista,
        titulo=intencao.titulo,
        atributos=intencao.atributos,
        apresentacao=intencao.apresentacao,
        track_titulo=_titulo_track(track),
        uploader=_uploader_track(track),
        url=_url_track(track),
        source=_source_track(track),
        duracao=float(track.duration) if track.duration is not None else None,
        variantes_track=detectar_variantes(texto_track),
        apresentacao_track=detectar_apresentacao(texto_track),
        registrado_em=stamp,
    )
    _memoria[chave] = preferencia
    _memoria.move_to_end(chave)
    while len(_memoria) > _MAX_ENTRIES:
        _memoria.popitem(last=False)
    return True


def _consulta_compativel(atual: ConsultaNormalizada, anterior: PreferenciaBusca) -> bool:
    # Mudança explícita de versão/apresentação sempre vence a memória recente.
    if atual.atributos and atual.atributos != anterior.atributos:
        return False
    if atual.apresentacao and atual.apresentacao != anterior.apresentacao:
        return False

    if atual.artista and atual.titulo and anterior.artista and anterior.titulo:
        return (
            _similaridade(atual.artista, anterior.artista) >= 0.82
            and _similaridade(atual.titulo, anterior.titulo) >= 0.82
        )

    return _similaridade(atual.texto, anterior.consulta) >= 0.80 and _jaccard(atual.texto, anterior.consulta) >= 0.55


def obter_preferencia_busca(
    query: str,
    *,
    guild_id: int,
    requester_id: int,
    now: float | None = None,
) -> PreferenciaBusca | None:
    chave = _chave(guild_id, requester_id)
    if chave is None:
        return None
    preferencia = _memoria.get(chave)
    if preferencia is None:
        return None

    stamp = float(time.monotonic() if now is None else now)
    if stamp - preferencia.registrado_em > _TTL_SECONDS:
        _memoria.pop(chave, None)
        return None

    intencao = analisar_consulta(query)
    if not _consulta_compativel(intencao, preferencia):
        return None
    _memoria.move_to_end(chave)
    return preferencia


def _duracao_score(atual: float | None, anterior: float | None) -> float:
    if not atual or not anterior:
        return 0.5
    diff = abs(float(atual) - float(anterior))
    limite = max(8.0, min(float(atual), float(anterior)) * 0.05)
    if diff <= limite:
        return 1.0
    if diff <= limite * 2.0:
        return 0.5
    return 0.0


def _afinidade(track: MusicTrack, preferencia: PreferenciaBusca) -> float:
    url = _url_track(track)
    if url and preferencia.url and url == preferencia.url:
        return 1.0

    titulo = _similaridade(_titulo_track(track), preferencia.track_titulo)
    uploader = _similaridade(_uploader_track(track), preferencia.uploader)
    duracao = _duracao_score(track.duration, preferencia.duracao)
    source = 1.0 if _source_track(track) and _source_track(track) == preferencia.source else 0.0

    texto = _texto_track(track)
    variantes = detectar_variantes(texto)
    apresentacao = detectar_apresentacao(texto)
    versao = 1.0 if variantes == preferencia.variantes_track else 0.0
    formato = 1.0 if apresentacao == preferencia.apresentacao_track else 0.0
    return 0.46 * titulo + 0.20 * uploader + 0.10 * duracao + 0.08 * source + 0.08 * versao + 0.08 * formato


def estabilizar_com_preferencia(
    tracks: Sequence[MusicTrack],
    avaliados: Sequence[ResultadoRanking],
    preferencia: PreferenciaBusca | None,
    *,
    max_score_delta: float = _MAX_SCORE_DELTA,
) -> list[ResultadoRanking]:
    """Usa preferência recente apenas como desempate entre quase iguais.

    Os scores semânticos não são alterados. Apenas candidatos dentro da janela
    do líder podem trocar de posição, então uma escolha antiga não consegue
    superar uma intenção nova claramente melhor.
    """
    itens = list(avaliados)
    if preferencia is None or len(itens) <= 1:
        return itens

    lider_score = itens[0].score
    limite = max(0.0, float(max_score_delta))
    melhor_pos = 0
    melhor_afinidade = _afinidade(tracks[itens[0].indice_original], preferencia)
    for pos, item in enumerate(itens[1:], start=1):
        if lider_score - item.score > limite:
            break
        afinidade = _afinidade(tracks[item.indice_original], preferencia)
        if afinidade >= melhor_afinidade + 0.05 and afinidade >= 0.80:
            melhor_pos = pos
            melhor_afinidade = afinidade

    if melhor_pos > 0:
        escolhido = itens.pop(melhor_pos)
        itens.insert(0, escolhido)
    return itens
