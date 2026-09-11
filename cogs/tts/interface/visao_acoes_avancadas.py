"""Ações avançadas do painel TTS.

Esta view reúne apenas navegação/configuração de interface. Ela não sintetiza
áudio, não manipula a fila e não conhece Worker, Termux ou APK.
"""
from __future__ import annotations

import discord

from .modais_simples import ModalApelidoFalado, VisaoAjudaIdioma
from .seletores_basicos import SeletorModo
from .visoes_auxiliares import VisaoConfiguracaoCargoIgnorado
from .visoes_base import VisaoBaseTTS, VisaoSelecaoSimples


class VisaoAcoesAvancadasTTS(VisaoBaseTTS):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(
            cog,
            owner_id,
            guild_id,
            timeout=180,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
        )
        self.server = server
        self.source_panel_message = source_panel_message
        self.panel_kind = "server" if server else "user"
        if not server:
            self.remove_item(self.ignored_role_button)
            # O apelido já fica no painel principal; aqui deixam só ajustes técnicos.
            self.remove_item(self.spoken_name_button)
        else:
            self.remove_item(self.spoken_name_button)

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    @discord.ui.button(label="Idioma gTTS", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def gtts_language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Idioma gTTS",
            description="Muda o idioma usado pelo modo gTTS. Exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=VisaoAjudaIdioma(
                self.cog,
                self._target_owner(interaction),
                self.guild_id,
                server=self.server,
                source_panel_message=self.source_panel_message,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Cargo ignorado", style=discord.ButtonStyle.secondary, emoji="🚫", row=1)
    async def ignored_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = VisaoConfiguracaoCargoIgnorado(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            source_panel_message=self.source_panel_message,
        )
        await view.send(interaction)

    @discord.ui.button(label="Modo de TTS", style=discord.ButtonStyle.secondary, emoji="🎛️", row=2)
    async def mode_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = VisaoSelecaoSimples(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Modo de TTS",
            "Escolhe o motor padrão usado por comandos antigos. Os prefixos ATTS, Kasane Teto, Edge e gTTS continuam escolhendo o motor por mensagem.",
            SeletorModo(self.cog, server=self.server),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await view.send(interaction)

    @discord.ui.button(label="Apelido", style=discord.ButtonStyle.secondary, emoji="🪪", row=1)
    async def spoken_name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_saved_spoken_name(self.guild_id, current_target_user_id)
        await interaction.response.send_modal(
            ModalApelidoFalado(
                self.cog,
                self.source_panel_message,
                target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
                target_user_name=self.target_user_name,
                current_value=current_value,
            )
        )
