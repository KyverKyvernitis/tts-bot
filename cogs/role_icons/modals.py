from __future__ import annotations

import logging

import discord

from .cog import RoleIconUserError, parse_discord_id

log = logging.getLogger(__name__)


class RoleIconConnectionModal(discord.ui.Modal):
    def __init__(self, panel: "RoleIconPanelView", *, mode: str, connection: dict | None = None):
        title = "Adicionar conexão" if mode == "add" else "Editar conexão"
        super().__init__(title=title)
        self.panel = panel
        self.mode = mode
        self.connection = dict(connection or {})
        self.user_input = discord.ui.TextInput(
            label="Usuário",
            placeholder="Menção ou ID do usuário",
            default=(f"{int(self.connection.get('user_id') or 0)}" if self.connection.get("user_id") else ""),
            min_length=1,
            max_length=80,
        )
        self.role_input = discord.ui.TextInput(
            label="Cargo conectado",
            placeholder="Menção ou ID do cargo com ícone",
            default=(f"{int(self.connection.get('role_id') or 0)}" if self.connection.get("role_id") else ""),
            min_length=1,
            max_length=80,
        )
        self.add_item(self.user_input)
        self.add_item(self.role_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await self.panel.cog.reply_error(interaction, "Isso só funciona dentro de um servidor.")
            return
        user_id = parse_discord_id(str(self.user_input.value or ""))
        role_id = parse_discord_id(str(self.role_input.value or ""))
        if user_id <= 0 or role_id <= 0:
            await self.panel.cog.reply_error(interaction, "Informe usuário e cargo por menção ou ID.")
            return
        try:
            if self.mode == "add":
                await self.panel.cog.add_connection(guild, user_id=user_id, role_id=role_id)
                message = "Conexão criada."
            else:
                await self.panel.cog.edit_connection(guild, str(self.connection.get("id") or ""), user_id=user_id, role_id=role_id)
                message = "Conexão atualizada."
        except RoleIconUserError as exc:
            await self.panel.cog.reply_error(interaction, str(exc))
            return
        except Exception as exc:
            log.exception("[role_icons] modal falhou")
            await self.panel.cog.reply_error(interaction, f"não consegui salvar agora ({type(exc).__name__}).")
            return
        await self.panel.refresh(interaction, notice=message)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .panel import RoleIconPanelView
