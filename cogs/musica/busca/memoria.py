from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import time
from typing import Sequence

from ..nucleo.modelos import MusicTrack
from .chaves import chave_semantica_busca
from .modelos import ResultadoRanking
from .telemetria import registrar_selecao_telemetria

_MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class EscolhaBusca:
    """Escolha global já confirmada por um usuário.

    A identidade é somente a consulta semântica. Guild e usuário não participam
    da chave: depois que alguém escolheu um resultado para uma consulta, qualquer
    pessoa pode reutilizar a mesma escolha sem repetir busca/ranking.
    """

    chave: str
    consulta: str
    track: MusicTrack
    registrado_em: float


# Alias mantido para imports antigos. A preferência antiga deixou de participar
# do ranking; agora a memória representa apenas uma escolha direta global.
PreferenciaBusca = EscolhaBusca

_memoria: OrderedDict[str, EscolhaBusca] = OrderedDict()


def limpar_memoria_busca() -> None:
    _memoria.clear()


def _copiar_track_limpo(
    track: MusicTrack,
    *,
    requester_id: int = 0,
    requester_name: str = "",
) -> MusicTrack:
    """Copia apenas metadata estável da faixa escolhida.

    Stream URLs e estado de reprodução não são compartilhados globalmente porque
    podem expirar ou pertencer a outra sessão/guild. O Music Agent resolve o
    stream novamente somente quando a faixa realmente for tocar.
    """
    clone = MusicTrack(
        title=track.title,
        webpage_url=track.webpage_url,
        requester_id=int(requester_id or 0),
        requester_name=str(requester_name or ""),
        stream_url="",
        duration=track.duration,
        uploader=track.uploader,
        thumbnail=track.thumbnail,
        source=track.source,
        original_url=track.original_url,
        extractor=track.extractor,
        is_live=track.is_live,
    )
    clone.fallback_reason = track.fallback_reason
    clone.display_title = track.display_title
    clone.display_uploader = track.display_uploader
    clone.display_thumbnail = track.display_thumbnail
    clone.display_source = track.display_source
    return clone


def registrar_selecao_busca(
    query: str,
    track: MusicTrack,
    *,
    guild_id: int = 0,
    requester_id: int = 0,
    now: float | None = None,
    posicao: int = 0,
    total: int = 0,
) -> bool:
    """Guarda globalmente a faixa escolhida para a consulta semântica.

    ``guild_id`` e ``requester_id`` permanecem na assinatura por compatibilidade,
    mas não entram na chave. A escolha é compartilhada entre guilds e usuários.
    """
    registrar_selecao_telemetria(
        posicao=posicao,
        total=total,
        fonte=(track.display_source or track.source),
    )
    clean_query = str(query or "").strip()
    if not clean_query:
        return False

    chave = chave_semantica_busca(clean_query)
    if not chave:
        return False
    stamp = float(time.monotonic() if now is None else now)
    escolha = EscolhaBusca(
        chave=chave,
        consulta=clean_query,
        track=_copiar_track_limpo(track),
        registrado_em=stamp,
    )
    _memoria[chave] = escolha
    _memoria.move_to_end(chave)
    while len(_memoria) > _MAX_ENTRIES:
        _memoria.popitem(last=False)
    return True


def obter_escolha_busca(
    query: str,
    *,
    requester_id: int = 0,
    requester_name: str = "",
) -> MusicTrack | None:
    """Retorna a escolha global sem executar busca, ranking ou desempate."""
    clean_query = str(query or "").strip()
    if not clean_query:
        return None
    chave = chave_semantica_busca(clean_query)
    escolha = _memoria.get(chave)
    if escolha is None:
        return None
    _memoria.move_to_end(chave)
    return _copiar_track_limpo(
        escolha.track,
        requester_id=requester_id,
        requester_name=requester_name,
    )


def esquecer_escolha_busca(query: str) -> bool:
    clean_query = str(query or "").strip()
    if not clean_query:
        return False
    return _memoria.pop(chave_semantica_busca(clean_query), None) is not None


def obter_preferencia_busca(
    query: str,
    *,
    guild_id: int = 0,
    requester_id: int = 0,
    now: float | None = None,
) -> PreferenciaBusca | None:
    """Compatibilidade para código externo antigo; não é usada pelo ranking."""
    clean_query = str(query or "").strip()
    if not clean_query:
        return None
    chave = chave_semantica_busca(clean_query)
    escolha = _memoria.get(chave)
    if escolha is not None:
        _memoria.move_to_end(chave)
    return escolha


def estabilizar_com_preferencia(
    tracks: Sequence[MusicTrack],
    avaliados: Sequence[ResultadoRanking],
    preferencia: PreferenciaBusca | None,
    *,
    max_score_delta: float = 0.0,
) -> list[ResultadoRanking]:
    """Compatibilidade: memória de escolha não interfere mais no ranking."""
    return list(avaliados)
