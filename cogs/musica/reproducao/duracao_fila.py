"""Duração das faixas ainda virtuais, sem resolver streams ou ampliar o player.

O Worker mantém apenas a janela próxima da reprodução. Este cache armazena
somente os segundos de cada posição da fonte; as mutações da fila continuam
sendo interpretadas a partir dos cursores autoritativos do Worker.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from ..nucleo.modelos import PlaylistCursor

logger = logging.getLogger(__name__)

DurationKey = tuple[str, str, str]


def _key(info: dict[str, Any]) -> DurationKey:
    return (
        str(info.get("instance_id") or ""),
        str(info.get("provider") or ""),
        str(info.get("source_url") or ""),
    )


def _segments(state: Any) -> list[dict[str, Any]]:
    layout = getattr(state, "agent_queue_layout", None)
    if isinstance(layout, list) and layout:
        virtual = [entry["virtual"] for entry in layout if isinstance(entry, dict)
                   and entry.get("kind") == "virtual" and isinstance(entry.get("virtual"), dict)]
        if virtual:
            return virtual
    values = getattr(state, "agent_virtual_playlists", None)
    if isinstance(values, list) and values:
        return [value for value in values if isinstance(value, dict) and value.get("active")]
    info = getattr(state, "agent_virtual_playlist", None)
    return [info] if isinstance(info, dict) and info.get("active") else []


def _bounds(state: Any, info: dict[str, Any]) -> tuple[int, int | None]:
    start = max(0, int(info.get("next_offset") or 0))
    raw_end = info.get("block_end_offset")
    if raw_end in (None, ""):
        raw_end = info.get("total_tracks")
    if raw_end in (None, ""):
        raw_end = getattr(state, "agent_virtual_duration_ends", {}).get(_key(info))
    end = None if raw_end in (None, "") else max(start, int(raw_end))
    remaining = info.get("remaining")
    if remaining not in (None, ""):
        known_end = start + max(0, int(remaining))
        end = known_end if end is None else min(end, known_end)
    return start, end


def virtual_duration(state: Any) -> tuple[float, bool, bool]:
    """Soma exata dos segmentos atuais; retorna (segundos, pendente, impossível)."""
    cache = getattr(state, "agent_virtual_duration_cache", {})
    unknown = getattr(state, "agent_virtual_duration_unknown", set())
    failed = getattr(state, "agent_virtual_duration_failed_until", {})
    total = 0.0
    pending = False
    unavailable = False
    for info in _segments(state):
        key = _key(info)
        if not key[1] or not key[2]:
            unavailable = True
            continue
        start, end = _bounds(state, info)
        if end is None:
            pending = True
            if key in failed:
                unavailable = True
            continue
        saved = cache.get(key, {})
        for index in range(start, end):
            if index in saved:
                total += saved[index]
            elif (key, index) in unknown or key in failed:
                unavailable = True
            else:
                pending = True
    return total, pending, unavailable


def virtual_track_count(state: Any) -> int | None:
    """Contagem virtual exata após percorrer o provider, inclusive sem total inicial."""
    total = 0
    for info in _segments(state):
        start, end = _bounds(state, info)
        if end is None:
            return None
        total += max(0, end - start)
    return total


def _first_gap(state: Any) -> tuple[dict[str, Any], int, int] | None:
    cache = state.agent_virtual_duration_cache
    for info in _segments(state):
        key = _key(info)
        if not key[1] or not key[2] or key in state.agent_virtual_duration_failed_until:
            continue
        start, end = _bounds(state, info)
        if end is not None and end <= start:
            continue
        saved = cache.setdefault(key, {})
        index = start
        while end is None or index < end:
            if (key, index) in state.agent_virtual_duration_unknown:
                index += 1
                continue
            if index not in saved:
                break
            index += 1
        if end is None or index < end:
            # Nunca pular uma posição da fonte nem baixar mais de uma página.
            limit = min(50, end - index) if end is not None else 50
            return info, index, limit
    return None


def _cursor(info: dict[str, Any], offset: int) -> PlaylistCursor:
    raw_total = info.get("total_tracks")
    return PlaylistCursor(
        provider=str(info.get("provider") or ""),
        source_url=str(info.get("source_url") or ""),
        title=str(info.get("title") or ""),
        resource_type=str(info.get("resource_type") or "playlist"),
        resource_id=str(info.get("resource_id") or ""),
        next_offset=offset,
        total_tracks=None if raw_total in (None, "") else int(raw_total),
        instance_id=str(info.get("instance_id") or ""),
    )


def schedule_virtual_duration_scan(router: Any, guild_id: int, state: Any) -> None:
    """Completa as durações em segundo plano, sem bloquear o painel ou o áudio."""
    segments = _segments(state)
    if not segments:
        task = state.agent_virtual_duration_task
        if task is not None and not task.done():
            task.cancel()
        state.agent_virtual_duration_task = None
        state.agent_virtual_duration_cache.clear()
        state.agent_virtual_duration_unknown.clear()
        state.agent_virtual_duration_ends.clear()
        state.agent_virtual_duration_failed_until.clear()
        return

    active = {_key(info) for info in segments}
    for key in list(state.agent_virtual_duration_cache):
        if key not in active:
            state.agent_virtual_duration_cache.pop(key, None)
            state.agent_virtual_duration_ends.pop(key, None)
    state.agent_virtual_duration_unknown = {
        pair for pair in state.agent_virtual_duration_unknown if pair[0] in active
    }
    for key, retry_at in list(state.agent_virtual_duration_failed_until.items()):
        if key not in active or retry_at <= time.monotonic():
            state.agent_virtual_duration_failed_until.pop(key, None)

    task = state.agent_virtual_duration_task
    if task is not None and not task.done():
        return
    if _first_gap(state) is None:
        return

    # Um HTML público pode ter vários megabytes. Limite as leituras de duração
    # entre guilds sem competir pelo semáforo do refill que mantém o áudio.
    semaphore = getattr(router, "_virtual_playlist_duration_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)
        router._virtual_playlist_duration_semaphore = semaphore

    async def scan() -> None:
        try:
            while True:
                gap = _first_gap(state)
                if gap is None:
                    break
                info, offset, limit = gap
                key = _key(info)
                try:
                    # A consulta só lê metadata pública; não resolve YouTube
                    # nem adiciona objetos ao Worker. Refill de reprodução não
                    # depende deste cálculo para progredir.
                    async with semaphore:
                        batch = await asyncio.wait_for(
                            router.extractor.continue_playlist_window(
                                _cursor(info, offset), requester_id=0, requester_name="", limit=limit,
                            ),
                            timeout=20,
                        )
                    tracks = list(batch.tracks) if batch is not None else []
                    next_cursor = getattr(batch, "playlist_cursor", None)
                    next_offset = int(next_cursor.next_offset) if next_cursor else offset + len(tracks)
                    if next_offset != offset + len(tracks):
                        raise ValueError("provider omitiu posições da playlist")
                    _, expected_end = _bounds(state, info)
                    if not tracks:
                        if expected_end is not None and offset < expected_end:
                            raise ValueError("playlist acabou antes do total publicado")
                        state.agent_virtual_duration_ends[key] = offset
                        continue
                    if next_cursor is not None and next_cursor.exhausted:
                        if expected_end is not None and next_offset < expected_end:
                            raise ValueError("playlist acabou antes do total publicado")
                        state.agent_virtual_duration_ends[key] = next_offset
                    # O snapshot pode mudar durante a request. Metadados já
                    # lidos continuam válidos apenas para a mesma ocorrência.
                    if key not in {_key(value) for value in _segments(state)}:
                        continue
                    saved = state.agent_virtual_duration_cache.setdefault(key, {})
                    for index, track in enumerate(tracks, offset):
                        seconds = getattr(track, "duration", None)
                        if seconds is None or getattr(track, "is_live", False):
                            state.agent_virtual_duration_unknown.add((key, index))
                            continue
                        seconds = float(seconds)
                        if not math.isfinite(seconds) or seconds < 0:
                            state.agent_virtual_duration_unknown.add((key, index))
                            continue
                        saved[index] = seconds
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state.agent_virtual_duration_failed_until[key] = time.monotonic() + 45.0
                    logger.warning("[music/queue] duração de playlist indisponível | guild=%s offset=%s erro=%s",
                                   guild_id, offset, exc)
        finally:
            if state.agent_virtual_duration_task is asyncio.current_task():
                state.agent_virtual_duration_task = None
            if _segments(state):
                update = getattr(router, "update_panel", None)
                if callable(update):
                    try:
                        await update(guild_id, create=False)
                    except Exception:
                        logger.debug("[music/queue] falha ao atualizar duração no painel", exc_info=True)

    state.agent_virtual_duration_task = asyncio.create_task(scan())
