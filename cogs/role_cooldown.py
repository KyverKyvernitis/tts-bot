import asyncio
import discord
from discord.ext import commands
from config import TARGET_ROLE_ID, DISABLE_TIME

class RoleCooldownCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cooldown_active = False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        role = message.guild.get_role(TARGET_ROLE_ID)
        if not role:
            return

        if role in message.role_mentions and not self.cooldown_active:
            self.cooldown_active = True

            try:
                if role.mentionable:
                    await role.edit(mentionable=False, reason="Cargo mencionado; auto-desativando menções")
            except Exception:
                pass

            await asyncio.sleep(DISABLE_TIME)

            role = message.guild.get_role(TARGET_ROLE_ID)
            if role:
                try:
                    await role.edit(mentionable=True, reason="Cooldown acabou; auto-reativando menções")
                except Exception:
                    pass

            self.cooldown_active = False

async def setup(bot: commands.Bot):
    await bot.add_cog(RoleCooldownCog(bot))
