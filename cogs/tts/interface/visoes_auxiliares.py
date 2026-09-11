from __future__ import annotations

import discord

from .modais_simples import SeletorCargoIgnorado
from .seletores_basicos import SeletorTom, SeletorToggle, SeletorVelocidade
from .visoes_base import VisaoBaseTTS, VisaoSelecaoSimples


class VisaoConfiguracaoCargoIgnorado(VisaoBaseTTS):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        source_panel_message: discord.Message | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "server"
        self.source_panel_message = source_panel_message
        self.add_item(SeletorCargoIgnorado(cog))

    async def send(self, interaction: discord.Interaction):
        if self.source_panel_message is None:
            self.source_panel_message = getattr(interaction, "message", None)
        embed = self.cog._make_embed(
            "Cargo ignorado no TTS",
            "Selecione um cargo para ativar a regra. Desativar mantém o cargo salvo.",
            ok=True,
        )
        if interaction.response.is_done():
            msg = await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            try:
                msg = await interaction.original_response()
            except Exception:
                msg = None
        self.message = msg

    @discord.ui.button(label="Desativar", style=discord.ButtonStyle.danger, emoji="⛔", row=1)
    async def remove_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._remove_ignored_tts_role_from_panel(
            interaction,
            source_panel_message=self.source_panel_message,
        )


class VisaoLeituraRapidaTTS(VisaoBaseTTS):
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
        super().__init__(cog, owner_id, guild_id, timeout=180, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.source_panel_message = source_panel_message
        self.add_item(SeletorVelocidade(cog, server=server))
        self.add_item(SeletorTom(cog, server=server))
        for item in self.children:
            try:
                item.source_panel_message = source_panel_message
                item.target_user_id = target_user_id
                item.target_user_name = target_user_name
            except Exception:
                pass

    async def send(self, interaction: discord.Interaction):
        embed = self.cog._make_embed(
            "Leitura",
            "Muda velocidade e tom do modo Edge. O painel principal será atualizado depois de salvar.",
            ok=True,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)


class VisaoStatusTTS(VisaoBaseTTS):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.panel_kind = "status"

    def attach_message(self, message: discord.Message | None) -> None:
        self.message = message
        self.cog._register_status_view(self)

    async def refresh_from_config_change(self) -> None:
        if self.message is None or self.is_finished():
            return
        try:
            guild = self.cog.bot.get_guild(self.guild_id)
            if guild is None:
                self.cog._unregister_status_view(self)
                return
            target_user_id = int(self.target_user_id or self.owner_id or 0)
            member = guild.get_member(target_user_id) if target_user_id else None
            target_user_name = str(self.target_user_name or self.cog._member_panel_name(member))
            refreshed = await self.cog._build_status_embed(
                self.guild_id,
                target_user_id,
                viewer_user_id=self.owner_id,
                target_user_name=target_user_name,
                public=False,
            )
            await self.message.edit(embed=refreshed, view=self)
        except discord.NotFound:
            self.cog._unregister_status_view(self)
            self.stop()
        except Exception as e:
            print(f"[tts_status_refresh] falha ao atualizar status: {e!r}")

    async def on_timeout(self) -> None:
        self.cog._unregister_status_view(self)
        await super().on_timeout()

    @discord.ui.button(label="Abrir painel", style=discord.ButtonStyle.secondary, emoji="⚙️", row=0)
    async def open_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        target_user_id = int(self.target_user_id or interaction.user.id)
        target_user_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            target_user_id,
            server=False,
            panel_kind="user",
            target_user_name=target_user_name,
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
        )
        msg = await self.cog._respond(interaction, embed=embed, view=view, ephemeral=True)
        view.message = msg

    @discord.ui.button(label="Resetar para o padrão do servidor", style=discord.ButtonStyle.danger, emoji="♻️", row=0)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self.cog._get_db()
        if db is None or not hasattr(db, "reset_user_tts"):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para resetar as suas configurações.", ok=False),
                ephemeral=True,
            )
            return

        target_user_id = int(self.target_user_id or interaction.user.id)
        target_user_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        await self.cog._reset_user_tts_and_refresh(interaction.guild.id, target_user_id)

        refreshed = await self.cog._build_status_embed(
            interaction.guild.id,
            target_user_id,
            viewer_user_id=interaction.user.id,
            target_user_name=target_user_name,
            public=False,
        )
        await interaction.response.edit_message(embed=refreshed, view=self)
        await interaction.followup.send(
            embed=self.cog._make_embed("Configurações resetadas", f"As suas configurações de TTS agora seguem os padrões do servidor.", ok=True),
            ephemeral=True,
        )


class VisaoPainelToggleTTS(VisaoBaseTTS):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 180):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "toggle"

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    @discord.ui.button(label="Auto leave", style=discord.ButtonStyle.secondary, emoji="⏏️", row=1)
    async def auto_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await VisaoSelecaoSimples(self.cog, self._target_owner(interaction), self.guild_id, "Auto leave", "Escolha se o bot deve sair da call quando ficar sozinho ou só com bots.", SeletorToggle(self.cog, "auto_leave")).send(interaction)
