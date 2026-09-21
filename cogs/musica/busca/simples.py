from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..metadados.modelos import ApiTrackCandidate
from ..nucleo.modelos import MusicTrack


def _chave_faixa(track: MusicTrack) -> str:
    url = str(track.webpage_url or track.original_url or "").strip().lower()
    if url:
        return f"url:{url}"
    titulo = " ".join(str(track.title or track.display_title or "").lower().split())
    autor = " ".join(str(track.uploader or track.display_uploader or "").lower().split())
    return f"meta:{autor}|{titulo}"


def filtrar_faixas_minimas(tracks: Sequence[MusicTrack], *, limit: int = 3) -> list[MusicTrack]:
    """Mantem a ordem original e remove apenas candidatos inutilizaveis/duplicados.

    Este caminho e deliberadamente simples: nao calcula score, nao reordena e nao
    tenta completar a lista com uma segunda busca. A fonte decide a ordem.
    """
    limite = max(1, min(3, int(limit or 3)))
    resultado: list[MusicTrack] = []
    vistos: set[str] = set()
    for track in tracks:
        if not isinstance(track, MusicTrack):
            continue
        titulo = str(track.title or track.display_title or "").strip()
        if not titulo:
            continue
        chave = _chave_faixa(track)
        if chave in vistos:
            continue
        vistos.add(chave)
        resultado.append(track)
        if len(resultado) >= limite:
            break
    return resultado


def converter_youtube_api_minimo(
    candidates: Iterable[ApiTrackCandidate],
    *,
    requester_id: int,
    requester_name: str,
    query: str,
    limit: int = 3,
) -> list[MusicTrack]:
    """Converte search.list em MusicTrack preservando exatamente a ordem da API."""
    limite = max(1, min(3, int(limit or 3)))
    tracks: list[MusicTrack] = []
    vistos: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        title = str(candidate.title or "").strip()
        url = str(candidate.webpage_url or "").strip()
        if not title or not url:
            continue
        chave = url.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        artist = str(candidate.artist or "").strip()
        thumbnail = str(candidate.thumbnail or "").strip()
        track = MusicTrack(
            title=title,
            webpage_url=url,
            requester_id=int(requester_id or 0),
            requester_name=requester_name or "",
            duration=candidate.duration,
            uploader=artist,
            thumbnail=thumbnail,
            source="YouTube",
            original_url=url or query,
            extractor="worker-ytdlp",
        )
        track.display_title = title
        track.display_uploader = artist
        track.display_thumbnail = thumbnail
        track.display_source = "YouTube"
        tracks.append(track)
        if len(tracks) >= limite:
            break
    return tracks
