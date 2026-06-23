from __future__ import annotations

import contextlib
import logging
from typing import Any

import discord

from .cog import RoleIconUserError, member_label, role_label
from .modals import RoleIconConnectionModal
from .models import MAX_CONNECTIONS_PER_GUILD

log = logging.getLogger(__name__)


def _trim(text: str, limit: int = 3800) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


class _ConnectionSelect(discord.ui.Select):
    def __init__(self, panel: "RoleIconPanelView"):
        self.panel = panel
        guild = panel.cog.bot.get_guild(panel.guild_id)
        options: list[discord.SelectOption] = []
        for index, conn in enumerate(panel.connections):
            member = guild.get_member(int(conn.get("user_id") or 0)) if guild is not None else None
            role = guild.get_role(int(conn.get("role_id") or 0)) if guild is not None else None
            status = "ativo" if bool(conn.get("enabled", True)) else "desativado"
            label = f"{member_label(member, int(conn.get('user_id') or 0))} → {role_label(role, int(conn.get('role_id') or 0))}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(index),
                description=f"{status} · {str(conn.get('last_color') or 'sem cor')}"[:100],
                default=index == panel.selected_index,
            ))
        super().__init__(placeholder="Escolher conexão", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        try:
            self.panel.selected_index = int(self.values[0])
        except Exception:
            self.panel.selected_index = 0
        await self.panel.refresh(interaction)


class _AddButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        super().__init__(label="Adicionar", emoji="➕", style=discord.ButtonStyle.secondary)
        self.panel = panel
        self.disabled = len(panel.connections) >= MAX_CONNECTIONS_PER_GUILD

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        await interaction.response.send_modal(RoleIconConnectionModal(self.panel, mode="add"))


class _EditButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        super().__init__(label="Editar", style=discord.ButtonStyle.secondary)
        self.panel = panel
        self.disabled = not bool(panel.connections)

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        conn = self.panel.selected_connection
        if conn is None:
            await self.panel.cog.reply_error(interaction, "Nenhuma conexão selecionada.")
            return
        await interaction.response.send_modal(RoleIconConnectionModal(self.panel, mode="edit", connection=conn))


class _ToggleButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        conn = panel.selected_connection or {}
        enabled = bool(conn.get("enabled", True))
        super().__init__(label="Desativar" if enabled else "Ativar", style=discord.ButtonStyle.secondary)
        self.panel = panel
        self.disabled = not bool(panel.connections)

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        conn = self.panel.selected_connection
        if conn is None:
            await self.panel.cog.reply_error(interaction, "Nenhuma conexão selecionada.")
            return
        try:
            enabled = await self.panel.cog.toggle_connection(self.panel.guild_id, str(conn.get("id") or ""))
        except RoleIconUserError as exc:
            await self.panel.cog.reply_error(interaction, str(exc))
            return
        await self.panel.refresh(interaction, notice="Conexão ativada." if enabled else "Conexão desativada.")


class _RefreshBaseButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        super().__init__(label="Atualizar base", style=discord.ButtonStyle.secondary)
        self.panel = panel
        self.disabled = not bool(panel.connections)

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        conn = self.panel.selected_connection
        guild = interaction.guild
        if conn is None or guild is None:
            await self.panel.cog.reply_error(interaction, "Nenhuma conexão selecionada.")
            return
        try:
            await self.panel.cog.recapture_connection_base(guild, str(conn.get("id") or ""))
        except RoleIconUserError as exc:
            await self.panel.cog.reply_error(interaction, str(exc))
            return
        except Exception as exc:
            log.exception("[role_icons] recapture falhou")
            await self.panel.cog.reply_error(interaction, f"não consegui atualizar a base ({type(exc).__name__}).")
            return
        await self.panel.refresh(interaction, notice="Ícone base atualizado.")


class _ApplyNowButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        super().__init__(label="Aplicar agora", style=discord.ButtonStyle.secondary)
        self.panel = panel
        self.disabled = not bool(panel.connections)

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        conn = self.panel.selected_connection
        guild = interaction.guild
        if conn is None or guild is None:
            await self.panel.cog.reply_error(interaction, "Nenhuma conexão selecionada.")
            return
        member = await self.panel.cog._resolve_member(guild, int(conn.get("user_id") or 0))
        color_hex = await self.panel.cog.get_member_color_hex(member) if member is not None else None
        ok = await self.panel.cog.apply_connection(guild, conn, color_hex, force=True)
        await self.panel.refresh(interaction, notice="Ícone aplicado." if ok else "Não consegui aplicar. Veja o status da conexão.")


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        super().__init__(label="Preview", style=discord.ButtonStyle.secondary)
        self.panel = panel
        self.disabled = not bool(panel.connections)

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        conn = self.panel.selected_connection
        guild = interaction.guild
        if conn is None or guild is None:
            await self.panel.cog.reply_error(interaction, "Nenhuma conexão selecionada.")
            return
        try:
            file = await self.panel.cog.build_preview_file(guild, conn)
        except FileNotFoundError:
            await self.panel.cog.reply_error(interaction, "Ícone base ausente. Use Atualizar base.")
            return
        except Exception as exc:
            log.exception("[role_icons] preview falhou")
            await self.panel.cog.reply_error(interaction, f"não consegui gerar preview ({type(exc).__name__}).")
            return
        await interaction.response.send_message("Preview do ícone.", file=file, ephemeral=True)


class _RemoveButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        super().__init__(label="Remover", emoji="🗑️", style=discord.ButtonStyle.secondary)
        self.panel = panel
        self.disabled = not bool(panel.connections)

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        conn = self.panel.selected_connection
        if conn is None:
            await self.panel.cog.reply_error(interaction, "Nenhuma conexão selecionada.")
            return
        await interaction.response.send_message("Remover esta conexão?", ephemeral=True, view=_ConfirmRemoveView(self.panel, str(conn.get("id") or "")))


class _CloseButton(discord.ui.Button):
    def __init__(self, panel: "RoleIconPanelView"):
        super().__init__(label="Fechar", style=discord.ButtonStyle.secondary)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.ensure_owner(interaction):
            return
        with contextlib.suppress(Exception):
            await interaction.message.delete()
        if not interaction.response.is_done():
            with contextlib.suppress(Exception):
                await interaction.response.defer()


class _ConfirmRemoveView(discord.ui.LayoutView):
    def __init__(self, panel: "RoleIconPanelView", connection_id: str):
        super().__init__(timeout=60)
        self.panel = panel
        self.connection_id = str(connection_id)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Remover conexão"),
            discord.ui.TextDisplay("O ícone do cargo não será alterado automaticamente."),
            discord.ui.ActionRow(_ConfirmRemoveButton(self), _CancelRemoveButton(self)),
            accent_color=discord.Color.red(),
        ))


