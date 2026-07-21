from .cog import FeedbackCog


async def setup(bot):
    await bot.add_cog(FeedbackCog(bot))
