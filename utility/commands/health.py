from __future__ import annotations

import discord
from discord import app_commands

HEALTH_COMMAND_GUILD_ID = 927002914449424404
HEALTH_COMMAND_GUILD = discord.Object(id=HEALTH_COMMAND_GUILD_ID)


class HealthCommandMixin:
    """Camada Discord do comando /health da cog Utility."""

    @app_commands.command(name="health", description="Mostra a saúde geral do bot, fila, cache, engines e guilds")
    @app_commands.guilds(HEALTH_COMMAND_GUILD)
    async def health(self, interaction: discord.Interaction):
        await self._send_health_response(interaction)
