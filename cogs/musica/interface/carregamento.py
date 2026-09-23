from __future__ import annotations

import contextlib
import asyncio
import logging
from typing import Any

import discord

from cogs.musica import configuracao as config

logger = logging.getLogger(__name__)

MUSIC_LOADING_REACTION_EMOJI = str(
    getattr(config, "MUSIC_LOADING_REACTION_EMOJI", "<a:areia:1496606578395189473>")
    or "<a:areia:1496606578395189473>"
).strip()


class MusicLoadingReaction:
    """Precise loading reaction for music commands.

    The reaction is intentionally best-effort: missing permissions, ephemeral
    interaction messages, unavailable custom emoji, or deleted messages must not
    break playback/search. The caller decides the exact lifetime: search removes
    it when results are shown; direct play removes it only when playback is
    confirmed or when the request fails/cancels/timeouts.
    """

    def __init__(self, message: Any, *, emoji: str | None = None) -> None:
        self.message = message
        self.emoji = str(emoji or MUSIC_LOADING_REACTION_EMOJI).strip()
        self.active = False
        self._start_task: asyncio.Task | None = None

    def start_background(self) -> None:
        """A latência da reação do Discord não faz parte do caminho do áudio."""
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start_bounded())

    async def _start_bounded(self) -> None:
        try:
            await asyncio.wait_for(self.start(), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            logger.debug("[music] reação de carregamento indisponível", exc_info=True)

    async def start(self) -> None:
        if self.active or not self.message or not self.emoji:
            return
        add_reaction = getattr(self.message, "add_reaction", None)
        if not callable(add_reaction):
            return
        with contextlib.suppress(discord.HTTPException, discord.Forbidden, discord.NotFound, TypeError, ValueError):
            await add_reaction(self.emoji)
            self.active = True

    async def finish(self) -> None:
        task, self._start_task = self._start_task, None
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if not self.active or not self.message or not self.emoji:
            return
        remove_reaction = getattr(self.message, "remove_reaction", None)
        if not callable(remove_reaction):
            self.active = False
            return
        # discord.py expects a Member/User for remove_reaction. Prefer the
        # client's current user/member and fall back to clear_reaction only if
        # the bot has permission; failures are ignored either way.
        user = None
        with contextlib.suppress(Exception):
            user = getattr(getattr(self.message, "_state", None), "user", None)
        with contextlib.suppress(Exception):
            if user is None:
                user = getattr(getattr(self.message, "guild", None), "me", None)
        try:
            if user is not None:
                await remove_reaction(self.emoji, user)
            else:
                clear_reaction = getattr(self.message, "clear_reaction", None)
                if callable(clear_reaction):
                    await clear_reaction(self.emoji)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, TypeError, ValueError):
            pass
        finally:
            self.active = False
