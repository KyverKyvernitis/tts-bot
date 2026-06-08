from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from .constants import DEFAULT_REPORT_TYPES, KIND_OTHER, KIND_REPORT
from .utils import clean_accent_hex, id_from_mention_or_text, normalize_report_types, parse_bool, truncate

if TYPE_CHECKING:
    from .cog import TicketsCog


_KIND_TO_PUBLIC_NAME = {
    "partnership": "parceria",
    "report": "denuncia",
    "suggestion": "sugestao",
    "other": "outros",
}
_PUBLIC_NAME_TO_KIND = {
    "parceria": "partnership",
    "denuncia": "report",
    "denúncia": "report",
    "sugestao": "suggestion",
    "sugestão": "suggestion",
    "outros": "other",
    "outro": "other",
}


def _input_value(input_obj) -> str:
    return str(getattr(input_obj, "value", "") or "").strip()


def _selected_values(select_obj: object) -> list[Any]:
    try:
        values = getattr(select_obj, "values", None)
        return list(values or [])
    except Exception:
        return []


def _selected_strings(select_obj: object) -> list[str]:
    return [str(value or "").strip() for value in _selected_values(select_obj) if str(value or "").strip()]


def _first_selected_id(select_obj: object) -> int:
    values = _selected_values(select_obj)
    if not values:
        return 0
    value = values[0]
    try:
        return int(getattr(value, "id", 0) or 0)
    except Exception:
        return id_from_mention_or_text(value)


def _selected_user_text(select_obj: object) -> str:
    values = _selected_values(select_obj)
    if not values:
        return ""
    user = values[0]
    uid = int(getattr(user, "id", 0) or 0)
    mention = str(getattr(user, "mention", "") or "").strip()
    if mention and uid:
        return f"{mention} (`{uid}`)"
    if uid:
        return f"{user} (`{uid}`)"
    return str(user or "").strip()


def _safe_clear_modal_items(modal: discord.ui.Modal) -> None:
    try:
        modal.clear_items()
        return
    except Exception:
        pass
    try:
        getattr(modal, "children", []).clear()
    except Exception:
        pass


def _label(text: str, component: discord.ui.Item, *, description: str | None = None) -> discord.ui.Item:
    label_cls = getattr(discord.ui, "Label", None)
    if label_cls is None:
        raise RuntimeError("discord.ui.Label indisponível")
    kwargs: dict[str, Any] = {
        "text": truncate(text, 45, suffix=""),
        "component": component,
    }
    if description:
        kwargs["description"] = truncate(description, 100, suffix="")
    return label_cls(**kwargs)


def _add_labeled(modal: discord.ui.Modal, text: str, component: discord.ui.Item, *, description: str | None = None) -> None:
    modal.add_item(_label(text, component, description=description))


def _checkbox_group_option(label: str, value: str, *, description: str | None = None, default: bool = False) -> object:
    option_cls = getattr(discord, "CheckboxGroupOption", None) or getattr(discord.ui, "CheckboxGroupOption", None)
    if option_cls is None:
        raise RuntimeError("CheckboxGroupOption indisponível")
    kwargs: dict[str, Any] = {
        "label": truncate(label, 100, suffix=""),
        "value": truncate(value, 100, suffix=""),
        "default": bool(default),
    }
    if description:
        kwargs["description"] = truncate(description, 100, suffix="")
    try:
        return option_cls(**kwargs)
    except TypeError:
        kwargs.pop("description", None)
        return option_cls(**kwargs)


def _guild_role(cog: "TicketsCog", guild_id: int, role_id: int) -> list[discord.Role]:
    guild = getattr(cog.bot, "get_guild", lambda _gid: None)(int(guild_id))
    if guild is None or not role_id:
        return []
    role = guild.get_role(int(role_id))
    return [role] if role is not None else []


def _guild_channel(cog: "TicketsCog", guild_id: int, channel_id: int) -> list[discord.abc.GuildChannel]:
    guild = getattr(cog.bot, "get_guild", lambda _gid: None)(int(guild_id))
    if guild is None or not channel_id:
        return []
    channel = guild.get_channel(int(channel_id))
    return [channel] if channel is not None else []


def _text_channel_types() -> list[discord.ChannelType]:
    types = [discord.ChannelType.text]
    for attr in ("news", "announcement_thread"):
        value = getattr(discord.ChannelType, attr, None)
        if value is not None and value not in types:
            types.append(value)
    return types


class SuggestionModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int):
        super().__init__(title="Enviar sugestão")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.title_input = discord.ui.TextInput(label="Título da sugestão", max_length=120, required=True)
        self.body_input = discord.ui.TextInput(label="Descrição da sugestão", style=discord.TextStyle.paragraph, max_length=1500, required=True)
        self.add_item(self.title_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._handle_suggestion_submission(
            interaction,
            title=_input_value(self.title_input),
            body=_input_value(self.body_input),
        )


class OtherTicketModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int):
        super().__init__(title="Abrir ticket")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.subject_input = discord.ui.TextInput(label="Assunto", max_length=120, required=True)
        self.body_input = discord.ui.TextInput(label="Explique o que você precisa", style=discord.TextStyle.paragraph, max_length=1500, required=True)
        self.add_item(self.subject_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._create_ticket_from_interaction(
            interaction,
            kind=KIND_OTHER,
            payload={"assunto": _input_value(self.subject_input), "descrição": _input_value(self.body_input)},
        )


class ReportTicketModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int):
        cfg = cog._get_config(guild_id)
        super().__init__(title="Enviar denúncia")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.uses_type_select = False
        self.uses_user_select = False
        report_types = normalize_report_types(cfg.get("report_types") or DEFAULT_REPORT_TYPES)
        notice = str((cfg.get("texts") or {}).get("report_modal_notice") or "Ao enviar, criaremos um ticket privado.")

        try:
            self.add_item(discord.ui.TextDisplay(truncate(notice, 350)))
        except Exception:
            # Builds antigas aceitam só campos de entrada no modal. O aviso também fica no placeholder fallback.
            pass

        try:
            self.kind_select = discord.ui.Select(
                placeholder="Escolha o tipo da denúncia",
                min_values=1,
                max_values=1,
                required=True,
                options=[
                    discord.SelectOption(
                        label=truncate(label, 100, suffix=""),
                        value=truncate(label, 100, suffix=""),
                    )
                    for label in report_types[:10]
                ],
                custom_id=f"tickets:report_type:{self.guild_id}",
            )
            _add_labeled(self, "Tipo da denúncia", self.kind_select, description="Escolha uma das opções configuradas.")
            self.uses_type_select = True
        except Exception:
            _safe_clear_modal_items(self)
            try:
                self.add_item(discord.ui.TextDisplay(truncate(notice, 350)))
            except Exception:
                pass
            choices = ", ".join(report_types[:10])
            self.kind_input = discord.ui.TextInput(
                label="Tipo da denúncia",
                placeholder=truncate(f"Escolha um: {choices}", 100),
                max_length=100,
                required=True,
            )
            self.add_item(self.kind_input)

        try:
            self.target_select = discord.ui.UserSelect(
                placeholder="Selecione o usuário denunciado",
                min_values=0,
                max_values=1,
                required=False,
                custom_id=f"tickets:report_user:{self.guild_id}",
            )
            _add_labeled(self, "Usuário denunciado", self.target_select, description="Opcional, se a pessoa estiver no servidor.")
            self.uses_user_select = True
        except Exception:
            self.target_input = discord.ui.TextInput(label="Usuário denunciado, se houver", max_length=120, required=False)
            self.add_item(self.target_input)

        self.body_input = discord.ui.TextInput(label="Descrição do ocorrido", style=discord.TextStyle.paragraph, max_length=1500, required=True)
        self.proofs_input = discord.ui.TextInput(label="Provas, links ou observações", style=discord.TextStyle.paragraph, max_length=1000, required=False)
        self.add_item(self.body_input)
        self.add_item(self.proofs_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.uses_type_select:
            values = _selected_strings(self.kind_select)
            report_type = values[0] if values else "Outro"
        else:
            report_type = _input_value(self.kind_input)
        target = _selected_user_text(self.target_select) if self.uses_user_select else _input_value(self.target_input)
        await self.cog._create_ticket_from_interaction(
            interaction,
            kind=KIND_REPORT,
            payload={
                "tipo": report_type or "Outro",
                "usuário denunciado": target,
                "descrição": _input_value(self.body_input),
                "provas/observações": _input_value(self.proofs_input),
            },
        )


class PanelEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(title="Editar painel")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}
        self.title_input = discord.ui.TextInput(label="Título", default=truncate(panel.get("title") or "", 200, suffix=""), max_length=200, required=True)
        self.desc_input = discord.ui.TextInput(label="Descrição", default=truncate(panel.get("description") or "", 1800, suffix=""), style=discord.TextStyle.paragraph, max_length=1800, required=True)
        self.placeholder_input = discord.ui.TextInput(label="Placeholder do select", default=truncate(panel.get("placeholder") or "", 100, suffix=""), max_length=100, required=True)
        self.color_input = discord.ui.TextInput(label="Cor HEX", default=clean_accent_hex(panel.get("accent_color")), max_length=16, required=True)
        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(self.placeholder_input)
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        cfg["panel"].update({
            "title": _input_value(self.title_input),
            "description": _input_value(self.desc_input),
            "placeholder": _input_value(self.placeholder_input),
            "accent_color": clean_accent_hex(_input_value(self.color_input)),
        })
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Painel salvo.")


class OptionsEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(title="Editar opções")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(guild_id)
        enabled = cfg.get("enabled") or {}
        options_cfg = cfg.get("options") or {}
        self.uses_v2 = False

        try:
            checkbox_group_cls = getattr(discord.ui, "CheckboxGroup")
            checkbox_cls = getattr(discord.ui, "Checkbox")
            self.enabled_group = checkbox_group_cls(
                custom_id=f"tickets:options_enabled:{self.guild_id}",
                min_values=0,
                max_values=4,
                required=False,
                options=[
                    _checkbox_group_option("🤝 Parceria", "partnership", default=bool(enabled.get("partnership", True))),
                    _checkbox_group_option("👾 Denúncia", "report", default=bool(enabled.get("report", True))),
                    _checkbox_group_option("⚡ Sugestão", "suggestion", default=bool(enabled.get("suggestion", True))),
                    _checkbox_group_option("⚙️ Outros", "other", default=bool(enabled.get("other", True))),
                ],
            )
            self.multiple_checkbox = checkbox_cls(
                custom_id=f"tickets:options_multiple:{self.guild_id}",
                default=bool(options_cfg.get("allow_multiple_open_tickets", False)),
            )
            self.transcript_checkbox = checkbox_cls(
                custom_id=f"tickets:options_transcript:{self.guild_id}",
                default=bool(options_cfg.get("transcript_on_close", True)),
            )
            _add_labeled(self, "Opções ativas", self.enabled_group, description="Marque o que aparece no painel público.")
            _add_labeled(self, "Permitir vários tickets", self.multiple_checkbox, description="Se desligado, cada usuário só mantém um ticket aberto.")
            _add_labeled(self, "Transcript ao fechar", self.transcript_checkbox, description="Gera arquivo de histórico ao fechar o ticket.")
            self.uses_v2 = True
        except Exception:
            _safe_clear_modal_items(self)
            active = ", ".join(
                public_name
                for public_name, kind in _PUBLIC_NAME_TO_KIND.items()
                if public_name in {"parceria", "denuncia", "sugestao", "outros"} and enabled.get(kind, True)
            )
            self.enabled_input = discord.ui.TextInput(label="Opções ativas", default=active, placeholder="parceria, denuncia, sugestao, outros", max_length=120, required=True)
            self.multiple_input = discord.ui.TextInput(label="Permitir múltiplos tickets?", default="sim" if options_cfg.get("allow_multiple_open_tickets") else "não", max_length=10, required=True)
            self.transcript_input = discord.ui.TextInput(label="Transcript ao fechar?", default="sim" if options_cfg.get("transcript_on_close") else "não", max_length=10, required=True)
            self.add_item(self.enabled_input)
            self.add_item(self.multiple_input)
            self.add_item(self.transcript_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        if self.uses_v2:
            enabled_values = set(_selected_strings(self.enabled_group))
            cfg["enabled"] = {
                "partnership": "partnership" in enabled_values,
                "report": "report" in enabled_values,
                "suggestion": "suggestion" in enabled_values,
                "other": "other" in enabled_values,
            }
            cfg["options"]["allow_multiple_open_tickets"] = bool(getattr(self.multiple_checkbox, "value", False))
            cfg["options"]["transcript_on_close"] = bool(getattr(self.transcript_checkbox, "value", False))
        else:
            raw = _input_value(self.enabled_input).lower()
            tokens = {token.strip() for token in raw.replace(";", ",").replace("/", ",").split(",") if token.strip()}
            enabled_values = set(_PUBLIC_NAME_TO_KIND.get(token, token) for token in tokens)
            cfg["enabled"] = {"partnership": "partnership" in enabled_values, "report": "report" in enabled_values, "suggestion": "suggestion" in enabled_values, "other": "other" in enabled_values}
            cfg["options"]["allow_multiple_open_tickets"] = parse_bool(_input_value(self.multiple_input), default=False)
            cfg["options"]["transcript_on_close"] = parse_bool(_input_value(self.transcript_input), default=True)
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Opções salvas.")


class ChannelsEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(title="Editar canais")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(guild_id)
        channels = cfg.get("channels") or {}
        self.uses_v2 = False

        try:
            channel_select_cls = getattr(discord.ui, "ChannelSelect")
            self.category_select = channel_select_cls(
                placeholder="Categoria onde os tickets serão criados",
                channel_types=[discord.ChannelType.category],
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_channel(cog, self.guild_id, int(channels.get("category_id") or 0)),
                custom_id=f"tickets:channels_category:{self.guild_id}",
            )
            self.logs_select = channel_select_cls(
                placeholder="Canal para logs dos tickets",
                channel_types=_text_channel_types(),
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_channel(cog, self.guild_id, int(channels.get("logs_channel_id") or 0)),
                custom_id=f"tickets:channels_logs:{self.guild_id}",
            )
            self.suggestions_select = channel_select_cls(
                placeholder="Canal que recebe sugestões",
                channel_types=_text_channel_types(),
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_channel(cog, self.guild_id, int(channels.get("suggestions_channel_id") or 0)),
                custom_id=f"tickets:channels_suggestions:{self.guild_id}",
            )
            _add_labeled(self, "Categoria dos tickets", self.category_select, description="Opcional. Se vazio, usa o canal atual/sem categoria.")
            _add_labeled(self, "Canal de logs", self.logs_select, description="Onde o bot registra criação, fechamento e transcripts.")
            _add_labeled(self, "Canal de sugestões", self.suggestions_select, description="Onde as sugestões enviadas serão publicadas.")
            self.uses_v2 = True
        except Exception:
            _safe_clear_modal_items(self)
            self.category_input = discord.ui.TextInput(label="ID da categoria dos tickets", default=str(channels.get("category_id") or ""), required=False, max_length=25)
            self.logs_input = discord.ui.TextInput(label="ID do canal de logs", default=str(channels.get("logs_channel_id") or ""), required=False, max_length=25)
            self.suggestions_input = discord.ui.TextInput(label="ID do canal de sugestões", default=str(channels.get("suggestions_channel_id") or ""), required=False, max_length=25)
            self.add_item(self.category_input)
            self.add_item(self.logs_input)
            self.add_item(self.suggestions_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        if self.uses_v2:
            cfg["channels"]["category_id"] = _first_selected_id(self.category_select)
            cfg["channels"]["logs_channel_id"] = _first_selected_id(self.logs_select)
            cfg["channels"]["suggestions_channel_id"] = _first_selected_id(self.suggestions_select)
        else:
            cfg["channels"]["category_id"] = id_from_mention_or_text(_input_value(self.category_input))
            cfg["channels"]["logs_channel_id"] = id_from_mention_or_text(_input_value(self.logs_input))
            cfg["channels"]["suggestions_channel_id"] = id_from_mention_or_text(_input_value(self.suggestions_input))
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Canais salvos.")


class RolesEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(title="Editar cargos")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(guild_id)
        roles = cfg.get("roles") or {}
        self.uses_v2 = False

        try:
            role_select_cls = getattr(discord.ui, "RoleSelect")
            self.staff_select = role_select_cls(
                placeholder="Cargo staff geral",
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_role(cog, self.guild_id, int(roles.get("staff_role_id") or 0)),
                custom_id=f"tickets:roles_staff:{self.guild_id}",
            )
            self.partnership_select = role_select_cls(
                placeholder="Cargo staff parceria",
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_role(cog, self.guild_id, int(roles.get("partnership_staff_role_id") or 0)),
                custom_id=f"tickets:roles_partnership:{self.guild_id}",
            )
            self.report_select = role_select_cls(
                placeholder="Cargo staff denúncia",
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_role(cog, self.guild_id, int(roles.get("report_staff_role_id") or 0)),
                custom_id=f"tickets:roles_report:{self.guild_id}",
            )
            self.other_select = role_select_cls(
                placeholder="Cargo staff outros",
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_role(cog, self.guild_id, int(roles.get("other_staff_role_id") or 0)),
                custom_id=f"tickets:roles_other:{self.guild_id}",
            )
            _add_labeled(self, "Staff geral", self.staff_select, description="Cargo com acesso geral aos tickets.")
            _add_labeled(self, "Staff parceria", self.partnership_select, description="Opcional. Substitui/reforça o cargo geral.")
            _add_labeled(self, "Staff denúncia", self.report_select, description="Opcional. Substitui/reforça o cargo geral.")
            _add_labeled(self, "Staff outros", self.other_select, description="Opcional. Substitui/reforça o cargo geral.")
            self.uses_v2 = True
        except Exception:
            _safe_clear_modal_items(self)
            self.staff_input = discord.ui.TextInput(label="ID cargo staff geral", default=str(roles.get("staff_role_id") or ""), required=False, max_length=25)
            self.partnership_input = discord.ui.TextInput(label="ID cargo staff parceria", default=str(roles.get("partnership_staff_role_id") or ""), required=False, max_length=25)
            self.report_input = discord.ui.TextInput(label="ID cargo staff denúncia", default=str(roles.get("report_staff_role_id") or ""), required=False, max_length=25)
            self.other_input = discord.ui.TextInput(label="ID cargo staff outros", default=str(roles.get("other_staff_role_id") or ""), required=False, max_length=25)
            self.add_item(self.staff_input)
            self.add_item(self.partnership_input)
            self.add_item(self.report_input)
            self.add_item(self.other_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        if self.uses_v2:
            cfg["roles"]["staff_role_id"] = _first_selected_id(self.staff_select)
            cfg["roles"]["partnership_staff_role_id"] = _first_selected_id(self.partnership_select)
            cfg["roles"]["report_staff_role_id"] = _first_selected_id(self.report_select)
            cfg["roles"]["other_staff_role_id"] = _first_selected_id(self.other_select)
        else:
            cfg["roles"]["staff_role_id"] = id_from_mention_or_text(_input_value(self.staff_input))
            cfg["roles"]["partnership_staff_role_id"] = id_from_mention_or_text(_input_value(self.partnership_input))
            cfg["roles"]["report_staff_role_id"] = id_from_mention_or_text(_input_value(self.report_input))
            cfg["roles"]["other_staff_role_id"] = id_from_mention_or_text(_input_value(self.other_input))
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Cargos salvos.")


class ReportTypesEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(title="Tipos de denúncia")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(guild_id)
        current = "\n".join(normalize_report_types(cfg.get("report_types") or DEFAULT_REPORT_TYPES))
        self.types_input = discord.ui.TextInput(label="Até 10 tipos, um por linha", default=truncate(current, 900, suffix=""), style=discord.TextStyle.paragraph, max_length=900, required=True)
        self.add_item(self.types_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        cfg["report_types"] = normalize_report_types(_input_value(self.types_input))
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Tipos de denúncia salvos.")


class TextsEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(title="Editar textos")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(guild_id)
        texts = cfg.get("texts") or {}
        self.partner_confirm = discord.ui.TextInput(label="Confirmação de parceria", default=truncate(texts.get("partnership_confirm") or "", 500, suffix=""), style=discord.TextStyle.paragraph, max_length=500, required=True)
        self.partner_open = discord.ui.TextInput(label="Abertura do ticket parceria", default=truncate(texts.get("partnership_opening") or "", 500, suffix=""), style=discord.TextStyle.paragraph, max_length=500, required=True)
        self.report_notice = discord.ui.TextInput(label="Aviso no modal denúncia", default=truncate(texts.get("report_modal_notice") or "", 500, suffix=""), style=discord.TextStyle.paragraph, max_length=500, required=True)
        self.report_open = discord.ui.TextInput(label="Abertura do ticket denúncia", default=truncate(texts.get("report_opening") or "", 500, suffix=""), style=discord.TextStyle.paragraph, max_length=500, required=True)
        self.other_open = discord.ui.TextInput(label="Abertura do ticket outros", default=truncate(texts.get("other_opening") or "", 500, suffix=""), style=discord.TextStyle.paragraph, max_length=500, required=True)
        self.add_item(self.partner_confirm)
        self.add_item(self.partner_open)
        self.add_item(self.report_notice)
        self.add_item(self.report_open)
        self.add_item(self.other_open)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        cfg["texts"].update({
            "partnership_confirm": _input_value(self.partner_confirm),
            "partnership_opening": _input_value(self.partner_open),
            "report_modal_notice": _input_value(self.report_notice),
            "report_opening": _input_value(self.report_open),
            "other_opening": _input_value(self.other_open),
        })
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Textos salvos.")
