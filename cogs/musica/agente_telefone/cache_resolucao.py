from __future__ import annotations

import time

from cogs.musica import configuracao as config

from ..nucleo.modelos import ExtractedBatch, MusicTrack

_CHAVE_CACHE = tuple[str, int, bool, bool] | tuple[str, int, bool, bool, str]
_CACHE_RESOLUCAO: dict[_CHAVE_CACHE, tuple[float, ExtractedBatch]] = {}
_MAX_ITENS_CACHE = 96


def copiar_faixa_para_requisicao(
    track: MusicTrack,
    *,
    requester_id: int,
    requester_name: str,
) -> MusicTrack:
    clone = MusicTrack(
        title=track.title,
        webpage_url=track.webpage_url,
        requester_id=int(requester_id or track.requester_id or 0),
        requester_name=requester_name or track.requester_name or "",
        stream_url=track.stream_url,
        duration=track.duration,
        uploader=track.uploader,
        thumbnail=track.thumbnail,
        source=track.source,
        original_url=track.original_url,
        extractor=track.extractor,
        is_live=track.is_live,
    )
    clone.resolved_at_monotonic = track.resolved_at_monotonic
    clone.resolved_audio_max_abr = track.resolved_audio_max_abr
    clone.resolved_audio_abr = track.resolved_audio_abr
    clone.resolved_audio_ext = track.resolved_audio_ext
    clone.resolved_audio_codec = track.resolved_audio_codec
    clone.resolved_audio_format_id = track.resolved_audio_format_id
    clone.fallback_reason = track.fallback_reason
    clone.display_title = track.display_title
    clone.display_uploader = track.display_uploader
    clone.display_thumbnail = track.display_thumbnail
    clone.display_source = track.display_source
    return clone


def copiar_lote_para_requisicao(
    batch: ExtractedBatch,
    *,
    requester_id: int,
    requester_name: str,
) -> ExtractedBatch:
    return ExtractedBatch(
        tracks=[
            copiar_faixa_para_requisicao(
                track,
                requester_id=requester_id,
                requester_name=requester_name,
            )
            for track in batch.tracks
        ],
        query=batch.query,
        is_playlist=batch.is_playlist,
        playlist_title=batch.playlist_title,
        truncated=batch.truncated,
    )


def ttl_cache_resolucao(*, somente_metadados: bool) -> float:
    if somente_metadados:
        return max(
            0.0,
            float(
                getattr(config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 420.0)
                or 0.0
            ),
        )
    return max(
        0.0,
        float(
            getattr(config, "MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS", 90.0)
            or 0.0
        ),
    )


def chave_cache_resolucao(
    query: str,
    limit: int,
    somente_metadados: bool,
    permitir_playlist: bool = False,
    worker_scope: str = "",
) -> _CHAVE_CACHE:
    base = (
        str(query or "").strip().lower(),
        int(limit or 1),
        bool(somente_metadados),
        bool(permitir_playlist),
    )
    if somente_metadados:
        return base
    return base + (str(worker_scope or "").strip().lower(),)


def obter_cache_resolucao(
    key: _CHAVE_CACHE,
    *,
    requester_id: int,
    requester_name: str,
    somente_metadados: bool,
) -> ExtractedBatch | None:
    ttl = ttl_cache_resolucao(somente_metadados=somente_metadados)
    if ttl <= 0:
        return None
    item = _CACHE_RESOLUCAO.get(key)
    if not item:
        return None
    created, batch = item
    if time.monotonic() - created > ttl:
        _CACHE_RESOLUCAO.pop(key, None)
        return None
    return copiar_lote_para_requisicao(
        batch,
        requester_id=requester_id,
        requester_name=requester_name,
    )


def armazenar_cache_resolucao(
    key: _CHAVE_CACHE,
    batch: ExtractedBatch,
    *,
    somente_metadados: bool,
) -> None:
    if ttl_cache_resolucao(somente_metadados=somente_metadados) <= 0 or not batch.tracks:
        return
    if len(_CACHE_RESOLUCAO) >= _MAX_ITENS_CACHE:
        oldest = min(_CACHE_RESOLUCAO.items(), key=lambda item: item[1][0])[0]
        _CACHE_RESOLUCAO.pop(oldest, None)
    _CACHE_RESOLUCAO[key] = (
        time.monotonic(),
        copiar_lote_para_requisicao(batch, requester_id=0, requester_name=""),
    )
