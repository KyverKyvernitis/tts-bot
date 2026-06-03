from __future__ import annotations

from .cog import BirthdayCog

__all__ = ["BirthdayCog", "setup"]


async def setup(bot):
    from .cog import setup as _setup

    await _setup(bot)
