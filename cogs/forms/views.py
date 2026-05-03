"""Views da cog de formulários.

- FormView: mensagem persistente postada no canal de form.
- ResponseReviewView: mensagem enviada ao canal da staff, com campos separados
  e botões opcionais de aprovação/rejeição.
- CustomizationPanelView: painel `c` compacto, sem previews, com controles.
- SetupView: setup inicial com ChannelSelect.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord

from .constants import (
    BUTTON_LABEL_MAX,
    CID_CUST_APPROVAL_EDIT_BTN,
    CID_CUST_APPROVAL_ROLE_CLEAR_BTN,
    CID_CUST_APPROVAL_ROLE_SELECT,
    CID_CUST_APPROVAL_TOGGLE_BTN,
    CID_CUST_DELETE_BTN,
    CID_CUST_MODAL_BTN,
    CID_CUST_OPTIONS_BTN,
    CID_CUST_PANEL_BTN,
    CID_CUST_RESPONSE_BTN,
    CID_REVIEW_APPROVE_PREFIX,
    CID_REVIEW_REJECT_PREFIX,
    CID_SETUP_CONFIRM_BTN,
    CID_SETUP_FORM_SELECT,
    CID_SETUP_RESP_SELECT,
    CID_SUBMIT_PREFIX,
    CUSTOMIZATION_VIEW_TIMEOUT,
    DEFAULT_APPROVAL,
    DEFAULT_MODAL,
    DEFAULT_PANEL,
    DEFAULT_RESPONSE,
    SETUP_VIEW_TIMEOUT,
)

if TYPE_CHECKING:
    from .cog import FormsCog


def _truncate(text, limit: int) -> str:
    text = str(text or "")
    return text[:limit] if len(text) > limit else text


def _clean_emoji(text: str | None) -> str | None:
    text = str(text or "").strip()
    return text or None


def _media_url(text: str | None) -> str:
    text = str(text or "").strip()
    if text.startswith(("https://", "http://", "attachment://")):
        return text
    return ""


def _style_from_name(name: str | None, default: discord.ButtonStyle = discord.ButtonStyle.primary) -> discord.ButtonStyle:
    name = str(name or "").strip().lower()
    mapping = {
        "primary": discord.ButtonStyle.primary,
        "blurple": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "gray": discord.ButtonStyle.secondary,
        "grey": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "green": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
        "red": discord.ButtonStyle.danger,
    }
    return mapping.get(name, default)


def _format_field_block(label: str, value: str) -> str:
    label = str(label or "Campo").strip() or "Campo"
    value = str(value or "—").strip() or "—"
    return f"**{label}**\n{value}"


def _style_label(name: str | None) -> str:
    name = str(name or "").strip().lower()
    mapping = {
        "primary": "🟦 Azul/Roxo",
        "blurple": "🟦 Azul/Roxo",
        "secondary": "⬛ Cinza",
        "gray": "⬛ Cinza",
        "grey": "⬛ Cinza",
        "success": "🟩 Verde",
        "green": "🟩 Verde",
        "danger": "🟥 Vermelho",
        "red": "🟥 Vermelho",
    }
    return mapping.get(name, "🟦 Azul/Roxo")


def _style_value(name: str | None, default: str = "primary") -> str:
    name = str(name or "").strip().lower()
    aliases = {
        "primary": "primary",
        "blurple": "primary",
        "secondary": "secondary",
        "gray": "secondary",
        "grey": "secondary",
        "success": "success",
        "green": "success",
        "danger": "danger",
        "red": "danger",
    }
    return aliases.get(name, default)


class FormView(discord.ui.LayoutView):
    """View persistente do form público."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}
        title = str(panel.get("title") or DEFAULT_PANEL["title"])
        description = str(panel.get("description") or DEFAULT_PANEL["description"])
        button_label = _truncate(panel.get("button_label") or DEFAULT_PANEL["button_label"], BUTTON_LABEL_MAX)
        button_emoji = _clean_emoji(panel.get("button_emoji") or DEFAULT_PANEL.get("button_emoji"))
        media_url = _media_url(panel.get("media_url"))

        button = discord.ui.Button(
            label=button_label,
            emoji=button_emoji,
            style=_style_from_name(panel.get("button_style"), discord.ButtonStyle.primary),
            custom_id=f"{CID_SUBMIT_PREFIX}:{self.guild_id}",
        )
        button.callback = self._on_submit_click

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"# {title}"),
            discord.ui.TextDisplay(description),
        ]
        if media_url:
            children.extend([
                discord.ui.Separator(),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media_url, description="Imagem/GIF do formulário")
                ),
            ])
        children.extend([discord.ui.Separator(), discord.ui.ActionRow(button)])

        self.add_item(
            discord.ui.Container(
                *children,
                accent_color=discord.Color.blurple(),
            )
        )

    async def _on_submit_click(self, interaction: discord.Interaction):
        await self.cog._handle_submit_click(interaction, self.guild_id)


