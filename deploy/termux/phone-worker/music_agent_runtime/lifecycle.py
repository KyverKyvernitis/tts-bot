"""Lifecycle primitives shared by MusicAgent without owning runtime state."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Iterable


def remove_owned_task(registry: dict[Any, Any], key: Any, task: Any) -> bool:
    """Remove *key* only while *task* is still its registered owner."""
    if registry.get(key) is not task:
        return False
    registry.pop(key, None)
    return True


def playback_owned(state: Any, track: Any, token: int) -> bool:
    """Return whether a delayed playback step still owns the state generation."""
    return (
        getattr(state, "current", None) is track
        and int(getattr(state, "playback_token", 0) or 0) == int(token or 0)
    )


async def cancel_tasks(tasks: Iterable[Any]) -> int:
    """Cancel distinct live tasks and wait until every cancellation is collected."""
    current = asyncio.current_task()
    pending: list[asyncio.Task] = []
    seen: set[int] = set()
    for task in tasks:
        if not isinstance(task, asyncio.Task) or task is current or task.done():
            continue
        marker = id(task)
        if marker in seen:
            continue
        seen.add(marker)
        task.cancel()
        pending.append(task)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return len(pending)


async def stop_player_instance(player: Any, *, disconnect: bool, wavelink_player_type: Any) -> None:
    """Best-effort stop and disconnect as independent cleanup attempts."""
    if not player:
        return
    if isinstance(player, wavelink_player_type):
        with contextlib.suppress(Exception):
            await player.stop()
        if disconnect:
            with contextlib.suppress(Exception):
                await player.disconnect()
        return
    with contextlib.suppress(Exception):
        if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
            player.stop()
    if disconnect:
        with contextlib.suppress(Exception):
            await player.disconnect(force=True)
