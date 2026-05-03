"""Modais da cog de formulários."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .constants import (
    BUTTON_EMOJI_MAX,
    BUTTON_LABEL_MAX,
    DEFAULT_PANEL,
    DEFAULT_RESPONSE,
    FIELD_CONFIG_MAX,
    FIELD_LENGTH_CONFIG_MAX,
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
from .fields import (
    DISCORD_TEXT_INPUT_MAX,
    default_form_fields,
    next_field_id,
    normalize_form_fields,
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
    # Lista única e fixa para evitar duplicação visual acidental.
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


_ACCENT_COLOR_PRESETS = {
    "default": ("Azul/Roxo", "#5865F2", "Cor padrão do Discord"),
    "gray": ("Cinza", "#747F8D", "Cor neutra"),
    "green": ("Verde", "#57F287", "Cor positiva"),
    "red": ("Vermelho", "#ED4245", "Cor de alerta"),
    "yellow": ("Amarelo", "#FEE75C", "Cor chamativa"),
    "pink": ("Rosa", "#EB459E", "Cor destacada"),
    "purple": ("Roxo", "#9B59B6", "Cor alternativa"),
    "custom": ("Personalizada", "custom", "Usa o HEX preenchido abaixo"),
}


def _clean_accent_hex(raw: str | int | None, *, fallback: str = "#5865F2") -> str | None:
    value = str(raw or "").strip()
    if not value:
        value = str(fallback or "#5865F2").strip()
    if value.startswith("#"):
        value = value[1:]
    elif value.lower().startswith("0x"):
        value = value[2:]
    if len(value) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in value):
        return f"#{value.upper()}"
    return None


def _accent_choice_for(current: str | int | None, *, fallback: str = "#5865F2") -> tuple[str, str]:
    hex_value = _clean_accent_hex(current, fallback=fallback) or _clean_accent_hex(fallback) or "#5865F2"
    for key, (_, preset_hex, _) in _ACCENT_COLOR_PRESETS.items():
        if key != "custom" and preset_hex.upper() == hex_value.upper():
            return key, hex_value
    return "custom", hex_value


def _make_accent_radio_group(current: str | int | None, *, fallback: str = "#5865F2"):
    current_key, _ = _accent_choice_for(current, fallback=fallback)
    group = discord.ui.RadioGroup(required=True)
    for key, (label, value, description) in _ACCENT_COLOR_PRESETS.items():
        shown_label = f"{label} ({value})" if value != "custom" else label
        group.add_option(
            label=shown_label,
            value=key,
            description=description,
            default=(key == current_key),
        )
    return group


def _selected_accent_hex(group, custom_value: str | int | None, *, fallback: str = "#5865F2") -> tuple[str | None, str | None]:
    value = str(getattr(group, "value", "") or "default")
    if value == "custom":
        hex_value = _clean_accent_hex(custom_value, fallback="")
        if not hex_value:
            return None, "Use um HEX válido, tipo #5865F2, 5865F2 ou 0x5865F2."
        return hex_value, None
    preset = _ACCENT_COLOR_PRESETS.get(value) or _ACCENT_COLOR_PRESETS["default"]
    return str(preset[1]), None


def _parse_lengths(raw: str, *, fallback_min: int = 0, fallback_max: int = FIELD_VALUE_SHORT_MAX) -> tuple[int, int]:
    raw = str(raw or "").strip()
    if not raw:
        return int(fallback_min or 0), int(fallback_max or FIELD_VALUE_SHORT_MAX)
    for sep in ("|", ",", ";", "/"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            break
    else:
        left, right = "0", raw
    try:
        min_len = int(str(left).strip() or 0)
    except ValueError:
        min_len = int(fallback_min or 0)
    try:
        max_len = int(str(right).strip() or fallback_max)
    except ValueError:
        max_len = int(fallback_max or FIELD_VALUE_SHORT_MAX)
    max_len = max(1, min(DISCORD_TEXT_INPUT_MAX, max_len))
    min_len = max(0, min(max_len, min_len))
    return min_len, max_len


def _flag_values(group) -> set[str]:
    values = getattr(group, "values", []) or []
    result: set[str] = set()
    for value in values:
        result.add(str(getattr(value, "value", value)))
    return result


class FormSubmissionModal(discord.ui.Modal):
    """Modal mostrado ao usuário ao clicar no botão do form."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        cfg = cog._get_config(guild_id)
        modal_cfg = cfg.get("modal") or {}
        title = _truncate(modal_cfg.get("title") or "Nova verificação", MODAL_TITLE_MAX)
        super().__init__(title=title)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.field_inputs: list[tuple[str, discord.ui.TextInput]] = []

        fields = normalize_form_fields(modal_cfg)
        for index, field in enumerate(fields):
            if not field.get("enabled", True):
                continue
            max_length = int(field.get("max_length") or (FIELD_VALUE_LONG_MAX if field.get("long") else FIELD_VALUE_SHORT_MAX))
            min_length = int(field.get("min_length") or 0)
            kwargs = {
                "label": _truncate(field.get("label") or f"Campo {index + 1}", 45),
                "placeholder": _truncate(field.get("placeholder") or "", 100),
                "style": discord.TextStyle.paragraph if field.get("long") else discord.TextStyle.short,
                "max_length": max(1, min(DISCORD_TEXT_INPUT_MAX, max_length)),
                "required": bool(field.get("required", True)),
            }
            if min_length > 0:
                kwargs["min_length"] = max(0, min(int(min_length), int(kwargs["max_length"])))
            text_input = discord.ui.TextInput(**kwargs)
            self.field_inputs.append((str(field.get("id") or f"field{index + 1}"), text_input))
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._handle_submission(
            interaction,
            field_values={field_id: str(text_input.value or "").strip() for field_id, text_input in self.field_inputs},
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
    """Edição legada: título + 3 primeiros campos.

    O botão principal agora abre o painel de gerenciamento de campos, mas esta
    classe fica preservada para compatibilidade com callbacks antigos.
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar modal de submissão")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        modal = cfg.get("modal") or {}
        fields = normalize_form_fields(modal)
        while len(fields) < 3:
            fields.append(default_form_fields()[len(fields)])

        self.title_input = discord.ui.TextInput(
            label="Título do modal",
            default=_truncate(modal.get("title") or "", MODAL_TITLE_MAX),
            max_length=MODAL_TITLE_MAX,
            required=True,
        )
        self.field_inputs: list[discord.ui.TextInput] = []
        for idx, field in enumerate(fields[:3], start=1):
            text_input = discord.ui.TextInput(
                label=f"Campo {idx}: Label | Placeholder",
                default=_modal_config_line(field.get("label") or f"Campo {idx}", field.get("placeholder") or ""),
                max_length=FIELD_CONFIG_MAX,
                required=True,
            )
            self.field_inputs.append(text_input)

        self.add_item(self.title_input)
        for text_input in self.field_inputs:
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        modal = cfg.get("modal") or {}
        fields = normalize_form_fields(modal)
        for idx, text_input in enumerate(self.field_inputs):
            if idx >= len(fields):
                fields.append(default_form_fields()[min(idx, 2)])
            label, placeholder = _parse_modal_config_line(
                str(text_input.value or ""),
                fallback_label=str(fields[idx].get("label") or f"Campo {idx + 1}"),
                fallback_placeholder=str(fields[idx].get("placeholder") or ""),
            )
            fields[idx]["label"] = label
            fields[idx]["placeholder"] = placeholder
            fields[idx]["response_label"] = label
        await self.cog._update_modal_title_and_fields(
            interaction,
            title=str(self.title_input.value or "").strip(),
            fields=fields,
            success_message="✅ Campos do modal atualizados.",
        )


class FieldEditModal(discord.ui.Modal):
    """Cria/edita um campo individual do formulário."""

    def __init__(self, cog: "FormsCog", guild_id: int, *, field_index: int | None = None, staff_id: int = 0):
        fields = cog._get_form_fields(guild_id)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id or 0)
        self.field_index = field_index if field_index is None else max(0, min(int(field_index), len(fields) - 1))
        self.initial_count = len(fields)
        is_new = self.field_index is None
        title = "Adicionar campo" if is_new else "Editar campo"
        super().__init__(title=title)

        if is_new:
            field = {
                "id": next_field_id(fields),
                "label": f"Campo {len(fields) + 1}",
                "placeholder": "",
                "response_label": f"Campo {len(fields) + 1}",
                "required": False,
                "long": False,
                "show_in_response": True,
                "enabled": True,
                "min_length": 0,
                "max_length": FIELD_VALUE_SHORT_MAX,
            }
        else:
            field = dict(fields[self.field_index])
        self.field_id = str(field.get("id") or next_field_id(fields))

        self.label_input = discord.ui.TextInput(
            label="Label do campo",
            default=_truncate(field.get("label") or "", 45),
            max_length=45,
            required=True,
        )
        self.placeholder_input = discord.ui.TextInput(
            label="Placeholder opcional",
            default=_truncate(field.get("placeholder") or "", 100),
            max_length=100,
            required=False,
        )
        self.response_label_input = discord.ui.TextInput(
            label="Nome na resposta da staff",
            default=_truncate(field.get("response_label") or field.get("label") or "", 45),
            max_length=45,
            required=True,
        )
        self.length_input = discord.ui.TextInput(
            label="Tamanho mínimo | máximo",
            default=f"{int(field.get('min_length') or 0)} | {int(field.get('max_length') or (FIELD_VALUE_LONG_MAX if field.get('long') else FIELD_VALUE_SHORT_MAX))}",
            max_length=FIELD_LENGTH_CONFIG_MAX,
            required=True,
        )

        if not hasattr(discord.ui, "CheckboxGroup"):
            raise RuntimeError("discord.py 2.7+ é necessário para marcar opções de campo no modal.")
        self.flags_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=3)
        self.flags_group.add_option(
            label="Campo obrigatório",
            value="required",
            description="Desmarcado = o usuário pode deixar vazio.",
            default=bool(field.get("required", True)),
        )
        self.flags_group.add_option(
            label="Mostrar na resposta da staff",
            value="show_in_response",
            description="Desmarcado = coleta, mas não mostra no canal da staff.",
            default=bool(field.get("show_in_response", True)),
        )
        self.flags_group.add_option(
            label="Resposta longa",
            value="long",
            description="Usa campo grande/parágrafo no formulário.",
            default=bool(field.get("long", False)),
        )

        self.add_item(self.label_input)
        self.add_item(self.placeholder_input)
        self.add_item(self.response_label_input)
        self.add_item(self.length_input)
        self.add_item(discord.ui.Label(
            text="Opções do campo",
            description="Marque/desmarque o comportamento desse campo.",
            component=self.flags_group,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        flags = _flag_values(self.flags_group)
        min_len, max_len = _parse_lengths(
            str(self.length_input.value or ""),
            fallback_min=0,
            fallback_max=FIELD_VALUE_LONG_MAX if "long" in flags else FIELD_VALUE_SHORT_MAX,
        )
        field = {
            "id": self.field_id,
            "label": str(self.label_input.value or "").strip(),
            "placeholder": str(self.placeholder_input.value or "").strip(),
            "response_label": str(self.response_label_input.value or "").strip(),
            "required": "required" in flags,
            "long": "long" in flags,
            "show_in_response": "show_in_response" in flags,
            "enabled": True,
            "min_length": min_len,
            "max_length": max_len,
        }
        selected_index = await self.cog._upsert_form_field(
            interaction,
            index=self.field_index,
            field=field,
            staff_id=self.staff_id or int(getattr(interaction.user, "id", 0) or 0),
        )
        from .views import FieldManagerView
        try:
            await interaction.followup.send(
                "✅ Campo salvo.",
                view=FieldManagerView(
                    self.cog,
                    guild_id=self.guild_id,
                    staff_id=self.staff_id or int(getattr(interaction.user, "id", 0) or 0),
                    selected_index=selected_index,
                ),
                ephemeral=True,
            )
        except discord.HTTPException:
            pass


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
    """Edita texto/emoji dos botões e as DMs."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Textos da aprovação")
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


