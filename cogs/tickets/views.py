from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from .constants import (
    KIND_OTHER,
    KIND_PARTNERSHIP,
    KIND_REPORT,
    KIND_SUGGESTION,
    PUBLIC_OPTIONS,
    TICKET_KINDS,
)
from .permissions import permission_summary, reset_permissions
from .utils import accent_color, clean_panel_image_url, is_staff, truncate

if TYPE_CHECKING:
    from .cog import TicketsCog


def _option_label(kind: str) -> str:
    return PUBLIC_OPTIONS.get(kind, {}).get("label") or kind


def _option_emoji(kind: str) -> str:
    return PUBLIC_OPTIONS.get(kind, {}).get("emoji") or "🎫"


def _panel_image_component(url: object) -> discord.ui.Item | None:
    image_url = clean_panel_image_url(url)
    if not image_url:
        return None

    gallery_cls = getattr(discord.ui, "MediaGallery", None)
    item_cls = getattr(discord, "MediaGalleryItem", None) or getattr(discord.ui, "MediaGalleryItem", None)
    if gallery_cls is not None and item_cls is not None:
        attempts = (
            lambda: gallery_cls(item_cls(media=image_url)),
            lambda: gallery_cls(item_cls(url=image_url)),
            lambda: gallery_cls(items=[item_cls(media=image_url)]),
            lambda: gallery_cls(items=[item_cls(url=image_url)]),
            lambda: gallery_cls([item_cls(media=image_url)]),
            lambda: gallery_cls([item_cls(url=image_url)]),
        )
        for make_item in attempts:
            try:
                item = make_item()
                if item is not None:
                    return item
            except Exception:
                continue

    return discord.ui.TextDisplay(f"[Imagem do painel]({image_url})")


class TicketPublicPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        cfg = cog._get_config(self.guild_id)
        panel = cfg.get("panel") or {}
        enabled = cfg.get("enabled") or {}

        options: list[discord.SelectOption] = []
        for kind in TICKET_KINDS:
            if not bool(enabled.get(kind, True)):
                continue
            meta = PUBLIC_OPTIONS[kind]
            options.append(discord.SelectOption(
                label=meta["label"],
                value=kind,
                description=truncate(meta["description"], 100, suffix=""),
                emoji=meta["emoji"],
            ))

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"# {panel.get('title') or '🎫 Atendimento'}"),
            discord.ui.TextDisplay(str(panel.get("description") or "Escolha abaixo o tipo de atendimento.")),
        ]
        image_item = _panel_image_component(panel.get("image_url"))
        if image_item is not None:
            children.extend([discord.ui.Separator(), image_item])
        self.select: discord.ui.Select | None = None
        if options:
            select = discord.ui.Select(
                placeholder=truncate(panel.get("placeholder") or "Escolha uma opção", 100, suffix=""),
                min_values=1,
                max_values=1,
                options=options[:25],
                custom_id=f"tickets:panel:{self.guild_id}",
            )
            select.callback = self._on_select
            self.select = select
            children.extend([discord.ui.Separator(), discord.ui.ActionRow(select)])
        else:
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay("_Nenhuma opção está ativa no momento._")])

        self.add_item(discord.ui.Container(
            *children,
            accent_color=accent_color(panel.get("accent_color")),
        ))

    async def _on_select(self, interaction: discord.Interaction):
        value = ""
        try:
            value = str(((self.select.values if self.select is not None else []) or [""])[0])
        except Exception:
            value = ""
        if not value:
            try:
                data = interaction.data if isinstance(interaction.data, dict) else {}
                value = str((data.get("values") or [""])[0])
            except Exception:
                value = ""
        await self.cog._handle_public_choice(interaction, value)


class PartnershipConfirmView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        cfg = cog._get_config(self.guild_id)
        texts = cfg.get("texts") or {}
        confirm = discord.ui.Button(
            label="Criar ticket",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"tickets:partnership_confirm:{self.guild_id}:{self.user_id}",
        )
        confirm.callback = self._on_confirm
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🤝 Parceria"),
            discord.ui.TextDisplay(str(texts.get("partnership_confirm") or "Ao confirmar, criaremos um ticket privado.")),
            discord.ui.Separator(),
            discord.ui.ActionRow(confirm),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.user_id:
            await interaction.response.send_message("Só quem iniciou essa confirmação pode usar.", ephemeral=True)
            return False
        return True

    async def _on_confirm(self, interaction: discord.Interaction):
        await self.cog._create_ticket_from_interaction(interaction, kind=KIND_PARTNERSHIP, payload={})

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=SimpleNoticeView("🤝 Parceria cancelada.", color=discord.Color.dark_gray()))
        self.stop()


