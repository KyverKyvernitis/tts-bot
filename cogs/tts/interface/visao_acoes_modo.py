"""Ações específicas dos modos de TTS exibidas pelo painel.

A implementação canônica fica isolada da fachada legada de ``ui.py`` e cuida
somente da navegação de interface entre voz, leitura, idioma e retorno ao
painel principal. Não conhece síntese, fila, streaming, Worker, Termux ou APK.
"""
from __future__ import annotations

import inspect
from typing import Callable

import discord

from .controles_paineis import SeletorAcaoModoTTS
from .modais_simples import VisaoAjudaIdioma
from .seletores_basicos import SeletorRegiaoVoz
from .visoes_auxiliares import VisaoLeituraRapidaTTS
from .visoes_base import VisaoBaseTTS, VisaoSelecaoSimples


class VisaoAcoesModoTTS(VisaoBaseTTS):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        modo: str,
        servidor: bool,
        mensagem_painel_origem: discord.Message | None,
        id_usuario_alvo: int | None = None,
        nome_usuario_alvo: str | None = None,
    ):
        super().__init__(
            cog,
            owner_id,
            guild_id,
            timeout=180,
            target_user_id=id_usuario_alvo,
            target_user_name=nome_usuario_alvo,
        )
        self.modo = str(modo or "edge")
        self.servidor = bool(servidor)
        self.mensagem_painel_origem = mensagem_painel_origem

        # Atributos legados continuam disponíveis porque seletores existentes
        # ainda os consultam diretamente enquanto a migração é incremental.
        self.mode = self.modo
        self.server = self.servidor
        self.source_panel_message = self.mensagem_painel_origem
        self.panel_kind = "server" if self.servidor else "user"
        self._montar_botoes()

    def _dono_alvo(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    def _criar_botao(
        self,
        rotulo: str,
        callback: Callable[[discord.Interaction], object],
        *,
        emoji: str | None = None,
        estilo: discord.ButtonStyle = discord.ButtonStyle.secondary,
        linha: int | None = None,
    ) -> discord.ui.Button:
        botao = discord.ui.Button(label=rotulo, emoji=emoji, style=estilo, row=linha)

        async def executar(interaction: discord.Interaction):
            resultado = callback(interaction)
            if inspect.isawaitable(resultado):
                await resultado

        botao.callback = executar
        return botao

    def _criar_seletor_acao_modo(self) -> discord.ui.Select:
        return SeletorAcaoModoTTS(self.modo)

    def _criar_seletor_regiao_voz(self) -> discord.ui.Select:
        return SeletorRegiaoVoz(self.cog, server=self.servidor)

    def _criar_visao_selecao_voz(self, interaction: discord.Interaction) -> VisaoSelecaoSimples:
        return VisaoSelecaoSimples(
            self.cog,
            self._dono_alvo(interaction),
            self.guild_id,
            "Voz Edge",
            "Muda a voz usada pelo modo Edge.",
            self._criar_seletor_regiao_voz(),
            source_panel_message=self.mensagem_painel_origem,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )

    def _criar_visao_leitura(self, interaction: discord.Interaction) -> VisaoLeituraRapidaTTS:
        return VisaoLeituraRapidaTTS(
            self.cog,
            self._dono_alvo(interaction),
            self.guild_id,
            server=self.servidor,
            source_panel_message=self.mensagem_painel_origem,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )

    def _criar_visao_ajuda_idioma(self, interaction: discord.Interaction) -> VisaoAjudaIdioma:
        return VisaoAjudaIdioma(
            self.cog,
            self._dono_alvo(interaction),
            self.guild_id,
            server=self.servidor,
            source_panel_message=self.mensagem_painel_origem,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )

    def _montar_botoes(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass
        self.add_item(self._criar_seletor_acao_modo())
        self.add_item(self._criar_botao("Voltar", self._voltar_painel_principal, emoji="⬅️", linha=1))

    def _prefixo_modo(self) -> str:
        # Texto curto de orientação. O painel principal mostra os prefixos reais
        # vindos do banco; aqui usamos os padrões para evitar consulta assíncrona
        # dentro de uma função de renderização simples.
        if self.modo in {"atts", "android_native"}:
            return "%"
        if self.modo == "edge":
            return ","
        return "."

    def _titulo_descricao_modo(self) -> tuple[str, str]:
        prefixo = self._prefixo_modo()
        if self.modo in {"atts", "android_native"}:
            return "ATTS", f"Usado quando a mensagem começa com `{prefixo}texto`. Escolha no menu o que quer mudar."
        if self.modo == "edge":
            return "Edge", f"Usado quando a mensagem começa com `{prefixo}texto`. Escolha no menu o que quer mudar."
        if self.modo == "gtts":
            return "gTTS", f"Usado quando a mensagem começa com `{prefixo}texto`. Escolha no menu o que quer mudar."
        return "gTTS", f"Usado quando a mensagem começa com `{prefixo}texto`. Escolha no menu o que quer mudar."

    async def enviar(self, interaction: discord.Interaction):
        titulo, descricao = self._titulo_descricao_modo()
        embed = self.cog._make_embed(titulo, descricao, ok=True)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def _abrir_voz_edge(self, interaction: discord.Interaction):
        await self._criar_visao_selecao_voz(interaction).send(interaction)

    async def _abrir_leitura_edge(self, interaction: discord.Interaction):
        await self._criar_visao_leitura(interaction).send(interaction)

    async def _abrir_idioma_gtts(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Idioma gTTS",
            description="Muda o idioma usado pelo modo gTTS. Exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=self._criar_visao_ajuda_idioma(interaction),
            ephemeral=True,
        )

    async def _voltar_painel_principal(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        id_alvo = int(self.target_user_id or interaction.user.id)
        nome_alvo = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            id_alvo if not self.servidor else interaction.user.id,
            server=self.servidor,
            panel_kind="server" if self.servidor else "user",
            target_user_name=nome_alvo if not self.servidor else None,
            viewer_user_id=interaction.user.id,
        )
        visao = self.cog._build_panel_view(
            self._dono_alvo(interaction),
            interaction.guild.id,
            server=self.servidor,
            target_user_id=None if self.servidor else id_alvo,
            target_user_name=None if self.servidor else nome_alvo,
        )
        conteudo, embed_edicao, visao_edicao = self.cog._prepare_panel_payload(embed=embed, view=visao)
        await interaction.response.edit_message(content=conteudo, embed=embed_edicao, view=visao_edicao)
        visao.message = getattr(interaction, "message", None)

    # Fachadas de compatibilidade para seletores e consumidores que ainda usam
    # os nomes privados antigos durante a migração incremental.
    def _target_owner(self, interaction: discord.Interaction) -> int:
        return self._dono_alvo(interaction)

    def _make_button(self, label: str, callback: Callable[[discord.Interaction], object], *, emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: int | None = None) -> discord.ui.Button:
        return self._criar_botao(label, callback, emoji=emoji, estilo=style, linha=row)

    def _build_buttons(self) -> None:
        self._montar_botoes()

    def _prefix_for_mode(self) -> str:
        return self._prefixo_modo()

    def _mode_title_description(self) -> tuple[str, str]:
        return self._titulo_descricao_modo()

    async def send(self, interaction: discord.Interaction):
        await self.enviar(interaction)

    async def _open_edge_voice(self, interaction: discord.Interaction):
        await self._abrir_voz_edge(interaction)

    async def _open_edge_reading(self, interaction: discord.Interaction):
        await self._abrir_leitura_edge(interaction)

    async def _open_gtts_language(self, interaction: discord.Interaction):
        await self._abrir_idioma_gtts(interaction)

    async def _back_to_main_panel(self, interaction: discord.Interaction):
        await self._voltar_painel_principal(interaction)