class AccentColorsModal(discord.ui.Modal):
    """Edita a cor lateral dos Containers Components V2."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Cores do card")
        self.cog = cog
        self.guild_id = int(guild_id)

        if not all(hasattr(discord.ui, attr) for attr in ("Label", "RadioGroup")):
            raise RuntimeError("discord.py 2.7+ é necessário para cores por RadioGroup em modal.")

        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}
        response = cfg.get("response") or {}

        _, panel_hex = _accent_choice_for(panel.get("accent_color"), fallback=DEFAULT_PANEL.get("accent_color") or "#5865F2")
        _, response_hex = _accent_choice_for(response.get("accent_color"), fallback=DEFAULT_RESPONSE.get("accent_color") or "#5865F2")

        self.panel_group = _make_accent_radio_group(panel.get("accent_color"), fallback=DEFAULT_PANEL.get("accent_color") or "#5865F2")
        self.response_group = _make_accent_radio_group(response.get("accent_color"), fallback=DEFAULT_RESPONSE.get("accent_color") or "#5865F2")
        self.panel_custom_input = discord.ui.TextInput(
            label="HEX personalizado do formulário",
            default=panel_hex,
            placeholder="#5865F2",
            max_length=16,
            required=False,
        )
        self.response_custom_input = discord.ui.TextInput(
            label="HEX personalizado da resposta",
            default=response_hex,
            placeholder="#5865F2",
            max_length=16,
            required=False,
        )

        self.add_item(discord.ui.Label(
            text="Cor lateral do formulário",
            description="Muda a barrinha do card público.",
            component=self.panel_group,
        ))
        self.add_item(self.panel_custom_input)
        self.add_item(discord.ui.Label(
            text="Cor lateral da resposta",
            description="Muda a barrinha da mensagem enviada para a staff.",
            component=self.response_group,
        ))
        self.add_item(self.response_custom_input)

    async def on_submit(self, interaction: discord.Interaction):
        panel_hex, panel_error = _selected_accent_hex(
            self.panel_group,
            str(self.panel_custom_input.value or ""),
            fallback=DEFAULT_PANEL.get("accent_color") or "#5865F2",
        )
        response_hex, response_error = _selected_accent_hex(
            self.response_group,
            str(self.response_custom_input.value or ""),
            fallback=DEFAULT_RESPONSE.get("accent_color") or "#5865F2",
        )
        if panel_error or response_error or not panel_hex or not response_hex:
            await interaction.response.send_message(f"❌ {panel_error or response_error}", ephemeral=True)
            return

        await self.cog._update_accent_colors(
            interaction,
            panel_accent_color=panel_hex,
            response_accent_color=response_hex,
        )


class ApprovalOptionsModal(discord.ui.Modal):
    """Opções rápidas usando RadioGroup, Checkbox e RoleSelect dentro do modal."""

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
                default_values = [discord.Object(id=role_id)]
                placeholder = "Cargo atual salvo fora do cache"

        role_kwargs = {
            "placeholder": placeholder,
            "min_values": 0,
            "max_values": 1,
            "required": False,
        }
        if default_values:
            role_kwargs["default_values"] = default_values
        self.role_select = discord.ui.RoleSelect(**role_kwargs)

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
            description="Deixe vazio e envie para limpar o cargo salvo.",
            component=self.role_select,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        role_id = 0
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
