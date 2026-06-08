from __future__ import annotations

from .cog import TicketsCog


async def setup(bot):
    await bot.add_cog(TicketsCog(bot))
