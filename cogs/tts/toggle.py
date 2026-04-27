"""Cog separado só para o slash `/toggle_menu`.

Mora fora do `TTSVoice` porque o decorator `app_commands.guilds(*GUILD_IDS)`
precisa ser aplicado dinamicamente — se `GUILD_IDS` está vazio o comando vira
global, se está populado vira guild-only sem precisar mexer no cog principal.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config

# Lê uma vez no import: usado pelo decorator abaixo pra restringir o slash
# por guild quando o config tem GUILD_IDS definido.
TOGGLE_GUILD_IDS = tuple(getattr(config, "GUILD_IDS", []) or [])


def _toggle_guilds_decorator(func):
    # Se GUILD_IDS está vazio, vira no-op e o comando fica global.
    if TOGGLE_GUILD_IDS:
        return app_commands.guilds(*TOGGLE_GUILD_IDS)(func)
    return func


class TTSToggle(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _tts_cog(self):
        # O painel reusa toda a lógica do TTSVoice; aqui só busca a referência.
        return self.bot.get_cog("TTSVoice")

    @app_commands.command(name="toggle_menu", description="Abre um painel guiado para os toggles de TTS")
    @_toggle_guilds_decorator
    async def toggle_menu(self, interaction: discord.Interaction):
        tts = self._tts_cog()
        if tts is None:
            if interaction.response.is_done():
                await interaction.followup.send("O módulo de TTS não está carregado.", ephemeral=True)
            else:
                await interaction.response.send_message("O módulo de TTS não está carregado.", ephemeral=True)
            return

        await tts._defer_ephemeral(interaction)
        if not await tts._require_guild(interaction):
            return
        if not await tts._require_toggle_allowed_guild(interaction):
            return
        if not await tts._require_kick_members(interaction):
            return

        embed = await tts._build_toggle_embed(interaction.guild.id, interaction.user.id)
        view = tts._build_toggle_view(interaction.user.id, interaction.guild.id)
        msg = await tts._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, discord.ui.View):
            setattr(view, "message", msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSToggle(bot))
