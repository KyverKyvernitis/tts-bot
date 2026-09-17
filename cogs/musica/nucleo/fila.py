from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

from .estado import MusicGuildState
from .modelos import MusicTrack


def chaves_da_faixa(track: MusicTrack, compactar: Callable[[str], str]) -> set[str]:
    """Gera chaves estáveis para deduplicação da fila."""
    keys: set[str] = set()
    url = (track.webpage_url or track.original_url or "").strip().lower()
    if url:
        keys.add("url:" + url)
    title_key = compactar(track.title)
    if title_key:
        duration_bucket = ""
        if track.duration is not None:
            duration_bucket = str(int(max(0.0, float(track.duration)) // 8))
        keys.add("title:" + title_key + ":" + duration_bucket)
    return keys


def chaves_em_uso(state: MusicGuildState, compactar: Callable[[str], str]) -> set[str]:
    keys: set[str] = set()
    if state.current is not None:
        keys.update(chaves_da_faixa(state.current, compactar))
    for item in list(getattr(state, "forward_queue", []) or []):
        keys.update(chaves_da_faixa(item, compactar))
    for item in list(getattr(state.queue, "_queue", [])):
        keys.update(chaves_da_faixa(item, compactar))
    return keys


def itens_pendentes(state: MusicGuildState) -> list[MusicTrack]:
    """Retorna a ordem lógica local sem assumir propriedade da reprodução."""
    items: list[MusicTrack] = []
    with contextlib.suppress(Exception):
        items.extend(list(getattr(state, "forward_queue", []) or []))
    with contextlib.suppress(Exception):
        items.extend(list(getattr(state.queue, "_queue", [])))
    return items


def tem_pendentes(state: MusicGuildState) -> bool:
    return bool(itens_pendentes(state))


async def obter_proxima_faixa(state: MusicGuildState, *, timeout: float) -> tuple[MusicTrack, bool]:
    """Obtém a próxima faixa local e informa se veio de ``asyncio.Queue``.

    Este helper existe apenas para o caminho legado. No modo Worker-only a fila
    autoritativa continua no Phone Worker e a VPS mantém somente o espelho.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    poll_interval = 0.35
    while True:
        if getattr(state, "forward_queue", None):
            try:
                return state.forward_queue.popleft(), False
            except IndexError:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            return await asyncio.wait_for(state.queue.get(), timeout=min(poll_interval, remaining)), True
        except asyncio.TimeoutError:
            continue


def snapshot(state: MusicGuildState) -> list[MusicTrack]:
    return itens_pendentes(state)


def snapshot_historico(state: MusicGuildState) -> list[MusicTrack]:
    return list(state.history)


def registrar_historico(state: MusicGuildState, track: MusicTrack) -> bool:
    """Registra a faixa sem duplicar a mesma música em sequência."""
    try:
        if state.history and state.history[-1].display_url == track.display_url and state.history[-1].title == track.title:
            return False
        state.history.append(track)
        return True
    except Exception:
        return False


def inserir_no_inicio(state: MusicGuildState, track: MusicTrack) -> bool:
    try:
        if state.queue.full():
            return False
        state.queue._queue.appendleft(track)
        return True
    except Exception:
        return False


async def substituir_fila_local(state: MusicGuildState, tracks: list[MusicTrack], *, limite: int) -> None:
    """Substitui apenas o espelho/fila local de compatibilidade."""
    while not state.queue.empty():
        with contextlib.suppress(Exception):
            state.queue.get_nowait()
            state.queue.task_done()
    state.forward_queue.clear()
    for track in tracks[: max(0, int(limite))]:
        await state.queue.put(track)
