from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from ..metadados.modelos import ApiTrackCandidate
from ..nucleo.modelos import MusicTrack
from .atributos import (
    FORMATOS_APRESENTACAO,
    VARIANTES_INCOMPATIVEIS_ISRC,
    detectar_apresentacao,
    detectar_variantes,
)
from .normalizacao import limpar_apresentacao, tokens_texto
from .ranking import ranquear_faixas

_RUIDO_CANAL = frozenset({
    "official",
    "oficial",
    "topic",
    "vevo",
    "records",
    "recordings",
    "record",
    "music",
})

_PRIORIDADE_FONTE = {
    "worker-youtube": 100,
    "youtube": 90,
    "spotify": 80,
    "deezer": 70,
    "soundcloud": 60,
}


@dataclass(slots=True)
class _EntradaFusao:
    track: MusicTrack
    provider: str
    isrc: str = ""
    score_origem: float = 0.0
    variantes: frozenset[str] = field(default_factory=frozenset)
    apresentacao: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class _GrupoFusao:
    entradas: list[_EntradaFusao] = field(default_factory=list)
    isrcs: set[str] = field(default_factory=set)
    identidades: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ResumoFusao:
    entradas: int
    grupos: int
    duplicatas: int
    fontes: tuple[str, ...]


def _canal_canonico(value: str) -> str:
    tokens = [token for token in tokens_texto(value, remover_ruido=True) if token not in _RUIDO_CANAL]
    return " ".join(tokens)


def _titulo_canonico(value: str, artist: str = "") -> str:
    titulo = limpar_apresentacao(value)
    artista = _canal_canonico(artist)
    if artista and titulo.startswith(f"{artista} "):
        titulo = titulo[len(artista) + 1 :].strip()
    return titulo


def _identidade_track(track: MusicTrack) -> tuple[str, str]:
    artista_raw = track.display_uploader or track.uploader
    titulo_raw = track.display_title or track.title
    artista = _canal_canonico(artista_raw)
    titulo = _titulo_canonico(titulo_raw, artista_raw)
    return artista, titulo


def _duracao_compativel(a: float | None, b: float | None) -> bool:
    if not a or not b:
        return True
    limite = max(6.0, min(15.0, min(float(a), float(b)) * 0.04))
    return abs(float(a) - float(b)) <= limite


def _identidade_compativel(a: tuple[str, str], b: tuple[str, str], *, duracao_ok: bool) -> bool:
    artista_a, titulo_a = a
    artista_b, titulo_b = b
    if not titulo_a or not titulo_b:
        return False
    if a == b:
        return True
    if not duracao_ok:
        return False
    # Sem artista, só agrupamos títulos literalmente iguais para não apagar
    # covers/versões de pessoas diferentes.
    if not artista_a or not artista_b:
        return titulo_a == titulo_b
    artista_sim = SequenceMatcher(None, artista_a, artista_b, autojunk=False).ratio()
    titulo_sim = SequenceMatcher(None, titulo_a, titulo_b, autojunk=False).ratio()
    return artista_sim >= 0.92 and titulo_sim >= 0.94


def _provider_track(track: MusicTrack) -> str:
    source = str(track.source or track.display_source or "").lower()
    url = str(track.webpage_url or "").lower()
    if "youtube" in source or "youtu.be" in url or "youtube.com" in url:
        return "worker-youtube"
    return "worker"


def _track_de_api(
    candidate: ApiTrackCandidate,
    *,
    requester_id: int,
    requester_name: str,
    query: str,
) -> MusicTrack:
    provider = str(candidate.provider or candidate.source or "api").strip().lower()
    is_youtube = provider == "youtube" or "youtube.com" in str(candidate.webpage_url or "").lower() or "youtu.be" in str(candidate.webpage_url or "").lower()
    source = "YouTube" if is_youtube else (provider.title() if provider else "Metadata")
    track = MusicTrack(
        title=str(candidate.title or "Música").strip() or "Música",
        webpage_url=str(candidate.webpage_url or "").strip(),
        requester_id=int(requester_id or 0),
        requester_name=requester_name or "",
        duration=candidate.duration,
        uploader=str(candidate.artist or "").strip(),
        thumbnail=str(candidate.thumbnail or "").strip(),
        source=source,
        original_url=str(candidate.webpage_url or query or "").strip(),
        extractor="worker-ytdlp" if is_youtube else "metadata",
    )
    track.display_title = track.title
    track.display_uploader = track.uploader
    track.display_thumbnail = track.thumbnail
    track.display_source = source
    return track


def _texto_semantico_track(track: MusicTrack) -> str:
    return " ".join(part for part in (track.title, track.display_title) if part)


def _entrada_worker(track: MusicTrack) -> _EntradaFusao:
    texto = _texto_semantico_track(track)
    return _EntradaFusao(
        track=track,
        provider=_provider_track(track),
        variantes=detectar_variantes(texto),
        apresentacao=detectar_apresentacao(texto),
    )


def _entrada_api(
    candidate: ApiTrackCandidate,
    *,
    requester_id: int,
    requester_name: str,
    query: str,
) -> _EntradaFusao:
    provider = str(candidate.provider or candidate.source or "api").strip().lower() or "api"
    track = _track_de_api(
        candidate,
        requester_id=requester_id,
        requester_name=requester_name,
        query=query,
    )
    texto = _texto_semantico_track(track)
    return _EntradaFusao(
        track=track,
        provider=provider,
        isrc=str(candidate.isrc or "").strip().upper(),
        score_origem=float(candidate.score or 0.0),
        variantes=detectar_variantes(texto),
        apresentacao=detectar_apresentacao(texto),
    )


