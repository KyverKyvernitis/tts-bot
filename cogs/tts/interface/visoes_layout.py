"""Infraestrutura compartilhada dos painéis TTS baseados em LayoutView.

Mantém autorização, expiração e tratamento de erro dos painéis de layout sem
conhecer síntese, áudio, fila, Worker, Termux ou APK.
"""
from __future__ import annotations

import time

import discord

from .visoes_base import (
    DURACAO_DESPACHO_PAINEL_TTS,
    DURACAO_EXPIRACAO_PAINEL_TTS,
    mensagem_painel_expirado,
    responder_painel_tts_de_outro,
)


_CLASSE_VIEW_LAYOUT_TTS = getattr(discord.ui, "LayoutView", discord.ui.View)


class VisaoLayoutBaseTTS(_CLASSE_VIEW_LAYOUT_TTS):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        duracao_solicitada = max(1.0, float(timeout or DURACAO_EXPIRACAO_PAINEL_TTS))
        duracao_despacho = max(duracao_solicitada, DURACAO_DESPACHO_PAINEL_TTS)
        super().__init__(timeout=duracao_despacho)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.panel_kind: str = "user"
        self.target_user_id: int | None = target_user_id
        self.target_user_name: str | None = target_user_name
        self.expires_at_monotonic = time.monotonic() + duracao_solicitada

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            try:
                mensagem = await self.cog._build_expired_panel_message(self.guild_id, self.panel_kind)
            except Exception:
                mensagem = mensagem_painel_expirado(self.panel_kind)
            if interaction.response.is_done():
                await interaction.followup.send(mensagem, ephemeral=True)
            else:
                await interaction.response.send_message(mensagem, ephemeral=True)
            return False

        if self.owner_id == 0:
            return True
        if interaction.user.id != self.owner_id:
            await responder_painel_tts_de_outro(interaction)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(
            f"[tts_panel_error] user={getattr(interaction.user, 'id', None)} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"item={getattr(item, 'custom_id', None) or getattr(item, 'label', None) or type(item).__name__} "
            f"error={repr(error)}"
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
        except Exception as erro_resposta:
            print(f"[tts_panel_error] falha ao responder erro: {erro_resposta!r}")

    async def on_timeout(self) -> None:
        pass