class SimpleNoticeView(discord.ui.LayoutView):
    def __init__(self, text: str, *, color: discord.Color | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(str(text or "")), accent_color=color or discord.Color.blurple()))


class TicketChannelView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)

        close = discord.ui.Button(
            label="Fechar",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=f"tickets:close:{self.guild_id}:{self.channel_id}",
        )
        close.callback = self._on_close
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Ações do ticket"),
            discord.ui.TextDisplay("Use o botão abaixo para encerrar este atendimento."),
            discord.ui.Separator(),
            discord.ui.ActionRow(close),
            accent_color=discord.Color.dark_gray(),
        ))

    async def _is_ticket_staff_or_owner(self, interaction: discord.Interaction) -> bool:
        cfg = self.cog._get_config(self.guild_id)
        ticket = self.cog._find_active_ticket(self.guild_id, self.channel_id)
        uid = int(getattr(interaction.user, "id", 0) or 0)
        if ticket and uid == int(ticket.get("user_id") or 0):
            return True
        return isinstance(interaction.user, discord.Member) and is_staff(interaction.user, cfg)

    async def _staff_only(self, interaction: discord.Interaction) -> bool:
        cfg = self.cog._get_config(self.guild_id)
        if isinstance(interaction.user, discord.Member) and is_staff(interaction.user, cfg):
            return True
        await interaction.response.send_message("Só a equipe pode usar essa ação.", ephemeral=True)
        return False

    async def _on_add_user(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        await interaction.response.send_message(
            view=AddUserView(self.cog, self.guild_id, self.channel_id, int(interaction.user.id)),
            ephemeral=True,
        )

    async def _on_transcript(self, interaction: discord.Interaction):
        if not await self._is_ticket_staff_or_owner(interaction):
            await interaction.response.send_message("Você não pode gerar transcript deste ticket.", ephemeral=True)
            return
        await self.cog._handle_transcript_button(interaction, self.channel_id)

    async def _on_close(self, interaction: discord.Interaction):
        if not await self._is_ticket_staff_or_owner(interaction):
            await interaction.response.send_message("Você não pode fechar este ticket.", ephemeral=True)
            return
        await self.cog._send_close_confirmation(
            interaction,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            user_id=int(interaction.user.id),
        )


class AddUserView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, channel_id: int, staff_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.staff_id = int(staff_id)
        self.select = discord.ui.UserSelect(
            placeholder="Escolha o usuário para adicionar ao ticket",
            min_values=1,
            max_values=1,
            custom_id=f"tickets:add_user_select:{self.guild_id}:{self.channel_id}:{self.staff_id}",
        )
        self.select.callback = self._on_select
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 👤 Adicionar usuário"),
            discord.ui.TextDisplay("Selecione quem também poderá ver e enviar mensagens neste ticket."),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.select),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu essa seleção pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        users = getattr(self.select, "values", []) or []
        user = users[0] if users else None
        await self.cog._add_user_to_ticket(interaction, self.channel_id, user)


class CloseConfirmView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, channel_id: int, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.user_id = int(user_id)
        confirm = discord.ui.Button(
            label="Confirmar fechamento",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=f"tickets:close_confirm:{self.guild_id}:{self.channel_id}:{self.user_id}",
        )
        cancel = discord.ui.Button(
            label="Cancelar",
            emoji="❌",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tickets:close_cancel:{self.guild_id}:{self.channel_id}:{self.user_id}",
        )
        confirm.callback = self._on_confirm
        cancel.callback = self._on_cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🔒 Fechar ticket"),
            discord.ui.TextDisplay("Confirme para gerar logs/transcript quando configurado e apagar o canal."),
            discord.ui.Separator(),
            discord.ui.ActionRow(confirm, cancel),
            accent_color=discord.Color.red(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.user_id:
            await interaction.response.send_message("Só quem iniciou o fechamento pode confirmar.", ephemeral=True)
            return False
        return True

    async def _on_confirm(self, interaction: discord.Interaction):
        await self.cog._close_ticket(interaction, self.channel_id)
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=SimpleNoticeView("Fechamento cancelado.", color=discord.Color.dark_gray()))
        self.stop()


