from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Type

import discord

from .modais_atts import enviar_modal_configuracao_atts
from .modais_simples import (
    ModalPrefixoATTS,
    ModalPrefixoBot,
    ModalPrefixoEdge,
    ModalPrefixoGTTS,
    ModalPrefixoTeto,
)

if TYPE_CHECKING:
    from ..cog import TTSVoice


class BotaoLancadorPublicoTTS(discord.ui.Button):
    def __init__(self, *, acao: str, rotulo: str, emoji: str | None = None):
        super().__init__(label=rotulo, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.acao = str(acao)
        self.action = self.acao

    async def callback(self, interaction: discord.Interaction):
        painel = getattr(self, "view", None)
        if painel is None or interaction.guild is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        await painel._open_action(interaction, self.acao)


class SeletorAlvoPrefixo(discord.ui.Select):
    def __init__(
        self,
        cog: "TTSVoice",
        *,
        modal_bot: Type[discord.ui.Modal] = ModalPrefixoBot,
        modal_atts: Type[discord.ui.Modal] = ModalPrefixoATTS,
        modal_teto: Type[discord.ui.Modal] = ModalPrefixoTeto,
        modal_gtts: Type[discord.ui.Modal] = ModalPrefixoGTTS,
        modal_edge: Type[discord.ui.Modal] = ModalPrefixoEdge,
    ):
        self.cog = cog
        self._modal_bot = modal_bot
        self._modal_atts = modal_atts
        self._modal_teto = modal_teto
        self._modal_gtts = modal_gtts
        self._modal_edge = modal_edge
        options = [
            discord.SelectOption(label="Bot", description="Símbolo usado nos comandos do bot. Exemplo: _panel", value="bot", emoji="🤖"),
            discord.SelectOption(label="ATTS", description="Símbolo antes da frase para usar ATTS. Exemplo: %bom dia", value="atts", emoji="📱"),
            discord.SelectOption(label="Kasane Teto", description="Símbolo antes da frase para usar a Teto. Exemplo: 'bom dia", value="teto", emoji="🥖"),
            discord.SelectOption(label="gTTS", description="Símbolo antes da frase para usar gTTS. Exemplo: .bom dia", value="gtts", emoji="🔤"),
            discord.SelectOption(label="Edge", description="Símbolo antes da frase para usar Edge. Exemplo: ,bom dia", value="edge", emoji="🔊"),
        ]
        super().__init__(placeholder="Escolha qual prefixo alterar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        visao = getattr(self, "view", None)
        mensagem_painel = getattr(visao, "source_panel_message", None) or getattr(interaction, "message", None)
        id_dono = getattr(visao, "owner_id", interaction.user.id)
        id_servidor = getattr(visao, "guild_id", interaction.guild.id if interaction.guild else 0)
        valor = self.values[0]
        if valor == "bot":
            modal = self._modal_bot(self.cog, mensagem_painel, id_dono, id_servidor)
        elif valor == "atts":
            modal = self._modal_atts(self.cog, mensagem_painel, id_dono, id_servidor)
        elif valor == "teto":
            modal = self._modal_teto(self.cog, mensagem_painel, id_dono, id_servidor)
        elif valor == "edge":
            modal = self._modal_edge(self.cog, mensagem_painel, id_dono, id_servidor)
        else:
            modal = self._modal_gtts(self.cog, mensagem_painel, id_dono, id_servidor)
        await interaction.response.send_modal(modal)


class SeletorPainelPrincipalTTS(discord.ui.Select):
    def __init__(self, *, servidor: bool):
        self.servidor = bool(servidor)
        self.server = self.servidor
        if self.servidor:
            options = [
                discord.SelectOption(label="Prefixos", description="Símbolos do bot, ATTS, Teto, Edge e gTTS", value="prefixes", emoji="⌨️"),
                discord.SelectOption(label="ATTS", description="Android TTS padrão do servidor", value="atts", emoji="📱"),
                discord.SelectOption(label="Edge", description="Idioma, voz e leitura Edge padrão do servidor", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Idioma gTTS padrão do servidor", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Regras", description="Autor antes da frase e cargo ignorado", value="rules", emoji="☑️"),
            ]
            placeholder = "Escolha o ajuste do servidor"
        else:
            options = [
                discord.SelectOption(label="ATTS", description="Android nativo: idioma, voz, velocidade e tom", value="atts", emoji="📱"),
                discord.SelectOption(label="Edge", description="Voz natural: idioma, voz, velocidade e tom", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Voz simples: idioma usado no gTTS", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Apelido", description="Nome que o bot fala por você", value="spoken_name", emoji="🪪"),
            ]
            placeholder = "Escolha o que editar"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        painel = getattr(self, "view", None)
        if painel is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        valor = self.values[0]
        if valor == "atts":
            await painel._open_atts_panel(interaction)
        elif valor == "edge":
            await painel._open_edge_panel(interaction)
        elif valor == "gtts":
            await painel._open_gtts_panel(interaction)
        elif valor == "spoken_name":
            await painel._open_spoken_name_modal(interaction)
        elif valor == "prefixes":
            await painel._open_prefixes_panel(interaction)
        elif valor == "rules":
            await painel._open_rules_panel(interaction)
        else:
            await interaction.response.send_message("Opção indisponível.", ephemeral=True)


AbridorModalATTS = Callable[..., Awaitable[None]]


async def _abrir_modal_atts_padrao(
    interaction: discord.Interaction,
    cog: "TTSVoice",
    panel_message: discord.Message | None,
    *,
    server: bool,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
    context: str = "atts",
) -> None:
    await enviar_modal_configuracao_atts(
        interaction,
        cog,
        panel_message,
        servidor=server,
        id_usuario_alvo=target_user_id,
        nome_usuario_alvo=target_user_name,
        contexto=context,
    )


class SeletorAcaoModoTTS(discord.ui.Select):
    def __init__(self, modo: str, *, abrir_modal_atts: AbridorModalATTS = _abrir_modal_atts_padrao):
        self.modo = str(modo or "edge")
        self.mode = self.modo
        self._abrir_modal_atts = abrir_modal_atts
        if self.modo == "atts" or self.modo == "android_native":
            options = [
                discord.SelectOption(label="Configurar ATTS", description="Idioma, voz, velocidade e tom", value="atts_settings", emoji="📱"),
            ]
            placeholder = "Editar ATTS"
        elif self.modo == "edge":
            options = [
                discord.SelectOption(label="Voz Edge", description="Escolhe a voz usada no prefixo Edge", value="edge_voice", emoji="🎙️"),
                discord.SelectOption(label="Leitura Edge", description="Velocidade e tom do Edge", value="edge_reading", emoji="🎚️"),
            ]
            placeholder = "Editar Edge"
        elif self.modo == "gtts":
            options = [
                discord.SelectOption(label="Idioma gTTS", description="Idioma usado no prefixo gTTS", value="gtts_language", emoji="🌐"),
            ]
            placeholder = "Editar gTTS"
        else:
            options = [
                discord.SelectOption(label="Idioma gTTS", description="Idioma usado no prefixo gTTS", value="gtts_language", emoji="🌐"),
            ]
            placeholder = "Editar gTTS"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        painel = getattr(self, "view", None)
        if painel is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        valor = self.values[0]
        if valor == "atts_settings":
            await self._abrir_modal_atts(
                interaction,
                painel.cog,
                painel.source_panel_message,
                server=painel.server,
                target_user_id=painel.target_user_id,
                target_user_name=painel.target_user_name,
                context="mode-atts",
            )
        elif valor == "edge_voice":
            await painel._open_edge_voice(interaction)
        elif valor == "edge_reading":
            await painel._open_edge_reading(interaction)
        elif valor == "gtts_language":
            await painel._open_gtts_language(interaction)
        else:
            await interaction.response.send_message("Opção indisponível.", ephemeral=True)
