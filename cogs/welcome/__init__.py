from __future__ import annotations

from .cog import WelcomeCog

__all__ = ["WelcomeCog", "setup"]


async def setup(bot):
    from .cog import setup as _setup

    await _setup(bot)