def _variantes_compativeis(a: _EntradaFusao, b: _EntradaFusao, *, mesmo_isrc: bool) -> bool:
    hard_a = set(a.variantes) & set(VARIANTES_INCOMPATIVEIS_ISRC)
    hard_b = set(b.variantes) & set(VARIANTES_INCOMPATIVEIS_ISRC)
    if hard_a != hard_b and (hard_a or hard_b):
        return False
    if mesmo_isrc:
        return True
    # Fora de um ISRC compartilhado, remaster e outros qualificadores leves
    # também precisam coincidir para evitar colapsar edições diferentes.
    return a.variantes == b.variantes or (not a.variantes and not b.variantes)


def _apresentacao_compativel(a: _EntradaFusao, b: _EntradaFusao) -> bool:
    # Metadata de catálogo não representa uma apresentação concreta. Ela pode
    # enriquecer qualquer resultado do worker sem forçar audio/video/lyrics a
    # virarem uma única opção.
    if not (a.provider.startswith("worker") and b.provider.startswith("worker")):
        return True
    formato_a = set(a.apresentacao) & set(FORMATOS_APRESENTACAO)
    formato_b = set(b.apresentacao) & set(FORMATOS_APRESENTACAO)
    if formato_a and formato_b and formato_a != formato_b:
        return False
    return True


def _grupo_compativel(grupo: _GrupoFusao, entrada: _EntradaFusao) -> bool:
    identidade = _identidade_track(entrada.track)
    mesmo_isrc = bool(entrada.isrc and entrada.isrc in grupo.isrcs)
    encontrou_identidade = False

    for existente in grupo.entradas:
        if not _variantes_compativeis(entrada, existente, mesmo_isrc=mesmo_isrc):
            return False
        if not _apresentacao_compativel(entrada, existente):
            return False
        if _identidade_compativel(
            identidade,
            _identidade_track(existente.track),
            duracao_ok=_duracao_compativel(entrada.track.duration, existente.track.duration),
        ):
            encontrou_identidade = True

    return mesmo_isrc or encontrou_identidade


def _adicionar_grupo(grupo: _GrupoFusao, entrada: _EntradaFusao) -> None:
    grupo.entradas.append(entrada)
    if entrada.isrc:
        grupo.isrcs.add(entrada.isrc)
    identidade = _identidade_track(entrada.track)
    if any(identidade):
        grupo.identidades.add(identidade)


def _prioridade(entrada: _EntradaFusao) -> tuple[int, float, int]:
    provider = entrada.provider
    prioridade = _PRIORIDADE_FONTE.get(provider, 40 if provider == "worker" else 30)
    tem_url = 1 if entrada.track.webpage_url else 0
    return prioridade, entrada.score_origem, tem_url


def _representante(grupo: _GrupoFusao) -> MusicTrack:
    entrada = max(grupo.entradas, key=_prioridade)
    track = entrada.track

    # Uma fonte oficial de metadata pode preencher campos ausentes sem trocar a
    # URL exata do YouTube retornada pelo worker.
    metadata = sorted(
        (item for item in grupo.entradas if item.provider in {"spotify", "deezer"}),
        key=_prioridade,
        reverse=True,
    )
    if metadata:
        meta = metadata[0].track
        if not track.display_title:
            track.display_title = meta.display_title or meta.title
        if not track.display_uploader:
            track.display_uploader = meta.display_uploader or meta.uploader
        if not track.thumbnail:
            track.thumbnail = meta.thumbnail
        if not track.display_thumbnail:
            track.display_thumbnail = meta.display_thumbnail or meta.thumbnail
        if not track.duration:
            track.duration = meta.duration
    return track


def fundir_resultados(
    query: str,
    worker_tracks: Sequence[MusicTrack],
    api_candidates: Iterable[ApiTrackCandidate],
    *,
    requester_id: int = 0,
    requester_name: str = "",
    limit: int = 5,
) -> tuple[list[MusicTrack], ResumoFusao]:
    """Funde resultados equivalentes sem resolver áudio antecipadamente.

    O worker fornece candidatos YouTube leves e as APIs fornecem metadata. A
    fusão preserva versões distintas (live/remix/cover etc.), mas colapsa a
    mesma gravação quando identidade, duração ou ISRC confirmam equivalência.
    """
    entradas: list[_EntradaFusao] = [_entrada_worker(track) for track in worker_tracks]
    entradas.extend(
        _entrada_api(
            candidate,
            requester_id=requester_id,
            requester_name=requester_name,
            query=query,
        )
        for candidate in api_candidates
        if candidate and str(candidate.title or "").strip()
    )

    grupos: list[_GrupoFusao] = []
    for entrada in entradas:
        for grupo in grupos:
            if _grupo_compativel(grupo, entrada):
                _adicionar_grupo(grupo, entrada)
                break
        else:
            grupo = _GrupoFusao()
            _adicionar_grupo(grupo, entrada)
            grupos.append(grupo)

    representantes = [_representante(grupo) for grupo in grupos]
    ranqueados, _ = ranquear_faixas(query, representantes)
    try:
        limite = max(1, min(10, int(limit or 5)))
    except Exception:
        limite = 5
    fontes = tuple(sorted({entrada.provider for entrada in entradas if entrada.provider}))
    resumo = ResumoFusao(
        entradas=len(entradas),
        grupos=len(grupos),
        duplicatas=max(0, len(entradas) - len(grupos)),
        fontes=fontes,
    )
    return ranqueados[:limite], resumo