class _ConfirmRemoveButton(discord.ui.Button):
    def __init__(self, view: _ConfirmRemoveView):
        super().__init__(label="Remover", style=discord.ButtonStyle.secondary, emoji="🗑️")
        self.confirm_view = view

    async def callback(self, interaction: discord.Interaction):
        panel = self.confirm_view.panel
        if not await panel.ensure_owner(interaction):
            return
        ok = await panel.cog.remove_connection(panel.guild_id, self.confirm_view.connection_id)
        await interaction.response.edit_message(content="Conexão removida." if ok else "Conexão não encontrada.", view=None)
        await panel.refresh_from_message(notice="Conexão removida." if ok else None)


class _CancelRemoveButton(discord.ui.Button):
    def __init__(self, view: _ConfirmRemoveView):
        super().__init__(label="Cancelar", style=discord.ButtonStyle.secondary)
        self.confirm_view = view

    async def callback(self, interaction: discord.Interaction):
        panel = self.confirm_view.panel
        if not await panel.ensure_owner(interaction):
            return
        await interaction.response.edit_message(content="Cancelado.", view=None)


class RoleIconPanelView(discord.ui.LayoutView):
    def __init__(self, cog, *, guild_id: int, owner_id: int, selected_index: int = 0):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.owner_id = int(owner_id)
        self.selected_index = int(selected_index)
        self.message: discord.Message | None = None
        self.connections: list[dict[str, Any]] = []
        self._build_layout()

    @property
    def selected_connection(self) -> dict[str, Any] | None:
        if not self.connections:
            return None
        self.selected_index = max(0, min(self.selected_index, len(self.connections) - 1))
        return dict(self.connections[self.selected_index])

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction, *, notice: str | None = None):
        self._build_layout(notice=notice)
        if not interaction.response.is_done():
            await interaction.response.defer()
        target = interaction.message or self.message
        if target is not None:
            await target.edit(view=self)
            self.message = target

    async def refresh_from_message(self, *, notice: str | None = None):
        self._build_layout(notice=notice)
        if self.message is not None:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self)

    def _summary_lines(self) -> list[str]:
        guild = self.cog.bot.get_guild(self.guild_id)
        lines = [
            "# 🔗 Ícones de cargo conectados",
            f"Conexões: **{len(self.connections)}/{MAX_CONNECTIONS_PER_GUILD}**",
        ]
        if not self.connections:
            lines.append("Nenhum cargo conectado ainda.")
            return lines
        lines.append("")
        for index, conn in enumerate(self.connections, start=1):
            member = guild.get_member(int(conn.get("user_id") or 0)) if guild is not None else None
            role = guild.get_role(int(conn.get("role_id") or 0)) if guild is not None else None
            enabled = "ativo" if bool(conn.get("enabled", True)) else "desativado"
            color = str(conn.get("last_color") or "sem cor")
            marker = "▶" if index - 1 == self.selected_index else "•"
            lines.append(f"{marker} **{index}.** {member_label(member, int(conn.get('user_id') or 0))} → {role_label(role, int(conn.get('role_id') or 0))} · {enabled} · {color}")
        selected = self.selected_connection
        if selected is not None:
            status = str(selected.get("last_status") or "Aguardando cor.")
            lines.extend(["", f"**Status:** {status}"])
        return lines

    def _build_layout(self, *, notice: str | None = None):
        self.clear_items()
        cfg = self.cog._get_config(self.guild_id)
        self.connections = list(cfg.get("connections") or [])
        if self.connections:
            self.selected_index = max(0, min(self.selected_index, len(self.connections) - 1))
        else:
            self.selected_index = 0
        children: list[discord.ui.Item] = [discord.ui.TextDisplay(_trim("\n".join(self._summary_lines())))]
        if notice:
            children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(f"**{notice}**"))
        if self.connections:
            children.append(discord.ui.Separator())
            children.append(discord.ui.ActionRow(_ConnectionSelect(self)))
        children.extend([
            discord.ui.ActionRow(_AddButton(self), _EditButton(self), _ToggleButton(self)),
            discord.ui.ActionRow(_RefreshBaseButton(self), _ApplyNowButton(self), _PreviewButton(self)),
            discord.ui.ActionRow(_RemoveButton(self), _CloseButton(self)),
        ])
        self.add_item(discord.ui.Container(*children, accent_color=discord.Color.blurple()))
