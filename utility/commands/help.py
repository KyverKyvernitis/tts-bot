from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tts.aliases import matches_prefixed_command


class HelpCommandMixin:
    """Camada Discord do comando help da cog Utility.

    A lógica pesada de páginas/sessões continua em cogs.utility para reaproveitar
    as views e helpers existentes; este módulo mantém o comando em arquivo próprio.
    """

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return

        prefixes = await self._get_prefix_data(message.guild)
        bot_prefix = prefixes["bot_prefix"]
        if not matches_prefixed_command(message.content, bot_prefix, kind="help"):
            return

        await self._send_help_response(
            guild=message.guild,
            owner=message.author,
            responder=message.channel,
            prefix_command_message=message,
        )

    @app_commands.command(name="help", description="Mostra a central de ajuda com todos os comandos principais do bot")
    async def help_command(self, interaction: discord.Interaction):
        await self._send_help_response(
            guild=interaction.guild,
            owner=interaction.user,
            responder=interaction.channel,
            interaction=interaction,
            ephemeral=True,
        )
