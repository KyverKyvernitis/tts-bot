from __future__ import annotations

from .cog import RoleIconsCog


async def setup(bot):
    await bot.add_cog(RoleIconsCog(bot))
