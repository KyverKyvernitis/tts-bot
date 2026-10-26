"""Atividade musical e propriedade de operações, exclusivamente em memória.

Conexão de voz, histórico e o mixer persistente de TTS não constituem uma
sessão musical. Estes helpers não consultam worker, Discord ou armazenamento.
"""
from __future__ import annotations

import sys
from typing import Any


def _running(task: Any) -> bool:
    if task is None or not callable(getattr(task, "done", None)) or task.done():
        return False
    cancelled = getattr(task, "cancelled", None)
    cancelling = getattr(task, "cancelling", None)
    return not (callable(cancelled) and cancelled()) and not (callable(cancelling) and cancelling())


def _positive(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and int(value or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _virtual_pending(info: Any) -> bool:
    if not isinstance(info, dict) or not info or info.get("active") is False:
        return False
    cursor = info.get("cursor") if isinstance(info.get("cursor"), dict) else info
    if cursor.get("exhausted") is True:
        return False
    remaining = info.get("remaining")
    if remaining is not None:
        return _positive(remaining)
    end = cursor.get("block_end_offset")
    if end is None:
        end = cursor.get("total_tracks")
    if end is not None:
        try:
            return int(end) > int(cursor.get("next_offset") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
    return bool(info.get("active") or (cursor.get("provider") and cursor.get("source_url")))


def has_music_activity(router: Any, guild_id: int, *, state: Any = None) -> bool:
    """True somente para trabalho musical atual ou aceito e ainda pendente."""
    st = router.get_state(int(guild_id)) if state is None else state
    generation = int(getattr(st, "music_operation_generation", 0) or 0)
    owners = getattr(st, "music_pending_requests", ()) or ()
    if any(isinstance(owner, tuple) and owner and owner[0] == generation for owner in owners):
        return True
    status = str(getattr(st, "current_status", "") or "").lower()
    current = getattr(st, "current", None)
    if current is not None and (getattr(st, "paused", False)
                                or status not in {"idle", "stopped", "error", "failed"}):
        return True
    if status in {"preparing", "resolving", "starting", "queued", "skipping"}:
        return True
    if getattr(st, "forward_queue", None):
        return True
    queue = getattr(st, "queue", None)
    if getattr(queue, "_queue", None) or isinstance(queue, (list, tuple)) and queue:
        return True
    if _running(getattr(st, "current_resolve_task", None)):
        return True
    # The remote fields describe the logical queue, including unmaterialized
    # playlists that are absent from the short visible preview.
    if str(getattr(st, "current_backend", "") or "").lower() == "agent":
        if _positive(getattr(st, "agent_remote_queue_size", 0)):
            return True
        for entry in getattr(st, "agent_queue_layout", ()) or ():
            if isinstance(entry, dict):
                if entry.get("kind") == "track" and entry.get("track") is not None:
                    return True
                if entry.get("kind") == "virtual" and _virtual_pending(entry.get("virtual", entry)):
                    return True
        if any(_virtual_pending(info) for info in getattr(st, "agent_virtual_playlists", ()) or ()):
            return True
        if _virtual_pending(getattr(st, "agent_virtual_playlist", None)):
            return True
    if str(getattr(st, "agent_deferred_command_status", "") or "").lower() == "pending":
        return True
    # This module is already loaded by real control entrypoints. Looking at its
    # in-memory registry also covers a deferred request before the mirror syncs.
    commands = sys.modules.get("cogs.musica.agente_telefone.comandos")
    deferred = getattr(commands, "estado_comando_diferido", None)
    if callable(deferred):
        record = deferred(int(guild_id))
        if isinstance(record, dict) and record.get("status") == "pending":
            return True
    return False


def begin_music_request(router: Any, guild_id: int) -> object:
    st = router.get_state(int(guild_id))
    owners = getattr(st, "music_pending_requests", None)
    if owners is None:
        owners = st.music_pending_requests = set()
    st.music_request_serial = int(getattr(st, "music_request_serial", 0) or 0) + 1
    owner = (int(getattr(st, "music_operation_generation", 0) or 0), object())
    owners.add(owner)
    return owner


def end_music_request(router: Any, guild_id: int, owner: object) -> None:
    st = router.get_state(int(guild_id))
    owners = getattr(st, "music_pending_requests", None)
    if owners is not None:
        owners.discard(owner)


def claim_music_stop(router: Any, guild_id: int) -> object | None:
    st = router.get_state(int(guild_id))
    if getattr(st, "music_stop_owner", None) is not None:
        return None
    owner = object()
    st.music_stop_owner = owner
    return owner


def owns_music_stop(router: Any, guild_id: int, owner: object) -> bool:
    return owner is not None and getattr(router.get_state(int(guild_id)), "music_stop_owner", None) is owner


def release_music_stop(router: Any, guild_id: int, owner: object) -> None:
    st = router.get_state(int(guild_id))
    if owner is not None and getattr(st, "music_stop_owner", None) is owner:
        st.music_stop_owner = None
