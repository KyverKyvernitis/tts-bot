from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from .constants import (
    DEFAULT_REPORT_TYPES,
    FLOW_CONFIRM_TICKET,
    FLOW_DIRECT_TICKET,
    FLOW_MODAL_CHANNEL,
    FLOW_MODAL_TICKET,
    KIND_OTHER,
    KIND_REPORT,
    OPTION_FLOWS,
    PUBLIC_OPTIONS,
    TICKET_KINDS,
)
from .permissions import (
    TICKET_PERMISSION_LABELS,
    SCOPE_LABELS,
    default_permissions_config,
    reset_permissions,
    scope_permissions,
    set_scope_permissions,
)
from .utils import (
    clean_accent_hex,
    clean_option_emoji,
    clean_panel_image_url,
    get_ticket_option,
    id_from_mention_or_text,
    iter_ticket_options,
    normalize_report_types,
    parse_bool,
    truncate,
)

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
    def __init__(self, cog: "TicketsCog", guild_id: int, option_id: str = KIND_REPORT):
        cfg = cog._get_config(guild_id)
        item = get_ticket_option(cfg, option_id) or get_ticket_option(cfg, KIND_REPORT) or {}
        super().__init__(title=truncate(item.get("modal_title") or "Enviar denúncia", 45, suffix=""))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.option_id = str(option_id or KIND_REPORT)
        self.uses_type_select = False
        self.uses_user_select = False
        report_types = normalize_report_types(cfg.get("report_types") or DEFAULT_REPORT_TYPES)
        notice = str(item.get("modal_notice") or (cfg.get("texts") or {}).get("report_modal_notice") or "Ao enviar, criaremos um ticket privado.")

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
            kind=self.option_id,
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
        self.image_input = discord.ui.TextInput(
            label="URL da imagem do painel",
            default=truncate(panel.get("image_url") or "", 500, suffix=""),
            placeholder="https://exemplo.com/imagem.png — opcional",
            max_length=500,
            required=False,
        )
        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(self.placeholder_input)
        self.add_item(self.color_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_image_url = _input_value(self.image_input)
        image_url = clean_panel_image_url(raw_image_url)
        if raw_image_url and not image_url:
            await interaction.response.send_message("URL da imagem inválida. Use um link começando com `http://` ou `https://`, ou deixe vazio para remover.", ephemeral=True)
            return
        cfg = self.cog._get_config(self.guild_id)
        cfg["panel"].update({
            "title": _input_value(self.title_input),
            "description": _input_value(self.desc_input),
            "placeholder": _input_value(self.placeholder_input),
            "accent_color": clean_accent_hex(_input_value(self.color_input)),
            "image_url": image_url,
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
        options_cfg = cfg.get("options") or {}
        self.uses_v2 = False

        ticket_options = iter_ticket_options(cfg, include_disabled=True)
        try:
            checkbox_group_cls = getattr(discord.ui, "CheckboxGroup")
            checkbox_cls = getattr(discord.ui, "Checkbox")
            group_options = []
            for item in ticket_options[:19]:
                option_id = str(item.get("id") or "")
                if not option_id:
                    continue
                label = f"{item.get('emoji') or '🎫'} {item.get('label') or option_id}"
                group_options.append(_checkbox_group_option(label, option_id, default=bool(item.get("enabled", True))))
            self.enabled_group = checkbox_group_cls(
                custom_id=f"tickets:options_enabled:{self.guild_id}",
                min_values=0,
                max_values=max(1, len(group_options)),
                required=False,
                options=group_options,
            )
            self.multiple_checkbox = checkbox_cls(
                custom_id=f"tickets:options_multiple:{self.guild_id}",
                default=bool(options_cfg.get("allow_multiple_open_tickets", False)),
            )
            self.transcript_checkbox = checkbox_cls(
                custom_id=f"tickets:options_transcript:{self.guild_id}",
                default=bool(options_cfg.get("transcript_on_close", True)),
            )
            self.webhook_checkbox = checkbox_cls(
                custom_id=f"tickets:options_webhook:{self.guild_id}",
                default=bool(options_cfg.get("use_server_webhook", False)),
            )
            _add_labeled(self, "Opções ativas", self.enabled_group, description="Marque o que aparece no painel público.")
            _add_labeled(self, "Permitir vários tickets", self.multiple_checkbox, description="Se desligado, cada usuário só mantém um ticket aberto.")
            _add_labeled(self, "Transcript ao fechar", self.transcript_checkbox, description="Gera arquivo de histórico ao fechar o ticket.")
            _add_labeled(self, "Usar webhook do servidor", self.webhook_checkbox, description="Mensagens visuais usam nome e foto do servidor quando possível.")
            self.uses_v2 = True
        except Exception:
            _safe_clear_modal_items(self)
            active = ", ".join(
                str(item.get("id"))
                for item in ticket_options
                if bool(item.get("enabled", True))
            )
            self.enabled_input = discord.ui.TextInput(label="Opções ativas", default=active, placeholder="partnership, report, suggestion, other", max_length=500, required=True)
            self.multiple_input = discord.ui.TextInput(label="Permitir múltiplos tickets?", default="sim" if options_cfg.get("allow_multiple_open_tickets") else "não", max_length=10, required=True)
            self.transcript_input = discord.ui.TextInput(label="Transcript ao fechar?", default="sim" if options_cfg.get("transcript_on_close") else "não", max_length=10, required=True)
            self.webhook_input = discord.ui.TextInput(label="Usar webhook do servidor?", default="sim" if options_cfg.get("use_server_webhook") else "não", max_length=10, required=True)
            self.add_item(self.enabled_input)
            self.add_item(self.multiple_input)
            self.add_item(self.transcript_input)
            self.add_item(self.webhook_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        created_label = ""
        if self.uses_v2:
            enabled_values = set(_selected_strings(self.enabled_group))
            items = cfg.get("option_items") if isinstance(cfg.get("option_items"), dict) else {}
            for option_id, item in items.items():
                if isinstance(item, dict):
                    item["enabled"] = option_id in enabled_values
            cfg["enabled"] = {kind: bool((items.get(kind) or {}).get("enabled", False)) for kind in TICKET_KINDS}
            cfg["options"]["allow_multiple_open_tickets"] = bool(getattr(self.multiple_checkbox, "value", False))
            cfg["options"]["transcript_on_close"] = bool(getattr(self.transcript_checkbox, "value", False))
            cfg["options"]["use_server_webhook"] = bool(getattr(self.webhook_checkbox, "value", False))
        else:
            raw = _input_value(self.enabled_input).lower()
            tokens = {token.strip() for token in raw.replace(";", ",").replace("/", ",").split(",") if token.strip()}
            enabled_values = set(_PUBLIC_NAME_TO_KIND.get(token, token) for token in tokens)
            items = cfg.get("option_items") if isinstance(cfg.get("option_items"), dict) else {}
            for option_id, item in items.items():
                if isinstance(item, dict):
                    item["enabled"] = option_id in enabled_values
            cfg["enabled"] = {kind: bool((items.get(kind) or {}).get("enabled", False)) for kind in TICKET_KINDS}
            cfg["options"]["allow_multiple_open_tickets"] = parse_bool(_input_value(self.multiple_input), default=False)
            cfg["options"]["transcript_on_close"] = parse_bool(_input_value(self.transcript_input), default=True)
            cfg["options"]["use_server_webhook"] = parse_bool(_input_value(self.webhook_input), default=False)
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, f"Opções salvas.{created_label}")


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


class PermissionsEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int, scope: str):
        labels = {"everyone": "@everyone", "staff": "staff", "creator": "autor"}
        super().__init__(title=f"Permissões: {labels.get(scope, scope)}")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.scope = scope if scope in {"everyone", "staff", "creator"} else "creator"
        cfg = cog._get_config(guild_id)
        current = scope_permissions(cfg, self.scope)
        defaults = default_permissions_config().get(self.scope, {})
        self.uses_v2 = False

        try:
            checkbox_group_cls = getattr(discord.ui, "CheckboxGroup")
            options = []
            for key in defaults:
                label = TICKET_PERMISSION_LABELS.get(key, key.replace("_", " "))
                options.append(_checkbox_group_option(label, key, default=bool(current.get(key, False))))
            self.permissions_group = checkbox_group_cls(
                custom_id=f"tickets:permissions:{self.guild_id}:{self.scope}",
                min_values=0,
                max_values=max(1, len(options)),
                required=False,
                options=options,
            )
            description = "Marque as permissões liberadas neste grupo."
            if self.scope == "everyone":
                description = "Cuidado: marcar Ver canal pode tornar tickets públicos."
            _add_labeled(self, SCOPE_LABELS.get(self.scope, self.scope), self.permissions_group, description=description)
            self.uses_v2 = True
        except Exception:
            _safe_clear_modal_items(self)
            enabled = ", ".join(key for key, value in current.items() if value)
            allowed = ", ".join(defaults.keys())
            self.permissions_input = discord.ui.TextInput(
                label="Permissões liberadas",
                default=truncate(enabled, 900, suffix=""),
                placeholder=truncate(allowed, 100, suffix=""),
                style=discord.TextStyle.paragraph,
                max_length=900,
                required=False,
            )
            self.add_item(self.permissions_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        defaults = default_permissions_config().get(self.scope, {})
        if self.uses_v2:
            selected = set(_selected_strings(self.permissions_group))
        else:
            raw = _input_value(self.permissions_input).lower()
            selected = {token.strip() for token in raw.replace(";", ",").replace("\n", ",").split(",") if token.strip()}
        values = {key: key in selected for key in defaults}
        set_scope_permissions(cfg, self.scope, values)
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(
            interaction,
            self.guild_id,
            self.staff_id,
            f"Permissões de {SCOPE_LABELS.get(self.scope, self.scope)} salvas.",
        )




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


_FLOW_INPUT_HELP = "confirm_ticket, modal_ticket, modal_channel ou direct_ticket"


def _normalize_flow_input(raw: object, *, fallback: str = FLOW_MODAL_TICKET) -> str:
    text = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "confirmar": FLOW_CONFIRM_TICKET,
        "confirmar_ticket": FLOW_CONFIRM_TICKET,
        "confirm_ticket": FLOW_CONFIRM_TICKET,
        "modal": FLOW_MODAL_TICKET,
        "modal_ticket": FLOW_MODAL_TICKET,
        "ticket_modal": FLOW_MODAL_TICKET,
        "canal": FLOW_MODAL_CHANNEL,
        "modal_channel": FLOW_MODAL_CHANNEL,
        "sugestao": FLOW_MODAL_CHANNEL,
        "sugestão": FLOW_MODAL_CHANNEL,
        "direto": FLOW_DIRECT_TICKET,
        "direct": FLOW_DIRECT_TICKET,
        "direct_ticket": FLOW_DIRECT_TICKET,
    }
    return aliases.get(text, text if text in OPTION_FLOWS else fallback)


def _flow_label(flow: object) -> str:
    labels = {
        FLOW_CONFIRM_TICKET: "Confirmar e criar ticket",
        FLOW_MODAL_TICKET: "Modal e criar ticket",
        FLOW_MODAL_CHANNEL: "Modal e enviar para canal",
        FLOW_DIRECT_TICKET: "Criar ticket direto",
    }
    return labels.get(str(flow or ""), "Modal e criar ticket")


def _flow_select_options(current: str) -> list[discord.SelectOption]:
    definitions = [
        (FLOW_CONFIRM_TICKET, "Confirmar e criar ticket", "Mostra confirmação antes de criar o canal.", "✅"),
        (FLOW_MODAL_TICKET, "Modal e criar ticket", "Abre modal e cria um ticket depois do envio.", "📝"),
        (FLOW_MODAL_CHANNEL, "Modal e enviar para canal", "Abre modal e envia a resposta em um canal.", "📨"),
        (FLOW_DIRECT_TICKET, "Criar ticket direto", "Cria o canal privado imediatamente.", "🎫"),
    ]
    options: list[discord.SelectOption] = []
    for value, label, description, emoji in definitions:
        kwargs = dict(label=label, value=value, description=description, emoji=emoji)
        try:
            kwargs["default"] = value == current
            options.append(discord.SelectOption(**kwargs))
        except TypeError:
            kwargs.pop("default", None)
            options.append(discord.SelectOption(**kwargs))
    return options


class TicketOptionEditModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int, option_id: str):
        cfg = cog._get_config(guild_id)
        item = get_ticket_option(cfg, option_id) or {}
        super().__init__(title=truncate(f"Editar opção: {item.get('label') or option_id}", 45, suffix=""))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.option_id = str(option_id)
        self.uses_v2 = False
        current_flow = str(item.get("flow") or FLOW_MODAL_TICKET)

        try:
            self.label_input = discord.ui.TextInput(
                label="Nome da opção",
                default=truncate(item.get("label") or "Nova opção", 80, suffix=""),
                max_length=80,
                required=True,
            )
            self.emoji_input = discord.ui.TextInput(
                label="Emoji",
                default=truncate(item.get("emoji") or "🎫", 32, suffix=""),
                max_length=32,
                required=True,
            )
            self.description_input = discord.ui.TextInput(
                label="Descrição no select",
                default=truncate(item.get("description") or "Abrir atendimento.", 100, suffix=""),
                max_length=100,
                required=True,
            )
            self.flow_select = discord.ui.Select(
                placeholder="Escolha o fluxo desta opção",
                min_values=1,
                max_values=1,
                required=True,
                options=_flow_select_options(current_flow),
                custom_id=f"tickets:option_flow:{self.guild_id}:{self.option_id}",
            )
            channel_select_cls = getattr(discord.ui, "ChannelSelect")
            self.target_channel_select = channel_select_cls(
                placeholder="Canal destino, se o fluxo enviar para canal",
                channel_types=_text_channel_types(),
                min_values=0,
                max_values=1,
                required=False,
                default_values=_guild_channel(cog, self.guild_id, int(item.get("target_channel_id") or 0)),
                custom_id=f"tickets:option_target_channel:{self.guild_id}:{self.option_id}",
            )
            self.add_item(self.label_input)
            self.add_item(self.emoji_input)
            self.add_item(self.description_input)
            _add_labeled(self, "Fluxo", self.flow_select, description="Escolha como essa opção se comporta no painel público.")
            _add_labeled(self, "Canal destino", self.target_channel_select, description="Usado apenas no fluxo que envia a resposta para canal.")
            self.uses_v2 = True
        except Exception:
            _safe_clear_modal_items(self)
            self.label_input = discord.ui.TextInput(
                label="Nome da opção",
                default=truncate(item.get("label") or "Nova opção", 80, suffix=""),
                max_length=80,
                required=True,
            )
            self.emoji_input = discord.ui.TextInput(
                label="Emoji",
                default=truncate(item.get("emoji") or "🎫", 32, suffix=""),
                max_length=32,
                required=True,
            )
            self.description_input = discord.ui.TextInput(
                label="Descrição no select",
                default=truncate(item.get("description") or "Abrir atendimento.", 100, suffix=""),
                max_length=100,
                required=True,
            )
            self.flow_input = discord.ui.TextInput(
                label="Fluxo",
                default=current_flow,
                placeholder=_FLOW_INPUT_HELP,
                max_length=32,
                required=True,
            )
            self.channel_input = discord.ui.TextInput(
                label="Canal destino, se enviar para canal",
                default=str(item.get("target_channel_id") or ""),
                placeholder="ID ou menção; vazio usa canal de sugestões",
                max_length=40,
                required=False,
            )
            self.add_item(self.label_input)
            self.add_item(self.emoji_input)
            self.add_item(self.description_input)
            self.add_item(self.flow_input)
            self.add_item(self.channel_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        items = cfg.get("option_items") if isinstance(cfg.get("option_items"), dict) else {}
        item = items.get(self.option_id)
        if not isinstance(item, dict):
            await interaction.response.send_message("Opção não encontrada. Abra o editor novamente.", ephemeral=True)
            return
        item["label"] = truncate(_input_value(self.label_input), 80, suffix="") or "Nova opção"
        item["emoji"] = clean_option_emoji(_input_value(self.emoji_input), fallback="🎫")
        item["description"] = truncate(_input_value(self.description_input), 100, suffix="") or "Abrir atendimento."
        if self.uses_v2:
            selected_flow = (_selected_strings(self.flow_select) or [str(item.get("flow") or FLOW_MODAL_TICKET)])[0]
            item["flow"] = _normalize_flow_input(selected_flow, fallback=str(item.get("flow") or FLOW_MODAL_TICKET))
            item["target_channel_id"] = _first_selected_id(self.target_channel_select)
        else:
            item["flow"] = _normalize_flow_input(_input_value(self.flow_input), fallback=str(item.get("flow") or FLOW_MODAL_TICKET))
            item["target_channel_id"] = id_from_mention_or_text(_input_value(self.channel_input))
        cfg["enabled"] = {kind: bool((items.get(kind) or {}).get("enabled", False)) for kind in TICKET_KINDS}
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(
            interaction,
            self.guild_id,
            self.staff_id,
            f"Opção salva: {item.get('emoji') or '🎫'} {item.get('label') or self.option_id} · {_flow_label(item.get('flow'))}."
        )


class SingleTicketTextModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int, option_id: str, text_key: str):
        cfg = cog._get_config(guild_id)
        item = get_ticket_option(cfg, option_id) or {}
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.option_id = str(option_id)
        self.text_key = str(text_key)
        labels = {
            "confirmation_text": "Confirmação",
            "opening_text": "Abertura/envio",
            "modal_notice": "Aviso do modal",
            "modal_title": "Título do modal",
            "subject_label": "Campo assunto",
            "body_label": "Campo descrição",
            "close_notice": "Fechamento",
        }
        title = labels.get(self.text_key, self.text_key)
        if self.text_key == "close_notice":
            current = str((cfg.get("texts") or {}).get("close_notice") or "Este ticket será fechado em alguns segundos.")
        else:
            current = str(item.get(self.text_key) or "")
        super().__init__(title=truncate(f"Texto: {title}", 45, suffix=""))
        max_len = 1800 if self.text_key in {"confirmation_text", "opening_text", "modal_notice", "close_notice"} else 45
        style = discord.TextStyle.paragraph if max_len > 100 else discord.TextStyle.short
        self.text_input = discord.ui.TextInput(
            label=truncate(title, 45, suffix=""),
            default=truncate(current, max_len, suffix=""),
            style=style,
            max_length=max_len,
            required=False if self.text_key in {"modal_notice"} else True,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        value = _input_value(self.text_input)
        if self.text_key == "close_notice":
            cfg.setdefault("texts", {})["close_notice"] = truncate(value, 1800, suffix="") or "Este ticket será fechado em alguns segundos."
        else:
            items = cfg.get("option_items") if isinstance(cfg.get("option_items"), dict) else {}
            item = items.get(self.option_id)
            if not isinstance(item, dict):
                await interaction.response.send_message("Opção não encontrada. Abra o editor novamente.", ephemeral=True)
                return
            if self.text_key in {"modal_title", "subject_label", "body_label"}:
                item[self.text_key] = truncate(value, 45, suffix="") or item.get(self.text_key) or "Texto"
            else:
                item[self.text_key] = truncate(value, 1800, suffix="")
            # Mantém compatibilidade com os textos antigos para exports/backups.
            if self.option_id == "partnership":
                if self.text_key == "confirmation_text":
                    cfg.setdefault("texts", {})["partnership_confirm"] = item[self.text_key]
                elif self.text_key == "opening_text":
                    cfg.setdefault("texts", {})["partnership_opening"] = item[self.text_key]
            elif self.option_id == "report":
                if self.text_key == "modal_notice":
                    cfg.setdefault("texts", {})["report_modal_notice"] = item[self.text_key]
                elif self.text_key == "opening_text":
                    cfg.setdefault("texts", {})["report_opening"] = item[self.text_key]
            elif self.option_id == "other" and self.text_key == "opening_text":
                cfg.setdefault("texts", {})["other_opening"] = item[self.text_key]
            elif self.option_id == "suggestion" and self.text_key == "opening_text":
                cfg.setdefault("texts", {})["suggestion_published"] = item[self.text_key]
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Texto salvo.")


class GenericTicketModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, option_id: str):
        cfg = cog._get_config(guild_id)
        item = get_ticket_option(cfg, option_id) or {}
        super().__init__(title=truncate(item.get("modal_title") or item.get("label") or "Abrir ticket", 45, suffix=""))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.option_id = str(option_id)
        notice = str(item.get("modal_notice") or "")
        if notice:
            try:
                self.add_item(discord.ui.TextDisplay(truncate(notice, 350)))
            except Exception:
                pass
        self.subject_input = discord.ui.TextInput(
            label=truncate(item.get("subject_label") or "Assunto", 45, suffix=""),
            max_length=120,
            required=True,
        )
        self.body_input = discord.ui.TextInput(
            label=truncate(item.get("body_label") or "Explique o que você precisa", 45, suffix=""),
            style=discord.TextStyle.paragraph,
            max_length=1500,
            required=True,
        )
        self.add_item(self.subject_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._create_ticket_from_interaction(
            interaction,
            kind=self.option_id,
            payload={"assunto": _input_value(self.subject_input), "descrição": _input_value(self.body_input)},
        )


class GenericChannelModal(discord.ui.Modal):
    def __init__(self, cog: "TicketsCog", guild_id: int, option_id: str):
        cfg = cog._get_config(guild_id)
        item = get_ticket_option(cfg, option_id) or {}
        super().__init__(title=truncate(item.get("modal_title") or item.get("label") or "Enviar mensagem", 45, suffix=""))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.option_id = str(option_id)
        notice = str(item.get("modal_notice") or "")
        if notice:
            try:
                self.add_item(discord.ui.TextDisplay(truncate(notice, 350)))
            except Exception:
                pass
        self.title_input = discord.ui.TextInput(label=truncate(item.get("subject_label") or "Título", 45, suffix=""), max_length=120, required=True)
        self.body_input = discord.ui.TextInput(label=truncate(item.get("body_label") or "Descrição", 45, suffix=""), style=discord.TextStyle.paragraph, max_length=1500, required=True)
        self.add_item(self.title_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._handle_channel_option_submission(
            interaction,
            option_id=self.option_id,
            title=_input_value(self.title_input),
            body=_input_value(self.body_input),
        )
