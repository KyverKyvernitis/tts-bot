from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from cogs.musica import configuracao as config

from ..agente_telefone.comandos import music_agent_command
from ..metadados.direct_play import consulta_metadata_direct_play
from ..nucleo.modelos import MusicTrack, PlaylistCursor
from ..nucleo.playlist_virtual import PlaylistWindowPolicy

logger = logging.getLogger(__name__)


def _cursor_retry_key(cursor: PlaylistCursor) -> str:
    return f"{cursor.provider}|{cursor.source_url}|{max(0, int(cursor.next_offset))}"


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
    return PlaylistCursor(
        provider=provider,
        source_url=source_url,
        title=str(value.get("title") or "").strip(),
        resource_type=str(value.get("resource_type") or "playlist").strip() or "playlist",
        resource_id=str(value.get("resource_id") or "").strip(),
        next_offset=next_offset,
        total_tracks=total_tracks,
        exhausted=bool(value.get("exhausted")),
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

            next_cursor = batch.playlist_cursor or cursor.advanced(len(batch.tracks), exhausted=not batch.tracks)
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


def cancel_playlist_refill(state: Any) -> None:
    task = getattr(state, "virtual_playlist_refill_task", None)
    if task is not None and not task.done():
        with contextlib.suppress(Exception):
            task.cancel()
    state.virtual_playlist_refill_task = None
    _reset_refill_backoff(state)
