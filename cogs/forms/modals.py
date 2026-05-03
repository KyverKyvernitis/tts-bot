"""Modais da cog de formulários.

- FormSubmissionModal: aberto pelo botão do form. Coleta 3 campos separados.
- PanelEditModal: edita texto/botão/mídia do painel público.
- SubmissionModalEditModal: edita o título do modal e os 3 campos usando
  linhas "Label | Placeholder" para caber no limite de 5 inputs do Discord.
- ResponseEditModal: edita a aparência da mensagem enviada ao canal da staff.
- ApprovalEditModal: edita textos/emoji e DMs de aprovação/rejeição.
- ApprovalOptionsModal: usa componentes novos de modal (RadioGroup,
  Checkbox e RoleSelect) para opções marcáveis, cores e cargo.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .constants import (
    BUTTON_EMOJI_MAX,
    BUTTON_LABEL_MAX,
    FIELD_CONFIG_MAX,
    FIELD_VALUE_LONG_MAX,
    FIELD_VALUE_SHORT_MAX,
    MEDIA_URL_MAX,
    MODAL_TITLE_MAX,
    PANEL_DESCRIPTION_MAX,
    PANEL_TITLE_MAX,
    RESPONSE_FOOTER_MAX,
    RESPONSE_INTRO_MAX,
    RESPONSE_TITLE_MAX,
    REVIEW_DM_MAX,
)

if TYPE_CHECKING:
    from .cog import FormsCog


def _truncate(text, limit: int) -> str:
    text = str(text or "")
    return text[:limit] if len(text) > limit else text


def _modal_config_line(label: str, placeholder: str) -> str:
    label = str(label or "").strip()
    placeholder = str(placeholder or "").strip()
    if placeholder:
        return _truncate(f"{label} | {placeholder}", FIELD_CONFIG_MAX)
    return _truncate(label, FIELD_CONFIG_MAX)


def _parse_modal_config_line(raw: str, *, fallback_label: str, fallback_placeholder: str) -> tuple[str, str]:
    raw = str(raw or "").strip()
    if not raw:
        return fallback_label, fallback_placeholder
    if "|" in raw:
        label, placeholder = raw.split("|", 1)
        label = label.strip() or fallback_label
        placeholder = placeholder.strip()
        return label, placeholder
    return raw.strip() or fallback_label, fallback_placeholder


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


def _make_style_radio_group(current: str | None, *, default: str = "primary"):
    current = _style_value(current, default)
    group = discord.ui.RadioGroup(required=True)
    for value, label, description in (
        ("primary", "Azul/Roxo", "Cor principal do Discord"),
        ("secondary", "Cinza", "Cor neutra"),
        ("success", "Verde", "Cor positiva"),
        ("danger", "Vermelho", "Cor de alerta"),
    ):
        group.add_option(
            label=label,
            value=value,
            description=description,
            default=(value == current),
        )
    return group


def _selected_style(group, fallback: str = "primary") -> str:
    return _style_value(getattr(group, "value", None) or fallback, fallback)


class FormSubmissionModal(discord.ui.Modal):
    """Modal mostrado ao usuário ao clicar no botão do form."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        cfg = cog._get_config(guild_id)
        modal_cfg = cfg.get("modal") or {}
        title = _truncate(modal_cfg.get("title") or "Nova verificação", MODAL_TITLE_MAX)
        super().__init__(title=title)
        self.cog = cog
        self.guild_id = int(guild_id)

        f1_label = _truncate(modal_cfg.get("field1_label") or "Nome", 45)
        f1_placeholder = _truncate(modal_cfg.get("field1_placeholder") or "Leonardo", 100)
        f2_label = _truncate(modal_cfg.get("field2_label") or "Idade e pronome", 45)
        f2_placeholder = _truncate(modal_cfg.get("field2_placeholder") or "17, ele", 100)
        f3_label = _truncate(modal_cfg.get("field3_label") or "Descrição", 45)
        f3_placeholder = _truncate(modal_cfg.get("field3_placeholder") or "Não sei", 100)

        self.field1_input = discord.ui.TextInput(
            label=f1_label,
            placeholder=f1_placeholder,
            style=discord.TextStyle.short,
            max_length=FIELD_VALUE_SHORT_MAX,
            required=bool(modal_cfg.get("field1_required", True)),
        )
        self.field2_input = discord.ui.TextInput(
            label=f2_label,
            placeholder=f2_placeholder,
            style=discord.TextStyle.short,
            max_length=FIELD_VALUE_SHORT_MAX,
            required=bool(modal_cfg.get("field2_required", True)),
        )
        self.field3_input = discord.ui.TextInput(
            label=f3_label,
            placeholder=f3_placeholder,
            style=discord.TextStyle.paragraph,
            max_length=FIELD_VALUE_LONG_MAX,
            required=bool(modal_cfg.get("field3_required", True)),
        )
        self.add_item(self.field1_input)
        self.add_item(self.field2_input)
        self.add_item(self.field3_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._handle_submission(
            interaction,
            field_values={
                "field1": str(self.field1_input.value or "").strip(),
                "field2": str(self.field2_input.value or "").strip(),
                "field3": str(self.field3_input.value or "").strip(),
            },
        )


class PanelEditModal(discord.ui.Modal):
    """Edita o painel público do formulário."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar painel do form")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}

        self.title_input = discord.ui.TextInput(
            label="Título",
            default=_truncate(panel.get("title") or "", PANEL_TITLE_MAX),
            style=discord.TextStyle.short,
            max_length=PANEL_TITLE_MAX,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Descrição",
            default=_truncate(panel.get("description") or "", PANEL_DESCRIPTION_MAX),
            style=discord.TextStyle.paragraph,
            max_length=PANEL_DESCRIPTION_MAX,
            required=True,
        )
        self.button_input = discord.ui.TextInput(
            label="Botão: texto",
            default=_truncate(panel.get("button_label") or "", BUTTON_LABEL_MAX),
            style=discord.TextStyle.short,
            max_length=BUTTON_LABEL_MAX,
            required=True,
        )
        self.button_emoji_input = discord.ui.TextInput(
            label="Botão: emoji opcional",
            default=_truncate(panel.get("button_emoji") or "", BUTTON_EMOJI_MAX),
            style=discord.TextStyle.short,
            max_length=BUTTON_EMOJI_MAX,
            required=False,
        )
        self.media_url_input = discord.ui.TextInput(
            label="Imagem/GIF por URL opcional",
            default=_truncate(panel.get("media_url") or "", MEDIA_URL_MAX),
            style=discord.TextStyle.paragraph,
            max_length=MEDIA_URL_MAX,
            required=False,
        )

        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(self.button_input)
        self.add_item(self.button_emoji_input)
        self.add_item(self.media_url_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._update_panel_config(
            interaction,
            title=str(self.title_input.value or "").strip(),
            description=str(self.desc_input.value or "").strip(),
            button_label=str(self.button_input.value or "").strip(),
            button_emoji=str(self.button_emoji_input.value or "").strip(),
            media_url=str(self.media_url_input.value or "").strip(),
        )


class SubmissionModalEditModal(discord.ui.Modal):
    """Edita o modal visto pelo usuário.

    Para caber em um único modal, cada campo usa formato:
      Label | Placeholder
    Exemplo: Nome | Leonardo
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar modal de submissão")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        modal = cfg.get("modal") or {}

        self.title_input = discord.ui.TextInput(
            label="Título do modal",
            default=_truncate(modal.get("title") or "", MODAL_TITLE_MAX),
            max_length=MODAL_TITLE_MAX,
            required=True,
        )
        self.field1_input = discord.ui.TextInput(
            label="Campo 1: Label | Placeholder",
            default=_modal_config_line(modal.get("field1_label") or "Nome", modal.get("field1_placeholder") or "Leonardo"),
            max_length=FIELD_CONFIG_MAX,
            required=True,
        )
        self.field2_input = discord.ui.TextInput(
            label="Campo 2: Label | Placeholder",
            default=_modal_config_line(modal.get("field2_label") or "Idade e pronome", modal.get("field2_placeholder") or "17, ele"),
            max_length=FIELD_CONFIG_MAX,
            required=True,
        )
        self.field3_input = discord.ui.TextInput(
            label="Campo 3: Label | Placeholder",
            default=_modal_config_line(modal.get("field3_label") or "Descrição", modal.get("field3_placeholder") or "Não sei"),
            max_length=FIELD_CONFIG_MAX,
            required=True,
        )

        self.add_item(self.title_input)
        self.add_item(self.field1_input)
        self.add_item(self.field2_input)
        self.add_item(self.field3_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        modal = cfg.get("modal") or {}
        f1_label, f1_ph = _parse_modal_config_line(
            str(self.field1_input.value or ""),
            fallback_label=str(modal.get("field1_label") or "Nome"),
            fallback_placeholder=str(modal.get("field1_placeholder") or "Leonardo"),
        )
        f2_label, f2_ph = _parse_modal_config_line(
            str(self.field2_input.value or ""),
            fallback_label=str(modal.get("field2_label") or "Idade e pronome"),
            fallback_placeholder=str(modal.get("field2_placeholder") or "17, ele"),
        )
        f3_label, f3_ph = _parse_modal_config_line(
            str(self.field3_input.value or ""),
            fallback_label=str(modal.get("field3_label") or "Descrição"),
            fallback_placeholder=str(modal.get("field3_placeholder") or "Não sei"),
        )
        await self.cog._update_modal_config(
            interaction,
            title=str(self.title_input.value or "").strip(),
            field1_label=f1_label,
            field1_placeholder=f1_ph,
            field2_label=f2_label,
            field2_placeholder=f2_ph,
            field3_label=f3_label,
            field3_placeholder=f3_ph,
        )


class ResponseEditModal(discord.ui.Modal):
    """Edita o cartão postado no canal de respostas da staff."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar resposta da staff")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        response = cfg.get("response") or {}

        self.title_input = discord.ui.TextInput(
            label="Título da resposta",
            default=_truncate(response.get("title") or "", RESPONSE_TITLE_MAX),
            style=discord.TextStyle.short,
            max_length=RESPONSE_TITLE_MAX,
            required=True,
        )
        self.intro_input = discord.ui.TextInput(
            label="Texto acima dos campos opcional",
            default=_truncate(response.get("intro") or "", RESPONSE_INTRO_MAX),
            style=discord.TextStyle.paragraph,
            max_length=RESPONSE_INTRO_MAX,
            required=False,
        )
        self.footer_input = discord.ui.TextInput(
            label="Rodapé opcional",
            default=_truncate(response.get("footer") or "", RESPONSE_FOOTER_MAX),
            style=discord.TextStyle.paragraph,
            max_length=RESPONSE_FOOTER_MAX,
            required=False,
        )
        self.media_url_input = discord.ui.TextInput(
            label="Imagem/GIF por URL opcional",
            default=_truncate(response.get("media_url") or "", MEDIA_URL_MAX),
            style=discord.TextStyle.paragraph,
            max_length=MEDIA_URL_MAX,
            required=False,
        )

        self.add_item(self.title_input)
        self.add_item(self.intro_input)
        self.add_item(self.footer_input)
        self.add_item(self.media_url_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._update_response_config(
            interaction,
            title=str(self.title_input.value or "").strip(),
            intro=str(self.intro_input.value or "").strip(),
            footer=str(self.footer_input.value or "").strip(),
            media_url=str(self.media_url_input.value or "").strip(),
        )


class ApprovalEditModal(discord.ui.Modal):
    """Edita texto/emoji dos botões e as DMs.

    Cores, liga/desliga e cargo ficam no ApprovalOptionsModal, usando
    os componentes modernos de modal do discord.py 2.7.
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar aprovação")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        approval = cfg.get("approval") or {}

        self.approve_button_input = discord.ui.TextInput(
            label="Aprovar: Texto | Emoji",
            default=_modal_config_line(approval.get("approve_label") or "Aprovar", approval.get("approve_emoji") or "✅"),
            max_length=FIELD_CONFIG_MAX,
            required=True,
        )
        self.reject_button_input = discord.ui.TextInput(
            label="Rejeitar: Texto | Emoji",
            default=_modal_config_line(approval.get("reject_label") or "Rejeitar", approval.get("reject_emoji") or "❌"),
            max_length=FIELD_CONFIG_MAX,
            required=True,
        )
        self.approve_dm_input = discord.ui.TextInput(
            label="DM ao aprovar",
            default=_truncate(approval.get("approve_dm") or "", REVIEW_DM_MAX),
            style=discord.TextStyle.paragraph,
            max_length=REVIEW_DM_MAX,
            required=True,
        )
        self.reject_dm_input = discord.ui.TextInput(
            label="DM ao rejeitar",
            default=_truncate(approval.get("reject_dm") or "", REVIEW_DM_MAX),
            style=discord.TextStyle.paragraph,
            max_length=REVIEW_DM_MAX,
            required=True,
        )

        self.add_item(self.approve_button_input)
        self.add_item(self.reject_button_input)
        self.add_item(self.approve_dm_input)
        self.add_item(self.reject_dm_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        approval = cfg.get("approval") or {}
        approve_label, approve_emoji = _parse_modal_config_line(
            str(self.approve_button_input.value or ""),
            fallback_label=str(approval.get("approve_label") or "Aprovar"),
            fallback_placeholder=str(approval.get("approve_emoji") or "✅"),
        )
        reject_label, reject_emoji = _parse_modal_config_line(
            str(self.reject_button_input.value or ""),
            fallback_label=str(approval.get("reject_label") or "Rejeitar"),
            fallback_placeholder=str(approval.get("reject_emoji") or "❌"),
        )
        await self.cog._update_approval_config(
            interaction,
            approve_label=approve_label,
            approve_emoji=approve_emoji,
            reject_label=reject_label,
            reject_emoji=reject_emoji,
            approve_dm=str(self.approve_dm_input.value or "").strip(),
            reject_dm=str(self.reject_dm_input.value or "").strip(),
        )


class ApprovalOptionsModal(discord.ui.Modal):
    """Opções rápidas usando RadioGroup, Checkbox e RoleSelect dentro do modal.

    Requer discord.py 2.7+, que já está definido no requirements do projeto.
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Opções do formulário")
        self.cog = cog
        self.guild_id = int(guild_id)

        if not all(hasattr(discord.ui, attr) for attr in ("Label", "Checkbox", "RadioGroup", "RoleSelect")):
            raise RuntimeError("discord.py 2.7+ é necessário para opções marcáveis em modal.")

        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}
        approval = cfg.get("approval") or {}
        role_id = int(approval.get("role_id") or 0)
        self.current_role_id = role_id

        self.enabled_checkbox = discord.ui.Checkbox(default=bool(approval.get("enabled", False)))
        self.panel_style_group = _make_style_radio_group(panel.get("button_style"), default="primary")
        self.approve_style_group = _make_style_radio_group(approval.get("approve_style"), default="success")
        self.reject_style_group = _make_style_radio_group(approval.get("reject_style"), default="danger")

        default_values = []
        placeholder = "Cargo dado ao aprovar (opcional)"
        guild = cog.bot.get_guild(int(guild_id)) if getattr(cog, "bot", None) is not None else None
        if role_id:
            role = guild.get_role(role_id) if guild is not None else None
            if role is not None:
                default_values = [role]
                placeholder = f"Atual: {role.name}"[:150]
            else:
                # Object implementa Snowflake e evita quebrar caso o cargo esteja fora do cache.
                default_values = [discord.Object(id=role_id)]
                placeholder = "Cargo atual salvo fora do cache"

        self.role_select = discord.ui.RoleSelect(
            placeholder=placeholder,
            min_values=0,
            max_values=1,
            required=False,
            default_values=default_values,
        )

        self.add_item(discord.ui.Label(
            text="Aprovação",
            description="Marcado = as respostas terão botões Aprovar/Rejeitar.",
            component=self.enabled_checkbox,
        ))
        self.add_item(discord.ui.Label(
            text="Cor do botão Preencher",
            component=self.panel_style_group,
        ))
        self.add_item(discord.ui.Label(
            text="Cor do botão Aprovar",
            component=self.approve_style_group,
        ))
        self.add_item(discord.ui.Label(
            text="Cor do botão Rejeitar",
            component=self.reject_style_group,
        ))
        self.add_item(discord.ui.Label(
            text="Cargo ao aprovar",
            description="Para remover o cargo salvo, use Limpar cargo no painel.",
            component=self.role_select,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        role_id = int(getattr(self, "current_role_id", 0) or 0)
        if getattr(self.role_select, "values", None):
            role_id = int(self.role_select.values[0].id)

        await self.cog._update_approval_options(
            interaction,
            enabled=bool(getattr(self.enabled_checkbox, "value", False)),
            role_id=role_id,
            panel_style=_selected_style(self.panel_style_group, "primary"),
            approve_style=_selected_style(self.approve_style_group, "success"),
            reject_style=_selected_style(self.reject_style_group, "danger"),
        )
