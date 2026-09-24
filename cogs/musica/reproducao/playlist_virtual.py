from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from cogs.musica import configuracao as config

from ..agente_telefone.comandos import music_agent_command
from ..busca import registrar_lote_link_busca
from ..metadados.direct_play import consulta_metadata_direct_play
from ..nucleo.modelos import MusicTrack, PlaylistCursor
from ..nucleo.playlist_virtual import PlaylistWindowPolicy

logger = logging.getLogger(__name__)


def _cursor_retry_key(cursor: PlaylistCursor) -> str:
    return "|".join((
        str(cursor.instance_id or ""),
        str(cursor.provider or ""),
        str(cursor.source_url or ""),
        str(cursor.block_end_offset if cursor.block_end_offset is not None else ""),
        str(max(0, int(cursor.shuffle_seed or 0))),
        str(max(0, int(cursor.next_offset))),
    ))


def _reset_refill_backoff(state: Any, cursor: PlaylistCursor | None = None) -> None:
    state.virtual_playlist_refill_cursor_key = _cursor_retry_key(cursor) if cursor is not None else ""
    state.virtual_playlist_refill_failures = 0
    state.virtual_playlist_refill_retry_not_before = 0.0


def _prepare_refill_backoff(state: Any, cursor: PlaylistCursor) -> str:
    key = _cursor_retry_key(cursor)
    if str(getattr(state, "virtual_playlist_refill_cursor_key", "") or "") != key:
        _reset_refill_backoff(state, cursor)
    return key


def _record_refill_failure(state: Any, cursor: PlaylistCursor) -> float:
    key = _prepare_refill_backoff(state, cursor)
    failures = max(0, int(getattr(state, "virtual_playlist_refill_failures", 0) or 0)) + 1
    base = max(0.1, float(getattr(config, "MUSIC_PLAYLIST_REFILL_RETRY_BASE_SECONDS", 0.6) or 0.6))
    maximum = max(base, float(getattr(config, "MUSIC_PLAYLIST_REFILL_FAILURE_COOLDOWN_MAX_SECONDS", 20.0) or 20.0))
    delay = min(maximum, base * (2 ** min(8, failures - 1)))
    state.virtual_playlist_refill_cursor_key = key
    state.virtual_playlist_refill_failures = failures
    state.virtual_playlist_refill_retry_not_before = time.monotonic() + delay
    return delay


def _refill_semaphore(router: Any) -> asyncio.Semaphore:
    limit = max(1, min(4, int(getattr(config, "MUSIC_PLAYLIST_REFILL_MAX_CONCURRENCY", 2) or 2)))
    semaphore = getattr(router, "_virtual_playlist_refill_semaphore", None)
    current_limit = int(getattr(router, "_virtual_playlist_refill_semaphore_limit", 0) or 0)
    if semaphore is None or current_limit != limit:
        semaphore = asyncio.Semaphore(limit)
        router._virtual_playlist_refill_semaphore = semaphore
        router._virtual_playlist_refill_semaphore_limit = limit
    return semaphore


def consulta_agent_para_faixa(track: MusicTrack, fallback: str = "") -> str:
    extractor = str(getattr(track, "extractor", "") or "").lower()
    source = str(getattr(track, "source", "") or getattr(track, "display_source", "") or "").lower()
    is_metadata = extractor == "metadata" or any(token in source for token in ("spotify", "deezer", "apple", "metadata"))
    if is_metadata:
        return consulta_metadata_direct_play(
            titulo=str(getattr(track, "display_title", "") or getattr(track, "title", "") or ""),
            artista=str(getattr(track, "display_uploader", "") or getattr(track, "uploader", "") or ""),
            fallback=fallback,
        )
    return str(
        getattr(track, "webpage_url", "")
        or getattr(track, "original_url", "")
        or getattr(track, "stream_url", "")
        or getattr(track, "title", "")
        or fallback
        or ""
    ).strip()


def payload_faixa_agent(
    track: MusicTrack,
    *,
    requester_id: int = 0,
    requester_name: str = "",
    fallback_query: str = "",
) -> dict[str, Any]:
    return {
        "title": track.title,
        "webpage_url": track.webpage_url,
        "original_url": track.original_url,
        "stream_url": track.stream_url,
        "duration": track.duration,
        "uploader": track.uploader,
        "thumbnail": track.thumbnail,
        "source": track.source,
        "extractor": track.extractor,
        "display_title": getattr(track, "display_title", ""),
        "display_uploader": getattr(track, "display_uploader", ""),
        "display_source": getattr(track, "display_source", ""),
        "query": consulta_agent_para_faixa(track, fallback_query),
        "requester_id": requester_id or track.requester_id,
        "requester_name": requester_name or track.requester_name,
        "queue_item_id": str(getattr(track, "queue_item_id", "") or ""),
    }