class ResponseReviewView(discord.ui.LayoutView):
    """Mensagem de submissão enviada ao canal de respostas."""

    def __init__(
        self,
        cog: "FormsCog",
        guild_id: int,
        applicant_id: int,
        field_values: dict[str, str],
        *,
        status: str = "",
        reviewer_mention: str = "",
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.applicant_id = int(applicant_id)
        self.field_values = dict(field_values or {})
        self.status = str(status or "")
        self.reviewer_mention = str(reviewer_mention or "")
        self._build()

    def _build(self):
        cfg = self.cog._get_config(self.guild_id)
        modal = cfg.get("modal") or {}
        response = cfg.get("response") or {}
        approval = cfg.get("approval") or {}

        title = str(response.get("title") or DEFAULT_RESPONSE["title"])
        intro = str(response.get("intro") or "").strip()
        footer_tpl = str(response.get("footer") or DEFAULT_RESPONSE["footer"])
        media_url = _media_url(response.get("media_url"))

        ctx = self.cog._build_template_ctx(
            self.guild_id,
            self.applicant_id,
            self.field_values,
        )
        footer = self.cog._safe_format(footer_tpl, ctx)

        children: list[discord.ui.Item] = [discord.ui.TextDisplay(f"# {title}")]
        if intro:
            children.append(discord.ui.TextDisplay(self.cog._safe_format(intro, ctx)))
        children.extend([
            discord.ui.Separator(),
            discord.ui.TextDisplay(_format_field_block(modal.get("field1_label") or DEFAULT_MODAL["field1_label"], self.field_values.get("field1") or "")),
            discord.ui.TextDisplay(_format_field_block(modal.get("field2_label") or DEFAULT_MODAL["field2_label"], self.field_values.get("field2") or "")),
            discord.ui.TextDisplay(_format_field_block(modal.get("field3_label") or DEFAULT_MODAL["field3_label"], self.field_values.get("field3") or "")),
        ])
        if media_url:
            children.extend([
                discord.ui.Separator(),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media_url, description="Imagem/GIF da verificação")
                ),
            ])
        if footer:
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay(footer)])

        if self.status:
            label = "Aprovado" if self.status == "approved" else "Rejeitado"
            emoji = "✅" if self.status == "approved" else "❌"
            by = f" por {self.reviewer_mention}" if self.reviewer_mention else ""
            children.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"## {emoji} {label}{by}"),
            ])
        elif bool(approval.get("enabled", False)):
            approve_label = _truncate(approval.get("approve_label") or DEFAULT_APPROVAL["approve_label"], BUTTON_LABEL_MAX)
            reject_label = _truncate(approval.get("reject_label") or DEFAULT_APPROVAL["reject_label"], BUTTON_LABEL_MAX)
            approve_btn = discord.ui.Button(
                label=approve_label,
                emoji=_clean_emoji(approval.get("approve_emoji") or DEFAULT_APPROVAL["approve_emoji"]),
                style=_style_from_name(approval.get("approve_style"), discord.ButtonStyle.success),
                custom_id=f"{CID_REVIEW_APPROVE_PREFIX}:{self.guild_id}:{self.applicant_id}",
            )
            reject_btn = discord.ui.Button(
                label=reject_label,
                emoji=_clean_emoji(approval.get("reject_emoji") or DEFAULT_APPROVAL["reject_emoji"]),
                style=_style_from_name(approval.get("reject_style"), discord.ButtonStyle.danger),
                custom_id=f"{CID_REVIEW_REJECT_PREFIX}:{self.guild_id}:{self.applicant_id}",
            )
            approve_btn.callback = self._on_approve
            reject_btn.callback = self._on_reject
            children.extend([discord.ui.Separator(), discord.ui.ActionRow(approve_btn, reject_btn)])

        self.add_item(
            discord.ui.Container(
                *children,
                accent_color=(discord.Color.green() if self.status == "approved" else discord.Color.red() if self.status == "rejected" else discord.Color.blurple()),
            )
        )

    async def _on_approve(self, interaction: discord.Interaction):
        await self.cog._handle_review_action(interaction, self, approved=True)

    async def _on_reject(self, interaction: discord.Interaction):
        await self.cog._handle_review_action(interaction, self, approved=False)


