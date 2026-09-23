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


def consume_task_result(task: Any) -> None:
    """Collect a detached task result so late failures never become warnings.

    Blocking yt-dlp work can outlive the coroutine that requested cancellation.
    In that case the task is intentionally detached for a short period, but its
    eventual exception still needs an owner or asyncio reports
    ``exception was never retrieved`` / ``exception in shielded future``.
    """
    if not isinstance(task, asyncio.Future) or not task.done():
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


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


async def stop_player_instance(player: Any, *, disconnect: bool) -> None:
    """Best-effort stop and disconnect for discord.py voice clients."""
    if not player:
        return
    with contextlib.suppress(Exception):
        if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
            player.stop()
    if disconnect:
        with contextlib.suppress(Exception):
            await player.disconnect(force=True)