def payload_cursor_playlist(
    cursor: PlaylistCursor,
    *,
    requester_id: int = 0,
    requester_name: str = "",
) -> dict[str, Any]:
    """Placeholder leve inserido na fila remota para preservar ordenação.

    O marker fica exatamente onde o restante lógico da playlist pertence. Novas
    músicas adicionadas depois entram após esse marker; cada refill o substitui
    por uma nova janela + marker atualizado, sem materializar a coleção inteira.
    """

    if not str(cursor.instance_id or "").strip():
        cursor.instance_id = uuid.uuid4().hex
    return {
        "title": cursor.title or "Playlist",
        "webpage_url": cursor.source_url,
        "original_url": cursor.source_url,
        "source": "playlist-virtual",
        "extractor": "playlist-cursor",
        "requester_id": int(requester_id or 0),
        "requester_name": requester_name or "",
        "virtual_playlist_cursor": cursor.public(),
    }


def cursor_playlist_do_payload(value: Any) -> PlaylistCursor | None:
    if not isinstance(value, dict):
        return None
    source_url = str(value.get("source_url") or "").strip()
    provider = str(value.get("provider") or "").strip()
    if not source_url or not provider:
        return None
    total = value.get("total_tracks")
    try:
        total_tracks = None if total in (None, "") else max(0, int(total))
    except Exception:
        total_tracks = None
    try:
        next_offset = max(0, int(value.get("next_offset") or 0))
    except Exception:
        next_offset = 0
    block_raw = value.get("block_end_offset")
    try:
        block_end = None if block_raw in (None, "") else max(0, int(block_raw))
    except Exception:
        block_end = None
    return PlaylistCursor(
        provider=provider,
        source_url=source_url,
        title=str(value.get("title") or "").strip(),
        resource_type=str(value.get("resource_type") or "playlist").strip() or "playlist",
        resource_id=str(value.get("resource_id") or "").strip(),
        next_offset=next_offset,
        total_tracks=total_tracks,
        exhausted=bool(value.get("exhausted")),
        instance_id=str(value.get("instance_id") or "").strip(),
        block_end_offset=block_end,
        shuffle_seed=max(0, int(value.get("shuffle_seed") or 0)),
    )


