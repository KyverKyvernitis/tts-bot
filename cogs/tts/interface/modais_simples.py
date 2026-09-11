"""Modais e controles simples da interface do TTS.

Este módulo concentra componentes que apenas coletam uma escolha do usuário e
delegam a aplicação da mudança ao cog. Ele não conhece áudio, Worker, Termux ou
APK e não mantém estado global próprio.
"""
from __future__ import annotations

import discord


class ModalCodigoIdioma(discord.ui.Modal, title="Selecionar idioma"):
    language_code = discord.ui.TextInput(
        label="Digite um dos códigos",
        placeholder="pt-br, en, es, fr, ja",
        required=True,
        min_length=2,
        max_length=10,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = server
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_language_from_panel(
            interaction,
            str(self.language_code).strip(),
            server=self.server,
            source_panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class VisaoAjudaIdioma(discord.ui.View):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool = False,
        source_panel_message: discord.Message | None = None,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.server = server
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        target_owner = interaction.user.id if self.owner_id == 0 else self.owner_id
        if interaction.user.id != target_owner:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Sem permissão", "Esse painel pertence a outro usuário.", ok=False),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Ver lista de idiomas", style=discord.ButtonStyle.secondary, emoji="📚", row=0)
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = sorted(self.cog.gtts_languages.items())
        if not items:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Idiomas disponíveis", "Nenhum idioma encontrado.", ok=False),
                ephemeral=True,
            )
            return

        rows = []
        for i in range(0, len(items), 2):
            left_code, left_name = items[i]
            left = f"`{left_code}` — {left_name}"
            if i + 1 < len(items):
                right_code, right_name = items[i + 1]
                right = f"`{right_code}` — {right_name}"
                rows.append(f"{left}  |  {right}")
            else:
                rows.append(left)

        description = "\n".join(rows)
        embed = discord.Embed(
            title="Idiomas disponíveis",
            description=description,
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Selecionar idioma", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def select_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ModalCodigoIdioma(
                self.cog,
                self.source_panel_message,
                server=self.server,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            )
        )


class ModalPrefixoBot(discord.ui.Modal, title="Alterar prefixo do bot"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do bot",
        placeholder="Ex.: _",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="bot",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class ModalPrefixoATTS(discord.ui.Modal, title="Alterar prefixo do ATTS"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do ATTS",
        placeholder="Ex.: %",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="atts",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class ModalPrefixoTeto(discord.ui.Modal, title="Alterar prefixo da Kasane Teto"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo da Kasane Teto",
        placeholder="Ex.: '",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="teto",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class ModalPrefixoGTTS(discord.ui.Modal, title="Alterar prefixo do modo gTTS"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do modo gTTS",
        placeholder="Ex.: .",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="gtts",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class ModalPrefixoEdge(discord.ui.Modal, title="Alterar prefixo do modo Edge"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do modo Edge",
        placeholder="Ex.: ,",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="edge",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class SeletorCargoIgnorado(discord.ui.RoleSelect):
    def __init__(self, cog: "TTSVoice"):
        self.cog = cog
        super().__init__(
            placeholder="Selecione um cargo para ignorar no TTS",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        selected_role = self.values[0] if getattr(self, "values", None) else None
        if not isinstance(selected_role, discord.Role):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Cargo inválido", "Não consegui identificar o cargo selecionado.", ok=False),
                ephemeral=True,
            )
            return
        await self.cog._apply_ignored_tts_role_from_panel(
            interaction,
            selected_role,
            source_panel_message=source_panel_message,
        )


class ModalApelidoFalado(discord.ui.Modal, title="Alterar apelido falado"):
    spoken_name = discord.ui.TextInput(
        label="Apelido falado",
        placeholder="Digite um apelido pronunciável ou deixe vazio para limpar",
        required=False,
        max_length=32,
    )

    def __init__(
        self,
        cog: "TTSVoice",
        panel_message: discord.Message | None,
        *,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
        current_value: str | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        if current_value is not None:
            self.spoken_name.default = str(current_value or "")[:32]

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_spoken_name_from_modal(
            interaction,
            str(self.spoken_name),
            panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
