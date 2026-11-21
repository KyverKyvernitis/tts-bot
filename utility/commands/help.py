from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tts.aliases import extract_prefixed_argument, matches_prefixed_command
from utility.help_activity import remember_help_category
from utility.help_center import HELP_CONTROL_ID, help_search_modal


class HelpCommandMixin:
    """Entradas slash/prefixadas da central de ajuda."""

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type not in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")
        if interaction.type is discord.InteractionType.component and custom_id.startswith("music:"):
            remember_help_category(self.bot, interaction, "music")
            return

        match = HELP_CONTROL_ID.fullmatch(custom_id)
        if match is None:
            return
        action = match["action"]
        if (action == "query") != (interaction.type is discord.InteractionType.modal_submit):
            return
        if interaction.user.id != int(match["owner"]):
            await interaction.response.send_message("Usa `/help` pra abrir seu próprio card.", ephemeral=True)
            return

        if action == "search":
            await interaction.response.send_modal(help_search_modal(custom_id))
            return

        if action == "close":
            await interaction.response.defer()
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
            return

        subject = "início"
        if action == "area":
            values = data.get("values") or []
            if not values or not str(values[0]).startswith("category:"):
                await interaction.response.send_message("Escolhe uma das áreas do menu.", ephemeral=True)
                return
            subject = values[0]
        elif action == "query":
            subject = ""
            for label in data.get("components", ()):
                field = label.get("component") or {}
                if field.get("custom_id") == "assunto":
                    subject = str(field.get("value") or "").strip()
                    break
            if not subject:
                await interaction.response.send_message("Me diz o nome de um comando ou de uma área.", ephemeral=True)
                return

        await interaction.response.defer()
        try:
            await self._send_help_view(
                interaction.edit_original_response, guild=interaction.guild, owner=interaction.user,
                channel=interaction.channel, app_permissions=interaction.app_permissions, source=match["source"],
                subject=subject, music_from_activity=match["music"] == "1",
            )
        except discord.NotFound:
            # O dono pode ter fechado o card enquanto a nova área carregava.
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return

        prefixes = await self._get_prefix_data(message.guild)
        bot_prefix = prefixes["bot_prefix"]
        if not matches_prefixed_command(message.content, bot_prefix, kind="help"):
            return

        subject = extract_prefixed_argument(message.content, bot_prefix, kind="help") or None
        await self._send_help_response(
            guild=message.guild,
            owner=message.author,
            responder=message.channel,
            prefix_command_message=message,
            subject=subject,
        )

    @app_commands.command(name="help", description="Veja meus comandos e como usar cada um")
    @app_commands.describe(assunto="O nome de um comando ou de uma área")
    async def help_command(self, interaction: discord.Interaction, assunto: str | None = None):
        await self._send_help_response(
            guild=interaction.guild,
            owner=interaction.user,
            responder=interaction.channel,
            interaction=interaction,
            ephemeral=True,
            subject=assunto,
        )

    @help_command.autocomplete("assunto")
    async def help_subject_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._help_autocomplete_choices(interaction, current)