def _materialized_before_marker(remote: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    virtual = remote.get("virtual_playlist") if isinstance(remote.get("virtual_playlist"), dict) else None
    if not virtual:
        return None, 0
    try:
        count = max(0, int(virtual.get("materialized_before") or 0))
    except Exception:
        count = 0
    return virtual, count


def schedule_playlist_refill_from_result(router: Any, guild_id: int, result: Any) -> bool:
    """Agenda o primeiro refill a partir da resposta já recebida do Worker.

    A função não aguarda rede nem cria polling; apenas reutiliza o snapshot
    autoritativo retornado pelo comando de play. O scheduler normal coalesce
    qualquer corrida com o monitor periódico.
    """
    remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
    if not remote or not isinstance(remote.get("virtual_playlist"), dict):
        return False
    return schedule_playlist_refill_if_needed(router, int(guild_id), remote)


def schedule_playlist_refill_if_needed(router: Any, guild_id: int, remote: dict[str, Any]) -> bool:
    """Agenda refill com backpressure, retry e cancelamento por geração.

    O monitor do Music Agent continua sendo a única cadência contínua. Em caso
    de falha, o próprio refill faz poucas tentativas com backoff e depois deixa
    um cooldown monotônico para que snapshots seguintes não martelem o
    Spotify. Um semáforo compartilhado pelo router limita rajadas entre guilds.
    """

    virtual, materialized = _materialized_before_marker(remote)
    if not virtual:
        return False
    cursor = cursor_playlist_do_payload(virtual.get("cursor"))
    if cursor is None or cursor.exhausted:
        return False

    policy = PlaylistWindowPolicy.from_config()
    waiting = bool(virtual.get("waiting")) or str(remote.get("last_event") or "").lower() == "playlist_refill_needed"
    if not waiting and materialized > policy.low_watermark:
        return False

    state = router.get_state(int(guild_id))
    _prepare_refill_backoff(state, cursor)
    retry_not_before = float(getattr(state, "virtual_playlist_refill_retry_not_before", 0.0) or 0.0)
    if retry_not_before > time.monotonic():
        return False

    existing = getattr(state, "virtual_playlist_refill_task", None)
    if existing is not None and not existing.done():
        return False

    generation = int(getattr(state, "music_operation_generation", 0) or 0)
    requester_id = int(virtual.get("requester_id") or 0)
    requester_name = str(virtual.get("requester_name") or "")
    voice_channel_id = int(remote.get("voice_channel_id") or getattr(state, "last_voice_channel_id", 0) or 0)
    text_channel_id = int(remote.get("text_channel_id") or getattr(state, "last_text_channel_id", 0) or 0)
    refill_limit = policy.refill_limit(materialized)
    if waiting:
        refill_limit = max(refill_limit, policy.high_watermark)
    if cursor.block_end_offset is not None:
        block_remaining = max(0, int(cursor.block_end_offset) - int(cursor.next_offset))
        if cursor.shuffle_seed:
            # O bloco precisa chegar completo para a mesma permutação ser usada
            # no playback e na UI. Blocos do shuffle são sempre <= 100.
            refill_limit = min(100, block_remaining)
        else:
            refill_limit = min(refill_limit, block_remaining)
    refill_limit = max(1, refill_limit)

    async def runner() -> None:
        try:
            last_exc: Exception | None = None
            batch = None
            max_attempts = max(1, min(4, int(getattr(config, "MUSIC_PLAYLIST_REFILL_MAX_ATTEMPTS", 3) or 3)))
            retry_base = max(0.1, float(getattr(config, "MUSIC_PLAYLIST_REFILL_RETRY_BASE_SECONDS", 0.6) or 0.6))
            retry_max = max(retry_base, float(getattr(config, "MUSIC_PLAYLIST_REFILL_RETRY_MAX_SECONDS", 4.0) or 4.0))
            semaphore = _refill_semaphore(router)

            for attempt in range(max_attempts):
                if router.current_music_operation_generation(guild_id) != generation:
                    return
                try:
                    async with semaphore:
                        if router.current_music_operation_generation(guild_id) != generation:
                            return
                        batch = await router.extractor.continue_playlist_window(
                            cursor,
                            requester_id=requester_id,
                            requester_name=requester_name,
                            limit=refill_limit,
                        )
                    last_exc = None
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(min(retry_max, retry_base * (2 ** attempt)))

            if last_exc is not None:
                current = router.get_state(int(guild_id))
                cooldown = 0.0
                if router.current_music_operation_generation(guild_id) == generation:
                    cooldown = _record_refill_failure(current, cursor)
                logger.warning(
                    "[music/playlist] refill público falhou | guild=%s provider=%s offset=%s attempts=%s cooldown=%.2fs erro=%s",
                    guild_id,
                    cursor.provider,
                    cursor.next_offset,
                    max_attempts,
                    cooldown,
                    last_exc,
                )
                return
            if batch is None or router.current_music_operation_generation(guild_id) != generation:
                return

            # Cada nova janela da playlist passa a servir também como memória
            # de direct play por nome. Isso reutiliza os aliases existentes de
            # link (incluindo primeira palavra) e evita pesquisa externa futura.
            if cursor.provider.startswith("spotify") and batch.tracks:
                with contextlib.suppress(Exception):
                    registrar_lote_link_busca(batch.tracks)

            provider_next = batch.playlist_cursor or cursor.advanced(len(batch.tracks), exhausted=not batch.tracks)
            next_offset = max(int(cursor.next_offset), int(getattr(provider_next, "next_offset", cursor.next_offset + len(batch.tracks)) or 0))
            block_end = cursor.block_end_offset
            block_exhausted = bool(block_end is not None and next_offset >= int(block_end))
            next_cursor = PlaylistCursor(
                provider=provider_next.provider or cursor.provider,
                source_url=provider_next.source_url or cursor.source_url,
                title=provider_next.title or cursor.title,
                resource_type=provider_next.resource_type or cursor.resource_type,
                resource_id=provider_next.resource_id or cursor.resource_id,
                next_offset=next_offset,
                total_tracks=provider_next.total_tracks if provider_next.total_tracks is not None else cursor.total_tracks,
                exhausted=bool(block_exhausted or provider_next.exhausted or not batch.tracks),
                instance_id=cursor.instance_id,
                block_end_offset=block_end,
                shuffle_seed=cursor.shuffle_seed,
            )
            tracks_payload = [
                payload_faixa_agent(
                    track,
                    requester_id=requester_id,
                    requester_name=requester_name,
                )
                for track in batch.tracks
            ]
            command_attempts = max(1, min(3, int(getattr(config, "MUSIC_PLAYLIST_REFILL_COMMAND_MAX_ATTEMPTS", 2) or 2)))
            result: dict[str, Any] | None = None
            command_exc: Exception | None = None
            for command_attempt in range(command_attempts):
                if router.current_music_operation_generation(guild_id) != generation:
                    return
                try:
                    result = await music_agent_command(
                        "playlist_refill",
                        guild_id=guild_id,
                        voice_channel_id=voice_channel_id,
                        text_channel_id=text_channel_id,
                        tracks=tracks_payload,
                        requester_id=requester_id,
                        requester_name=requester_name,
                        expected_cursor=cursor.public(),
                        next_cursor=next_cursor.public(),
                        timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0),
                    )
                    command_exc = None
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    command_exc = exc
                    if command_attempt + 1 < command_attempts:
                        # Repetir é seguro: ``expected_cursor`` é CAS no Phone
                        # Worker. Se a primeira resposta se perdeu após aplicar,
                        # a segunda tentativa será ignorada sem duplicar faixas.
                        await asyncio.sleep(min(retry_max, retry_base * (2 ** command_attempt)))
            if command_exc is not None:
                raise command_exc
            if result is None or router.current_music_operation_generation(guild_id) != generation:
                return

            current = router.get_state(int(guild_id))
            _reset_refill_backoff(current, None if next_cursor.exhausted else next_cursor)
            remote_state = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
            if remote_state:
                await router.sync_music_agent_state(
                    guild_id,
                    None,
                    remote_state,
                    voice_channel_id=voice_channel_id,
                    text_channel_id=text_channel_id,
                    queued=False,
                    create_panel=True,
                )
            logger.info(
                "[music/playlist] janela adicionada | guild=%s added=%s offset=%s next=%s exhausted=%s ignored=%s",
                guild_id,
                len(batch.tracks),
                cursor.next_offset,
                next_cursor.next_offset,
                next_cursor.exhausted,
                bool(isinstance(result, dict) and result.get("ignored")),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cooldown = 0.0
            if router.current_music_operation_generation(guild_id) == generation:
                current = router.get_state(int(guild_id))
                cooldown = _record_refill_failure(current, cursor)
            logger.warning(
                "[music/playlist] refill não pôde ser entregue | guild=%s offset=%s cooldown=%.2fs erro=%s",
                guild_id,
                cursor.next_offset,
                cooldown,
                exc,
            )
        finally:
            current = router.get_state(int(guild_id))
            if getattr(current, "virtual_playlist_refill_task", None) is asyncio.current_task():
                current.virtual_playlist_refill_task = None

    try:
        task = asyncio.create_task(runner())
    except RuntimeError:
        return False
    state.virtual_playlist_refill_task = task
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return True



def virtual_playlist_consumed_count(info: dict[str, Any]) -> int:
    """Quantidade de itens da coleção que já deixaram a fila lógica.

    ``next_offset`` aponta depois da janela já materializada e
    ``materialized_before`` conta quantos desses itens ainda estão antes do
    marker no Worker. A diferença é exatamente o deslocamento da primeira
    posição atualmente visível da playlist.
    """
    if not isinstance(info, dict):
        return 0
    try:
        return max(0, int(info.get("next_offset") or 0) - int(info.get("materialized_before") or 0))
    except Exception:
        return 0


def virtual_playlist_remaining_count(info: dict[str, Any]) -> int | None:
    if not isinstance(info, dict):
        return None
    block_end = info.get("block_end_offset")
    if block_end not in (None, ""):
        try:
            return max(0, int(block_end) - int(info.get("next_offset") or 0))
        except Exception:
            return None
    total = info.get("total_tracks")
    if total in (None, ""):
        return None
    try:
        return max(0, int(total) - virtual_playlist_consumed_count(info))
    except Exception:
        return None


def _virtual_browse_state_key(info: dict[str, Any]) -> str:
    if not isinstance(info, dict) or not info:
        return ""
    return "|".join(
        (
            str(info.get("provider") or "").strip(),
            str(info.get("source_url") or "").strip(),
            str(virtual_playlist_consumed_count(info)),
            str(info.get("total_tracks") if info.get("total_tracks") not in (None, "") else "?"),
        )
    )


async def carregar_pagina_fila_virtual(
    router: Any,
    guild_id: int,
    page: int,
    *,
    page_size: int,
) -> list[MusicTrack]:
    """Carrega uma página da fila lógica, inclusive várias playlists virtuais.

    ``agent_queue_layout`` descreve a ordem exata como uma sequência de faixas
    já materializadas e segmentos virtuais. Somente os pedaços da página pedida
    são consultados no provider; áudio/yt-dlp continuam completamente lazy.
    """
    state = router.get_state(int(guild_id))
    page = max(0, int(page))
    page_size = max(1, min(25, int(page_size)))
    expected_key = str(getattr(state, "agent_virtual_playlist_browse_key", "") or "")

    if expected_key:
        cached = state.agent_virtual_playlist_pages.get(page)
        if isinstance(cached, list):
            return list(cached)

    layout = getattr(state, "agent_queue_layout", None)
    if not isinstance(layout, list) or not layout:
        # Compatibilidade com snapshots antigos (Wave 8–14): o cursor singular
        # representa a coleção inteira restante, enquanto ``materialized_before``
        # informa quantas faixas da janela já aparecem antes do marker.
        info = getattr(state, "agent_virtual_playlist", None)
        if isinstance(info, dict) and bool(info.get("active")):
            cached = state.agent_virtual_playlist_pages.get(page)
            if isinstance(cached, list):
                return list(cached)
            total_remaining = virtual_playlist_remaining_count(info)
            logical_start = page * page_size
            if total_remaining is not None and logical_start >= total_remaining:
                state.agent_virtual_playlist_pages[page] = []
                return []
            source_offset = virtual_playlist_consumed_count(info) + logical_start
            cursor = PlaylistCursor(
                provider=str(info.get("provider") or "").strip(),
                source_url=str(info.get("source_url") or "").strip(),
                title=str(info.get("title") or "").strip(),
                resource_type=str(info.get("resource_type") or "playlist").strip() or "playlist",
                resource_id=str(info.get("resource_id") or "").strip(),
                next_offset=source_offset,
                total_tracks=(None if info.get("total_tracks") in (None, "") else int(info.get("total_tracks"))),
                exhausted=False,
                instance_id=str(info.get("instance_id") or "").strip(),
                block_end_offset=(None if info.get("block_end_offset") in (None, "") else int(info.get("block_end_offset"))),
                shuffle_seed=max(0, int(info.get("shuffle_seed") or 0)),
            )
            if not cursor.provider or not cursor.source_url:
                return []
            try:
                batch = await router.extractor.continue_playlist_window(
                    cursor,
                    requester_id=int(info.get("requester_id") or 0),
                    requester_name=str(info.get("requester_name") or ""),
                    limit=page_size,
                )
                tracks = list(batch.tracks[:page_size]) if batch is not None else []
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.agent_virtual_playlist_browse_error = f"{type(exc).__name__}: {exc}"[:220]
                return []
            for local_index, track in enumerate(tracks):
                track.virtual_playlist_instance_id = str(info.get("instance_id") or "")
                track.virtual_source_index = source_offset + local_index
                track.virtual_provider = cursor.provider
                track.virtual_source_url = cursor.source_url
            state.agent_virtual_playlist_pages[page] = list(tracks)
            state.agent_virtual_playlist_browse_error = ""
            return tracks
        layout = []
        with contextlib.suppress(Exception):
            for track in list(getattr(state, "forward_queue", []) or []):
                layout.append({"kind": "track", "track": track})

    logical_start = page * page_size
    logical_end = logical_start + page_size
    logical_pos = 0
    pieces: list[tuple[str, Any, int, int]] = []

    def segment_length(info: dict[str, Any]) -> int | None:
        remaining = info.get("remaining")
        if remaining not in (None, ""):
            with contextlib.suppress(Exception):
                return max(0, int(remaining))
        try:
            begin = max(0, int(info.get("next_offset") or 0))
        except Exception:
            begin = 0
        raw_end = info.get("block_end_offset")
        if raw_end in (None, ""):
            raw_end = info.get("total_tracks")
        if raw_end in (None, ""):
            return None
        try:
            return max(0, int(raw_end) - begin)
        except Exception:
            return None

    for entry in layout:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        if kind == "track":
            length = 1
            seg_end = logical_pos + length
            if logical_pos < logical_end and seg_end > logical_start:
                pieces.append(("track", entry.get("track"), 0, 1))
            logical_pos = seg_end
            if logical_pos >= logical_end:
                break
            continue
        if kind != "virtual" or not isinstance(entry.get("virtual"), dict):
            continue
        info = entry["virtual"]
        length = segment_length(info)
        if length is None:
            # Total desconhecido: esse segmento absorve a página atual. Não há
            # como posicionar segmentos posteriores até o provider publicar o total.
            length = max(page_size, logical_end - logical_pos)
        seg_end = logical_pos + length
        if logical_pos < logical_end and seg_end > logical_start:
            overlap_start = max(logical_start, logical_pos) - logical_pos
            overlap_end = min(logical_end, seg_end) - logical_pos
            pieces.append(("virtual", info, int(overlap_start), int(overlap_end - overlap_start)))
        logical_pos = seg_end
        if logical_pos >= logical_end:
            break

    if not pieces:
        if expected_key:
            state.agent_virtual_playlist_pages[page] = []
        return []

    result: list[MusicTrack] = []
    try:
        for kind, value, offset_in_segment, count in pieces:
            if len(result) >= page_size:
                break
            if kind == "track":
                if isinstance(value, MusicTrack):
                    result.append(value)
                continue
            info = value if isinstance(value, dict) else {}
            provider = str(info.get("provider") or "").strip()
            source_url = str(info.get("source_url") or "").strip()
            if not provider or not source_url or count <= 0:
                continue
            segment_start = max(0, int(info.get("next_offset") or 0))
            shuffle_seed = max(0, int(info.get("shuffle_seed") or 0))
            source_index = segment_start + max(0, int(offset_in_segment))
            fetch_start = segment_start if shuffle_seed else source_index
            fetch_limit = min(count, page_size - len(result))
            if shuffle_seed:
                segment_len = segment_length(info)
                if segment_len is not None:
                    fetch_limit = max(1, min(100, int(segment_len)))
            cursor = PlaylistCursor(
                provider=provider,
                source_url=source_url,
                title=str(info.get("title") or "").strip(),
                resource_type=str(info.get("resource_type") or "playlist").strip() or "playlist",
                resource_id=str(info.get("resource_id") or "").strip(),
                next_offset=fetch_start,
                total_tracks=(None if info.get("total_tracks") in (None, "") else int(info.get("total_tracks"))),
                exhausted=False,
                instance_id=str(info.get("instance_id") or "").strip(),
                block_end_offset=(None if info.get("block_end_offset") in (None, "") else int(info.get("block_end_offset"))),
                shuffle_seed=shuffle_seed,
            )
            batch = await router.extractor.continue_playlist_window(
                cursor,
                requester_id=int(info.get("requester_id") or 0),
                requester_name=str(info.get("requester_name") or ""),
                limit=fetch_limit,
            )
            fetched = list(batch.tracks[:fetch_limit]) if batch is not None else []
            indexed = [(fetch_start + local_index, track) for local_index, track in enumerate(fetched)]
            if shuffle_seed and len(indexed) > 1:
                import random as _random
                _random.Random(shuffle_seed).shuffle(indexed)
                begin = max(0, int(offset_in_segment))
                indexed = indexed[begin:begin + count]
            else:
                indexed = indexed[:count]
            for actual_source_index, track in indexed:
                # A identidade virtual viaja com a opção do select. Assim uma
                # página distante pode tocar/remover/mover sem materializar os
                # outros itens da playlist.
                track.virtual_playlist_instance_id = str(info.get("instance_id") or "")
                track.virtual_source_index = actual_source_index
                track.virtual_provider = provider
                track.virtual_source_url = source_url
                result.append(track)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state.agent_virtual_playlist_browse_error = f"{type(exc).__name__}: {exc}"[:220]
        logger.debug(
            "[music/playlist] página lógica da fila não pôde ser carregada | guild=%s page=%s",
            guild_id,
            page,
            exc_info=True,
        )
        return []

    current = router.get_state(int(guild_id))
    if expected_key and str(getattr(current, "agent_virtual_playlist_browse_key", "") or "") != expected_key:
        return []
    current.agent_virtual_playlist_pages[page] = list(result)
    current.agent_virtual_playlist_browse_error = ""
    logger.info(
        "[music/playlist] página lógica pronta para UI | guild=%s page=%s tracks=%s",
        guild_id,
        page + 1,
        len(result),
    )
    return result


def cancel_playlist_refill(state: Any) -> None:
    task = getattr(state, "virtual_playlist_refill_task", None)
    if task is not None and not task.done():
        with contextlib.suppress(Exception):
            task.cancel()
    state.virtual_playlist_refill_task = None
    _reset_refill_backoff(state)
