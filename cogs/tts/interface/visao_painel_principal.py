"""Painel principal de configuração do TTS.

Agrupa somente composição e navegação da interface do painel pessoal/servidor.
Não conhece síntese, áudio, fila, streaming, Worker, Termux ou APK.
"""
from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Type

import discord

from ..utils.embed import build_settings_panel_text_from_embed
from .modais_atts import enviar_modal_configuracao_atts
from .modais_servidor import ModalPrefixosServidor, ModalRegrasServidorTTS
from .modais_simples import ModalApelidoFalado
from .modais_vozes_online import ModalConfiguracaoEdge, ModalConfiguracaoGTTS
from .operacoes_painel import enviar_modal_configuracao_com_fallback
from .seletores_basicos import SeletorRegiaoVoz
from .visoes_auxiliares import VisaoLeituraRapidaTTS
from .visoes_base import VisaoSelecaoSimples
from .visoes_layout import VisaoLayoutBaseTTS


AbridorModalATTS = Callable[..., Awaitable[None]]
EnviadorModalFallback = Callable[..., Awaitable[None]]
RenderizadorPainel = Callable[..., str]


class VisaoPainelPrincipalTTS(VisaoLayoutBaseTTS):
    def __init__(
        self,
        cog: "TTSVoice",
        id_dono: int,
        id_servidor: int,
        *,
        servidor: bool = False,
        duracao: float = 180,
        id_usuario_alvo: int | None = None,
        nome_usuario_alvo: str | None = None,
        abrir_modal_atts: AbridorModalATTS = enviar_modal_configuracao_atts,
        enviar_modal_fallback: EnviadorModalFallback = enviar_modal_configuracao_com_fallback,
        classe_modal_edge: Type[discord.ui.Modal] = ModalConfiguracaoEdge,
        classe_modal_gtts: Type[discord.ui.Modal] = ModalConfiguracaoGTTS,
        classe_modal_apelido: Type[discord.ui.Modal] = ModalApelidoFalado,
        classe_modal_prefixos: Type[discord.ui.Modal] = ModalPrefixosServidor,
        classe_modal_regras: Type[discord.ui.Modal] = ModalRegrasServidorTTS,
        classe_visao_selecao: Type[discord.ui.View] = VisaoSelecaoSimples,
        classe_seletor_regiao_voz: Type[discord.ui.Select] = SeletorRegiaoVoz,
        classe_visao_leitura: Type[discord.ui.View] = VisaoLeituraRapidaTTS,
        renderizar_painel: RenderizadorPainel = build_settings_panel_text_from_embed,
    ):
        super().__init__(
            cog,
            id_dono,
            id_servidor,
            timeout=duracao,
            target_user_id=id_usuario_alvo,
            target_user_name=nome_usuario_alvo,
        )
        self.servidor = bool(servidor)
        self.server = self.servidor
        self.panel_kind = "server" if self.servidor else "user"
        self._panel_embed: discord.Embed | None = None
        self._fallback_buttons_ready = False
        self._abrir_modal_atts = abrir_modal_atts
        self._enviar_modal_fallback = enviar_modal_fallback
        self._classe_modal_edge = classe_modal_edge
        self._classe_modal_gtts = classe_modal_gtts
        self._classe_modal_apelido = classe_modal_apelido
        self._classe_modal_prefixos = classe_modal_prefixos
        self._classe_modal_regras = classe_modal_regras
        self._classe_visao_selecao = classe_visao_selecao
        self._classe_seletor_regiao_voz = classe_seletor_regiao_voz
        self._classe_visao_leitura = classe_visao_leitura
        self._renderizar_painel = renderizar_painel
        self._reconstruir_itens()

    def _dono_alvo(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    def painel_componentes_v2(self) -> bool:
        return bool(
            hasattr(discord.ui, "LayoutView")
            and isinstance(self, getattr(discord.ui, "LayoutView"))
            and hasattr(discord.ui, "Container")
            and hasattr(discord.ui, "TextDisplay")
            and hasattr(discord.ui, "ActionRow")
        )

    def definir_embed_painel(self, embed: discord.Embed | None) -> None:
        self._panel_embed = embed
        self._reconstruir_itens()

    def _texto_painel(self) -> str:
        if self._panel_embed is None:
            return "### TTS do servidor\nCarregando painel." if self.servidor else "### TTS\nCarregando painel."
        try:
            return self._renderizar_painel(self._panel_embed, server=self.servidor)
        except Exception as erro:
            print(f"[tts_panel] falha ao renderizar painel v2: {erro!r}")
            return str(getattr(self._panel_embed, "description", "") or "Painel de TTS")[:4000]

    def _criar_botao(
        self,
        rotulo: str,
        callback: Callable[[discord.Interaction], object],
        *,
        emoji: str | None = None,
        estilo: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> discord.ui.Button:
        botao = discord.ui.Button(label=rotulo, emoji=emoji, style=estilo)

        async def executar(interaction: discord.Interaction):
            resultado = callback(interaction)
            if inspect.isawaitable(resultado):
                await resultado

        botao.callback = executar
        return botao

    def _criar_linha_acao(self, *botoes: discord.ui.Button):
        if self.painel_componentes_v2():
            linha = discord.ui.ActionRow()
            for botao in botoes:
                linha.add_item(botao)
            return linha
        return list(botoes)

    def _adicionar_linha_controle(self, container, *botoes: discord.ui.Button) -> None:
        linha = self._criar_linha_acao(*botoes)
        if self.painel_componentes_v2():
            container.add_item(linha)
        else:
            for botao in linha:
                self.add_item(botao)

    def _apelido_falado_ativo(self) -> bool:
        if self.servidor:
            return False
        try:
            db = self.cog._get_db()
            padroes = db.get_guild_tts_defaults(self.guild_id) if db is not None else {}
            return bool((padroes or {}).get("announce_author", False))
        except Exception as erro:
            print(f"[tts_panel] falha ao verificar apelido falado do painel: {erro!r}")
            return False

    def _botoes_painel(self) -> list[discord.ui.Button]:
        if self.servidor:
            return [
                self._criar_botao("Configurar prefixos", self._abrir_painel_prefixos, emoji="⌨️"),
                self._criar_botao("Configurar Edge", self._abrir_painel_edge, emoji="🔊"),
                self._criar_botao("Configurar gTTS", self._abrir_painel_gtts, emoji="🔤"),
                self._criar_botao("Configurar regras", self._abrir_painel_regras, emoji="☑️"),
            ]

        botoes = [
            self._criar_botao("Configurar Edge", self._abrir_painel_edge, emoji="🔊"),
            self._criar_botao("Configurar gTTS", self._abrir_painel_gtts, emoji="🔤"),
        ]
        if self._apelido_falado_ativo():
            botoes.append(self._criar_botao("Alterar apelido", self._abrir_modal_apelido, emoji="🪪"))
        return botoes

    def _reconstruir_itens(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass

        botoes = self._botoes_painel()
        if self.painel_componentes_v2():
            container = discord.ui.Container(
                discord.ui.TextDisplay(self._texto_painel()),
                accent_color=discord.Color.blurple(),
            )
            try:
                container.add_item(discord.ui.Separator(visible=True))
            except TypeError:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("**Ajustes**\nEscolha o que deseja configurar."))
            for botao in botoes:
                self._adicionar_linha_controle(container, botao)
            self.add_item(container)
            return

        for botao in botoes:
            self.add_item(botao)

    async def _abrir_painel_modo(self, interaction: discord.Interaction, modo: str):
        print(f"[tts_panel] mode_select | mode={modo} user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.servidor}")
        mensagem_painel = getattr(interaction, "message", None)
        id_usuario_alvo = self.target_user_id
        nome_usuario_alvo = self.target_user_name
        if not self.servidor and id_usuario_alvo is None:
            id_usuario_alvo = interaction.user.id
            nome_usuario_alvo = self.cog._member_panel_name(interaction.user)

        if modo == "atts" or modo == "android_native":
            await self._abrir_modal_atts(
                interaction,
                self.cog,
                mensagem_painel,
                server=self.servidor,
                target_user_id=id_usuario_alvo,
                target_user_name=nome_usuario_alvo,
                context="panel-atts",
            )
        elif modo == "edge":
            await self._enviar_modal_fallback(
                interaction,
                lambda: self._classe_modal_edge(self.cog, mensagem_painel, server=self.servidor, target_user_id=id_usuario_alvo, target_user_name=nome_usuario_alvo),
                lambda: self._classe_modal_edge(self.cog, mensagem_painel, server=self.servidor, target_user_id=id_usuario_alvo, target_user_name=nome_usuario_alvo, force_text_fallback=True),
                context="panel-edge",
            )
        else:
            await self._enviar_modal_fallback(
                interaction,
                lambda: self._classe_modal_gtts(self.cog, mensagem_painel, server=self.servidor, target_user_id=id_usuario_alvo, target_user_name=nome_usuario_alvo),
                lambda: self._classe_modal_gtts(self.cog, mensagem_painel, server=self.servidor, target_user_id=id_usuario_alvo, target_user_name=nome_usuario_alvo, force_text_fallback=True),
                context="panel-gtts",
            )

    async def _abrir_painel_atts(self, interaction: discord.Interaction):
        await self._abrir_painel_modo(interaction, "atts")

    async def _abrir_painel_edge(self, interaction: discord.Interaction):
        await self._abrir_painel_modo(interaction, "edge")

    async def _abrir_painel_gtts(self, interaction: discord.Interaction):
        await self._abrir_painel_modo(interaction, "gtts")

    async def _abrir_painel_voz(self, interaction: discord.Interaction):
        print(f"[tts_panel] voice_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.servidor}")
        visao = self._classe_visao_selecao(
            self.cog,
            self._dono_alvo(interaction),
            self.guild_id,
            "Voz",
            "Escolha a região e depois a voz. Os nomes técnicos ficam só dentro desta lista.",
            self._classe_seletor_regiao_voz(self.cog, server=self.servidor),
            source_panel_message=interaction.message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await visao.send(interaction)

    async def _abrir_painel_leitura(self, interaction: discord.Interaction):
        print(f"[tts_panel] reading_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.servidor}")
        await self._classe_visao_leitura(
            self.cog,
            self._dono_alvo(interaction),
            self.guild_id,
            server=self.servidor,
            source_panel_message=interaction.message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        ).send(interaction)

    async def _abrir_modal_apelido(self, interaction: discord.Interaction):
        print(f"[tts_panel] spoken_name_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.servidor}")
        id_atual = int(self.target_user_id or interaction.user.id)
        valor_atual = self.cog._get_saved_spoken_name(self.guild_id, id_atual)
        await interaction.response.send_modal(
            self._classe_modal_apelido(
                self.cog,
                interaction.message,
                target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
                target_user_name=self.target_user_name,
                current_value=valor_atual,
            )
        )

    async def _abrir_painel_prefixos(self, interaction: discord.Interaction):
        if not self.servidor:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Indisponível", "Prefixos são ajustes do servidor.", ok=False),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(self._classe_modal_prefixos(self.cog, getattr(interaction, "message", None)))

    async def _abrir_painel_regras(self, interaction: discord.Interaction):
        if not self.servidor:
            return await self._abrir_painel_avancado(interaction)
        await self._enviar_modal_fallback(
            interaction,
            lambda: self._classe_modal_regras(self.cog, getattr(interaction, "message", None)),
            lambda: self._classe_modal_regras(self.cog, getattr(interaction, "message", None), force_text_fallback=True),
            context="server-rules",
        )

    async def _abrir_painel_avancado(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.cog._make_embed(
                "Opção removida",
                "Esse painel foi simplificado. Use Edge, gTTS, Apelido ou o comando separado do TTS do servidor.",
                ok=True,
            ),
            ephemeral=True,
        )

    # Fachadas internas legadas usadas por seletores e consumidores existentes.
    def _target_owner(self, interaction: discord.Interaction) -> int:
        return self._dono_alvo(interaction)

    def is_components_v2_panel(self) -> bool:
        return self.painel_componentes_v2()

    def set_panel_embed(self, embed: discord.Embed | None) -> None:
        self.definir_embed_painel(embed)

    def _panel_text(self) -> str:
        return self._texto_painel()

    def _make_button(self, label: str, callback: Callable[[discord.Interaction], object], *, emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> discord.ui.Button:
        return self._criar_botao(label, callback, emoji=emoji, estilo=style)

    def _make_action_row(self, *buttons: discord.ui.Button):
        return self._criar_linha_acao(*buttons)

    def _add_control_row(self, container, *buttons: discord.ui.Button) -> None:
        self._adicionar_linha_controle(container, *buttons)

    def _spoken_name_enabled(self) -> bool:
        return self._apelido_falado_ativo()

    def _panel_buttons(self) -> list[discord.ui.Button]:
        return self._botoes_painel()

    def _rebuild_items(self) -> None:
        self._reconstruir_itens()

    async def _open_mode_panel(self, interaction: discord.Interaction, mode: str):
        await self._abrir_painel_modo(interaction, mode)

    async def _open_atts_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_atts(interaction)

    async def _open_edge_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_edge(interaction)

    async def _open_gtts_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_gtts(interaction)

    async def _open_voice_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_voz(interaction)

    async def _open_reading_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_leitura(interaction)

    async def _open_spoken_name_modal(self, interaction: discord.Interaction):
        await self._abrir_modal_apelido(interaction)

    async def _open_prefixes_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_prefixos(interaction)

    async def _open_rules_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_regras(interaction)

    async def _open_advanced_panel(self, interaction: discord.Interaction):
        await self._abrir_painel_avancado(interaction)
