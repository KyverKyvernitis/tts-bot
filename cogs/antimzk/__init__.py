import discord
from discord import app_commands
from discord.ext import commands as dcommands

from .cog import AntiMzkCore
from .constants import _guild_scoped


class AntiMzkCog(AntiMzkCore, dcommands.Cog):
    @_guild_scoped()
    @app_commands.command(name="modo_censura", description="Gerencia as roles e modos do modo censura")
    @app_commands.describe(
        action="Escolha o que fazer",
        role_id="ID da role para adicionar ou remover",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Adicionar role", value="add"),
        app_commands.Choice(name="Remover role", value="remove"),
        app_commands.Choice(name="Listar roles", value="list"),
        app_commands.Choice(name="Ativar ou desativar", value="toggle"),
        app_commands.Choice(name="Ativar ou desativar só para staff", value="toggle_kick_only"),
        app_commands.Choice(name="Definir cargo staff", value="set_staff_role"),
        app_commands.Choice(name="Remover cargo staff", value="clear_staff_role"),
    ])
    async def antimzk(
        self,
        interaction: discord.Interaction,
        action: str,
        role_id: str | None = None,
    ):
        await self._run_antimzk_command(interaction, action, role_id)

    @antimzk.error
    async def antimzk_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_antimzk_error(interaction, error)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._handle_antimzk_message(message)


async def setup(bot: dcommands.Bot):
    await bot.add_cog(AntiMzkCog(bot, bot.settings_db))
