"""Private /ttsendpoint modal. Never touches the synthesis hot path."""
from __future__ import annotations

import asyncio
import logging

import discord

from cogs.tts.edge_endpoint import ENDPOINT_LABELS, settings, transport_ready
from utility.technical_commands import can_use_technical_command

logger = logging.getLogger(__name__)


class EdgeEndpointModal(discord.ui.Modal):
    def __init__(self, cog):
        super().__init__(title="Endpoints Edge da VPS", timeout=300)
        self.cog = cog
        self.select = discord.ui.CheckboxGroup(min_values=1, max_values=2, required=True)
        for name, label in ENDPOINT_LABELS.items():
            self.select.add_option(
                label=label,
                value=name,
                description=("api.msedgeservices.com" if name == "services" else "speech.platform.bing.com"),
                default=name in settings.enabled,
            )
        self.add_item(discord.ui.Label(
            text="Endpoints permitidos",
            description="Marque um ou ambos. O segundo serve de alternativa, sem duplicar chamadas.",
            component=self.select,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await can_use_technical_command(self.cog.bot, interaction.user, interaction.guild):
            await interaction.response.send_message("Acesso restrito ao dono do bot na guilda administrativa.", ephemeral=True)
            return
        chosen = tuple(str(value) for value in self.select.values)
        if not chosen or len(set(chosen)) != len(chosen) or any(value not in ENDPOINT_LABELS for value in chosen):
            await interaction.response.send_message("Selecione ao menos um endpoint válido.", ephemeral=True)
            return
        if not transport_ready():
            await interaction.response.send_message(
                "O transporte Edge instalado não aceita troca dinâmica de endpoint.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # The modal is infrequent; disk writes run off the event loop.
        lock = self.cog._edge_endpoint_settings_lock
        try:
            async with lock:
                current = settings.enabled
                if set(chosen) == set(current):
                    updated = current
                else:
                    updated = await asyncio.to_thread(settings.save, chosen)
        except OSError:
            logger.exception("[tts_edge_endpoint] persistência falhou")
            await interaction.followup.send("Não foi possível salvar. A configuração anterior foi mantida.", ephemeral=True)
            return
        mode = "automático" if len(updated) == 2 else "fixo"
        readable = ", ".join(ENDPOINT_LABELS[key] for key in updated)
        logger.info("[tts_edge_endpoint] configuração alterada | modo=%s endpoints=%s", mode, ",".join(updated))
        await interaction.followup.send(
            f"Configuração da VPS salva. Modo: **{mode}**. Endpoints: **{readable}**. "
            "Novas sínteses usam essa seleção; as já iniciadas não mudam.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
