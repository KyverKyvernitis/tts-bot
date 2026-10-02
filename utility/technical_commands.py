"""Authorization shared by the owner's private technical commands."""

from __future__ import annotations

import contextlib
from typing import Any


TECHNICAL_COMMAND_GUILD_ID = 927002914449424404


async def can_use_technical_command(bot: Any, author: Any, guild: Any) -> bool:
    """Fail closed before any technical collection or file generation."""
    with contextlib.suppress(Exception):
        if guild is not None and int(guild.id) == TECHNICAL_COMMAND_GUILD_ID:
            return bool(await bot.is_owner(author))
    return False