class SuggestionMessageView(discord.ui.LayoutView):
    def __init__(self, *, guild_id: int, author_id: int, title: str, body: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# ⚡ Nova sugestão"),
            discord.ui.TextDisplay(f"**Autor:** <@{int(author_id)}> (`{int(author_id)}`)"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"## {truncate(title or 'Sugestão', 180)}"),
            discord.ui.TextDisplay(truncate(body or "_sem descrição_", 1800)),
            accent_color=discord.Color.gold(),
        ))


class TicketEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: discord.Message | None = None
        cfg = cog._get_config(self.guild_id)
        panel = cfg.get("panel") or {}
        channels = cfg.get("channels") or {}
        enabled = cfg.get("enabled") or {}
        options_cfg = cfg.get("options") or {}

        panel_channel_id = int(panel.get("channel_id") or 0)
        panel_message_id = int(panel.get("message_id") or 0)
        if panel_channel_id and panel_message_id:
            panel_ref = f"publicado em <#{panel_channel_id}>"
        else:
            panel_ref = "não publicado"

        category_id = int(channels.get("category_id") or 0)
        logs_channel_id = int(channels.get("logs_channel_id") or 0)
        suggestions_channel_id = int(channels.get("suggestions_channel_id") or 0)
        tickets_ref = f"<#{category_id}>" if category_id else "canal atual / sem categoria"
        logs_ref = f"<#{logs_channel_id}>" if logs_channel_id else "não configurado"
        suggestions_ref = f"<#{suggestions_channel_id}>" if suggestions_channel_id else "não configurado"
        webhook_ref = "servidor" if bool(options_cfg.get("use_server_webhook", False)) else "bot"

        active_names = [
            f"{PUBLIC_OPTIONS[k]['emoji']} {PUBLIC_OPTIONS[k]['label']}"
            for k in TICKET_KINDS
            if bool(enabled.get(k, True))
        ]
        active_text = " · ".join(active_names) if active_names else "nenhuma opção ativa"

        summary = (
            "Configure o sistema de atendimento deste servidor.\n"
            "Use o menu abaixo para escolher o que deseja editar.\n\n"
            f"**Painel:** {panel_ref}\n"
            f"**Tickets:** {tickets_ref}\n"
            f"**Logs:** {logs_ref}\n"
            f"**Sugestões:** {suggestions_ref}\n"
            f"**Envio:** {webhook_ref}\n\n"
            f"**Ativos:** {active_text}"
        )

        self.edit_select = discord.ui.Select(
            placeholder="Escolha o que editar...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Painel público",
                    value="panel",
                    description="Editar título, descrição, placeholder, cor e imagem.",
                    emoji="📝",
                ),
                discord.SelectOption(
                    label="Opções",
                    value="options",
                    description="Usar checkboxes para ligar/desligar opções.",
                    emoji="⚙️",
                ),
                discord.SelectOption(
                    label="Canais",
                    value="channels",
                    description="Selecionar categoria, logs e sugestões.",
                    emoji="📁",
                ),
                discord.SelectOption(
                    label="Cargos",
                    value="roles",
                    description="Selecionar cargos da equipe por tipo.",
                    emoji="👥",
                ),
                discord.SelectOption(
                    label="Permissões",
                    value="permissions",
                    description="Gerenciar @everyone, staff e autor do ticket.",
                    emoji="🔐",
                ),
                discord.SelectOption(
                    label="Denúncias",
                    value="reports",
                    description="Editar até 10 tipos de denúncia.",
                    emoji="👾",
                ),
                discord.SelectOption(
                    label="Textos",
                    value="texts",
                    description="Editar textos de abertura, confirmação e fechamento.",
                    emoji="💬",
                ),
            ],
            custom_id=f"tickets:editor_select:{guild_id}:{staff_id}",
        )
        self.edit_select.callback = self._on_select

        preview_btn = discord.ui.Button(
            label="Preview",
            emoji="👁️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tickets:edit_preview:{guild_id}:{staff_id}",
        )
        close_btn = discord.ui.Button(
            emoji="⛔",
            style=discord.ButtonStyle.danger,
            custom_id=f"tickets:edit_delete:{guild_id}:{staff_id}",
        )
        preview_btn.callback = self._on_preview
        close_btn.callback = self._on_delete

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🎫 Editor de Atendimento"),
            discord.ui.TextDisplay(summary),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.edit_select),
            discord.ui.ActionRow(preview_btn, close_btn),
            accent_color=discord.Color.gold(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu esse editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        value = ""
        try:
            value = str((getattr(self.edit_select, "values", []) or [""])[0])
        except Exception:
            value = ""
        if not value:
            try:
                data = interaction.data if isinstance(interaction.data, dict) else {}
                value = str((data.get("values") or [""])[0])
            except Exception:
                value = ""

        if value == "panel":
            await self._on_panel(interaction)
            return
        if value == "options":
            await self._on_options(interaction)
            return
        if value == "channels":
            await self._on_channels(interaction)
            return
        if value == "roles":
            await self._on_roles(interaction)
            return
        if value == "permissions":
            await self._on_permissions(interaction)
            return
        if value == "reports":
            await self._on_reports(interaction)
            return
        if value == "texts":
            await self._on_texts(interaction)
            return
        await interaction.response.send_message("Escolha inválida ou desatualizada.", ephemeral=True)

    async def _on_panel(self, interaction: discord.Interaction):
        from .modals import PanelEditModal
        await interaction.response.send_modal(PanelEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_options(self, interaction: discord.Interaction):
        from .modals import OptionsEditModal
        await interaction.response.send_modal(OptionsEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_channels(self, interaction: discord.Interaction):
        from .modals import ChannelsEditModal
        await interaction.response.send_modal(ChannelsEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_roles(self, interaction: discord.Interaction):
        from .modals import RolesEditModal
        await interaction.response.send_modal(RolesEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_permissions(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=TicketPermissionsEditorView(self.cog, self.guild_id, self.staff_id),
            ephemeral=True,
        )

    async def _on_reports(self, interaction: discord.Interaction):
        from .modals import ReportTypesEditModal
        await interaction.response.send_modal(ReportTypesEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_texts(self, interaction: discord.Interaction):
        from .modals import TextsEditModal
        await interaction.response.send_modal(TextsEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_preview(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=TicketPublicPanelView(self.cog, self.guild_id), ephemeral=True)

    async def _on_delete(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            if self.message is not None:
                await self.message.delete()
            else:
                await interaction.message.delete()
        except Exception:
            pass
        self.stop()


class TicketPermissionsEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(self.guild_id)

        self.select = discord.ui.Select(
            placeholder="Escolha o grupo de permissões...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="@everyone",
                    value="everyone",
                    description="Permissões padrão de todos no canal do ticket.",
                    emoji="🌐",
                ),
                discord.SelectOption(
                    label="Cargos staff",
                    value="staff",
                    description="Permissões dos cargos configurados como equipe.",
                    emoji="👥",
                ),
                discord.SelectOption(
                    label="Autor do ticket",
                    value="creator",
                    description="Permissões do membro que abriu o ticket.",
                    emoji="👤",
                ),
                discord.SelectOption(
                    label="Restaurar padrão seguro",
                    value="reset",
                    description="Voltar @everyone privado e autor/staff liberados.",
                    emoji="♻️",
                ),
            ],
            custom_id=f"tickets:permissions_select:{self.guild_id}:{self.staff_id}",
        )
        self.select.callback = self._on_select

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🔐 Permissões dos Tickets"),
            discord.ui.TextDisplay(
                "Configure quem pode ver, falar e gerenciar os canais criados.\n\n"
                f"**Atual:**\n{permission_summary(cfg)}"
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.select),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu o editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        value = ""
        try:
            value = str((getattr(self.select, "values", []) or [""])[0])
        except Exception:
            value = ""
        if not value:
            try:
                data = interaction.data if isinstance(interaction.data, dict) else {}
                value = str((data.get("values") or [""])[0])
            except Exception:
                value = ""

        if value == "reset":
            cfg = self.cog._get_config(self.guild_id)
            reset_permissions(cfg)
            await self.cog._save_config(self.guild_id, cfg)
            await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Permissões restauradas para o padrão seguro.")
            return

        if value in {"everyone", "staff", "creator"}:
            from .modals import PermissionsEditModal
            await interaction.response.send_modal(PermissionsEditModal(self.cog, self.guild_id, self.staff_id, value))
            return

        await interaction.response.send_message("Escolha inválida.", ephemeral=True)
