from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tts.aliases import extract_prefixed_argument, matches_prefixed_command
from cogs.tts.interface.visoes_base import DURACAO_PAINEL_PUBLICO_TTS
from utility.help_activity import remember_help_category
from utility.help_center import HELP_CONTROL_ID, send_help_layout


log = logging.getLogger(__name__)


class _OpenHelpCard:
    """Guarda a última página entregue até o prazo da mensagem original."""

    def __init__(
        self, cards: dict[int, _OpenHelpCard], message: discord.Message,
        edit: Callable[..., Awaitable[discord.Message]],
    ):
        self.cards = cards
        self.message = message
        self.edit = edit
        self.lock = asyncio.Lock()
        self.closed = False
        age = max(0.0, (discord.utils.utcnow() - message.created_at).total_seconds())
        self.deadline = asyncio.get_running_loop().time() + max(0.0, DURACAO_PAINEL_PUBLICO_TTS - age)
        cards[message.id] = self
        self.timer = asyncio.create_task(self._expire_at_deadline())

    @property
    def expired(self) -> bool:
        return asyncio.get_running_loop().time() >= self.deadline

    def finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.cards.pop(self.message.id, None)
        if self.timer is not asyncio.current_task():
            self.timer.cancel()

    async def disable(self) -> None:
        if self.closed:
            return
        view = discord.ui.LayoutView.from_message(self.message, timeout=None)
        view.stop()
        controls = [item for item in view.walk_children() if isinstance(item, (discord.ui.Button, discord.ui.Select))]
        if any(not item.disabled for item in controls):
            for item in controls:
                item.disabled = True
            await send_help_layout(self.edit, view)
        self.finish()

    async def _expire_at_deadline(self) -> None:
        try:
            await asyncio.sleep(max(0.0, self.deadline - asyncio.get_running_loop().time()))
            for attempt in range(2):
                try:
                    async with self.lock:
                        await self.disable()
                    return
                except (discord.DiscordServerError, OSError):
                    if attempt:
                        raise
                    await asyncio.sleep(5)
        except discord.NotFound:
            pass
        except (discord.HTTPException, OSError) as error:
            log.warning("Não consegui desativar o help %s: %s", self.message.id, error)
        finally:
            self.finish()


class HelpCommandMixin:
    """Entradas slash/prefixadas da central de ajuda."""

    def _track_help_card(
        self, message: discord.Message, edit: Callable[..., Awaitable[discord.Message]],
    ) -> _OpenHelpCard:
        # Um reload do cog preserva os prazos já agendados no bot.
        cards = getattr(self.bot, "_open_help_cards", None)
        if cards is None:
            cards = self.bot._open_help_cards = {}
        return cards.get(message.id) or _OpenHelpCard(cards, message, edit)

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

        # "search" leva cartões anteriores ao índice. Um formulário já aberto ainda pode ser enviado.
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

        if interaction.message is None:
            await interaction.response.send_message("Usa `/help` pra abrir meus comandos.", ephemeral=True)
            return
        # Capture o card antes do defer: o timer pode terminar enquanto o Discord confirma o clique.
        card = getattr(self.bot, "_open_help_cards", {}).get(interaction.message.id)
        await interaction.response.defer()
        if card is None:
            card = self._track_help_card(interaction.message, interaction.edit_original_response)
        try:
            view = None
            if action != "close" and not card.closed and not card.expired:
                view = await self._build_help_view(
                    guild=interaction.guild, owner=interaction.user, channel=interaction.channel,
                    app_permissions=interaction.app_permissions, source=match["source"],
                    subject=subject, music_from_activity=match["music"] == "1",
                )
            async with card.lock:
                if card.closed:
                    return
                if card.expired:
                    await card.disable()
                elif action == "close":
                    await interaction.delete_original_response()
                    card.finish()
                elif view is not None:
                    card.message = await send_help_layout(interaction.edit_original_response, view)
        except discord.NotFound:
            card.finish()

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