class _ApprovalRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent: "CustomizationPanelView", *, role_id: int = 0):
        self.parent_view = parent
        role_id = int(role_id or 0)

        default_values = []
        placeholder = "Escolher cargo ao aprovar"
        guild = parent.cog.bot.get_guild(parent.guild_id) if getattr(parent.cog, "bot", None) is not None else None
        if role_id:
            role = guild.get_role(role_id) if guild is not None else None
            if role is not None:
                default_values = [role]
                placeholder = f"Atual: {role.name}"[:150]
            else:
                default_values = [discord.Object(id=role_id)]
                placeholder = "Cargo atual salvo fora do cache"

        kwargs = {
            "placeholder": placeholder,
            "min_values": 1,
            "max_values": 1,
            "custom_id": CID_CUST_APPROVAL_ROLE_SELECT,
        }
        if default_values:
            kwargs["default_values"] = default_values
        super().__init__(**kwargs)

    async def callback(self, interaction: discord.Interaction):
        if not await self.parent_view.interaction_check(interaction):
            return
        if not self.values:
            await interaction.response.send_message("Escolha um cargo.", ephemeral=True)
            return
        role = self.values[0]
        await self.parent_view.cog._set_approval_role(interaction, int(role.id))




class CustomizationPanelView(discord.ui.LayoutView):
    """Painel `c` compacto."""

    def __init__(
        self,
        cog: "FormsCog",
        *,
        guild_id: int,
        staff_id: int,
    ):
        super().__init__(timeout=float(CUSTOMIZATION_VIEW_TIMEOUT))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: Optional[discord.Message] = None
        self._build()

    def _build(self):
        cfg = self.cog._get_config(self.guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        resp_ch_id = int(cfg.get("responses_channel_id") or 0)
        panel = cfg.get("panel") or {}
        modal = cfg.get("modal") or {}
        response = cfg.get("response") or {}
        approval = cfg.get("approval") or {}
        role_id = int(approval.get("role_id") or 0)
        approval_enabled = bool(approval.get("enabled", False))
        approval_state = "ligado" if approval_enabled else "desligado"
        role_text = f"<@&{role_id}>" if role_id else "nenhum cargo configurado"
        media_panel = "configurada" if _media_url(panel.get("media_url")) else "não configurada"
        media_response = "configurada" if _media_url(response.get("media_url")) else "não configurada"

        field_summary = (
            f"{modal.get('field1_label') or DEFAULT_MODAL['field1_label']} / "
            f"{modal.get('field2_label') or DEFAULT_MODAL['field2_label']} / "
            f"{modal.get('field3_label') or DEFAULT_MODAL['field3_label']}"
        )

        panel_btn = discord.ui.Button(
            label="Editar painel",
            emoji="📝",
            style=discord.ButtonStyle.primary,
            custom_id=CID_CUST_PANEL_BTN,
        )
        panel_btn.callback = self._on_panel
        modal_btn = discord.ui.Button(
            label="Editar campos",
            emoji="📋",
            style=discord.ButtonStyle.primary,
            custom_id=CID_CUST_MODAL_BTN,
        )
        modal_btn.callback = self._on_modal
        response_btn = discord.ui.Button(
            label="Editar resposta",
            emoji="📨",
            style=discord.ButtonStyle.primary,
            custom_id=CID_CUST_RESPONSE_BTN,
        )
        response_btn.callback = self._on_response
        toggle_btn = discord.ui.Button(
            label=("Desativar aprovação" if approval_enabled else "Ativar aprovação"),
            emoji=("🔕" if approval_enabled else "✅"),
            style=(discord.ButtonStyle.secondary if approval_enabled else discord.ButtonStyle.success),
            custom_id=CID_CUST_APPROVAL_TOGGLE_BTN,
        )
        toggle_btn.callback = self._on_approval_toggle
        approval_edit_btn = discord.ui.Button(
            label="Editar aprovação",
            emoji="🛠️",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_CUST_APPROVAL_EDIT_BTN,
        )
        approval_edit_btn.callback = self._on_approval_edit
        options_btn = discord.ui.Button(
            label="Editar opções",
            emoji="☑️",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_CUST_OPTIONS_BTN,
        )
        options_btn.callback = self._on_options
        delete_btn = discord.ui.Button(
            label="Apagar painel",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=CID_CUST_DELETE_BTN,
        )
        delete_btn.callback = self._on_delete

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# ⚙️ Customização do formulário"),
                discord.ui.TextDisplay(
                    "Painel compacto, sem previews. Textos ficam em modais; opções marcáveis ficam no modal moderno **Editar opções**.\n\n"
                    f"**Canal do formulário:** {f'<#{form_ch_id}>' if form_ch_id else '_não configurado_'}\n"
                    f"**Canal das respostas:** {f'<#{resp_ch_id}>' if resp_ch_id else '_não configurado_'}\n"
                    f"**Campos do formulário:** {field_summary}\n"
                    f"**Mídia do painel:** {media_panel}\n"
                    f"**Mídia da resposta:** {media_response}\n"
                    f"**Aprovação:** **{approval_state}**\n"
                    f"**Cargo ao aprovar:** {role_text}\n"
                    f"**Cores:** Preencher {_style_label(panel.get('button_style') or DEFAULT_PANEL.get('button_style'))} • Aprovar {_style_label(approval.get('approve_style') or DEFAULT_APPROVAL.get('approve_style'))} • Rejeitar {_style_label(approval.get('reject_style') or DEFAULT_APPROVAL.get('reject_style'))}\n\n"
                    "A mensagem `c` fica no chat enquanto este painel existir. Ela só é apagada quando outro `c` for enviado por staff ou quando você clicar em **Apagar painel**."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(panel_btn, modal_btn, response_btn),
                discord.ui.ActionRow(toggle_btn, approval_edit_btn, options_btn, delete_btn),
                accent_color=discord.Color.gold(),
            )
        )

        if approval_enabled:
            clear_role_btn = discord.ui.Button(
                label="Limpar cargo",
                emoji="🧹",
                style=discord.ButtonStyle.secondary,
                custom_id=CID_CUST_APPROVAL_ROLE_CLEAR_BTN,
                disabled=not bool(role_id),
            )
            clear_role_btn.callback = self._on_clear_role
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("## 🎭 Cargo ao aprovar"),
                    discord.ui.TextDisplay(
                        "Selecione o cargo que o botão **Aprovar** vai entregar. "
                        f"\n**Cargo atual:** {role_text}\n"
                        "Se nenhum cargo ficar configurado, o botão só envia a DM."
                    ),
                    discord.ui.ActionRow(_ApprovalRoleSelect(self, role_id=role_id)),
                    discord.ui.ActionRow(clear_role_btn),
                    accent_color=discord.Color.green(),
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            try:
                await interaction.response.send_message(
                    "Só quem abriu esse painel pode usar.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return False
        return True

    async def on_timeout(self):
        # Não apaga automaticamente: o usuário pediu para limpar só com outro `c`
        # ou pelo botão de apagar.
        return

    async def _on_panel(self, interaction: discord.Interaction):
        from .modals import PanelEditModal
        await interaction.response.send_modal(PanelEditModal(self.cog, self.guild_id))

    async def _on_modal(self, interaction: discord.Interaction):
        from .modals import SubmissionModalEditModal
        await interaction.response.send_modal(SubmissionModalEditModal(self.cog, self.guild_id))

    async def _on_response(self, interaction: discord.Interaction):
        from .modals import ResponseEditModal
        await interaction.response.send_modal(ResponseEditModal(self.cog, self.guild_id))

    async def _on_approval_toggle(self, interaction: discord.Interaction):
        await self.cog._toggle_approval(interaction)

    async def _on_approval_edit(self, interaction: discord.Interaction):
        from .modals import ApprovalEditModal
        await interaction.response.send_modal(ApprovalEditModal(self.cog, self.guild_id))

    async def _on_options(self, interaction: discord.Interaction):
        from .modals import ApprovalOptionsModal
        try:
            await interaction.response.send_modal(ApprovalOptionsModal(self.cog, self.guild_id))
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def _on_clear_role(self, interaction: discord.Interaction):
        await self.cog._set_approval_role(interaction, 0)

    async def _on_delete(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        await self.cog._purge_previous_c_session(self.guild_id)
        self.stop()


class SetupView(discord.ui.LayoutView):
    """Wizard inicial: 2 ChannelSelect + Confirmar."""

    def __init__(
        self,
        cog: "FormsCog",
        *,
        guild_id: int,
        staff_id: int,
    ):
        super().__init__(timeout=float(SETUP_VIEW_TIMEOUT))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: Optional[discord.Message] = None
        self.selected_form_channel_id: int = 0
        self.selected_resp_channel_id: int = 0

        self.form_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal de formulário",
            min_values=1,
            max_values=1,
            custom_id=CID_SETUP_FORM_SELECT,
        )
        self.form_select.callback = self._on_form_select

        self.resp_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal de respostas",
            min_values=1,
            max_values=1,
            custom_id=CID_SETUP_RESP_SELECT,
        )
        self.resp_select.callback = self._on_resp_select

        self.confirm_btn = discord.ui.Button(
            label="Confirmar",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=CID_SETUP_CONFIRM_BTN,
            disabled=True,
        )
        self.confirm_btn.callback = self._on_confirm

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# ⚙️ Configuração do formulário"),
                discord.ui.TextDisplay(
                    "Escolha o **canal de formulário** (onde o botão fica visível "
                    "pra todos) e o **canal de respostas** (pra onde as submissões "
                    "vão). Depois clique em Confirmar."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(self.form_select),
                discord.ui.ActionRow(self.resp_select),
                discord.ui.ActionRow(self.confirm_btn),
                accent_color=discord.Color.blue(),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            try:
                await interaction.response.send_message(
                    "Só quem abriu essa configuração pode usar.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return False
        return True

    def _maybe_enable_confirm(self):
        self.confirm_btn.disabled = not (
            self.selected_form_channel_id and self.selected_resp_channel_id
        )

    async def _on_form_select(self, interaction: discord.Interaction):
        if not self.form_select.values:
            return
        self.selected_form_channel_id = int(self.form_select.values[0].id)
        self._maybe_enable_confirm()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def _on_resp_select(self, interaction: discord.Interaction):
        if not self.resp_select.values:
            return
        self.selected_resp_channel_id = int(self.resp_select.values[0].id)
        self._maybe_enable_confirm()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def _on_confirm(self, interaction: discord.Interaction):
        if not (self.selected_form_channel_id and self.selected_resp_channel_id):
            try:
                await interaction.response.send_message(
                    "Selecione os dois canais antes de confirmar.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

        await self.cog._finalize_setup(
            interaction,
            setup_view=self,
            form_channel_id=self.selected_form_channel_id,
            resp_channel_id=self.selected_resp_channel_id,
        )
        self.stop()
