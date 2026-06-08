from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .constants import DEFAULT_REPORT_TYPES, KIND_OTHER, KIND_REPORT
from .utils import clean_accent_hex, id_from_mention_or_text, normalize_report_types, parse_bool, truncate

if TYPE_CHECKING:
    from .cog import TicketsCog


def _input_value(input_obj) -> str:
    return str(getattr(input_obj, "value", "") or "").strip()


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
        self.uses_select = False
        report_types = normalize_report_types(cfg.get("report_types") or DEFAULT_REPORT_TYPES)
        notice = str((cfg.get("texts") or {}).get("report_modal_notice") or "Ao enviar, criaremos um ticket privado.")

        try:
            self.add_item(discord.ui.TextDisplay(truncate(notice, 350)))
        except Exception:
            # Algumas builds antigas aceitam só inputs no modal. O aviso também fica no placeholder abaixo.
            pass

        self.kind_select = discord.ui.Select(
            placeholder="Tipo da denúncia",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=truncate(label, 100, suffix=""), value=truncate(label, 100, suffix="")) for label in report_types[:10]],
            custom_id=f"tickets:report_type:{self.guild_id}",
        )
        try:
            self.add_item(self.kind_select)
            self.uses_select = True
        except Exception:
            self.uses_select = False
            choices = ", ".join(report_types[:10])
            self.kind_input = discord.ui.TextInput(
                label="Tipo da denúncia",
                placeholder=truncate(f"Escolha um: {choices}", 100),
                max_length=100,
                required=True,
            )
            self.add_item(self.kind_input)

        self.target_input = discord.ui.TextInput(label="Usuário denunciado, se houver", max_length=120, required=False)
        self.body_input = discord.ui.TextInput(label="Descrição do ocorrido", style=discord.TextStyle.paragraph, max_length=1500, required=True)
        self.proofs_input = discord.ui.TextInput(label="Provas, links ou observações", style=discord.TextStyle.paragraph, max_length=1000, required=False)
        self.add_item(self.target_input)
        self.add_item(self.body_input)
        self.add_item(self.proofs_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.uses_select:
            values = getattr(self.kind_select, "values", []) or []
            report_type = str(values[0]) if values else "Outro"
        else:
            report_type = _input_value(self.kind_input)
        await self.cog._create_ticket_from_interaction(
            interaction,
            kind=KIND_REPORT,
            payload={
                "tipo": report_type or "Outro",
                "usuário denunciado": _input_value(self.target_input),
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
        options = cfg.get("options") or {}
        active = ", ".join(k for k in ("parceria", "denuncia", "sugestao", "outros") if enabled.get({"parceria":"partnership", "denuncia":"report", "sugestao":"suggestion", "outros":"other"}[k], True))
        self.enabled_input = discord.ui.TextInput(label="Opções ativas", default=active, placeholder="parceria, denuncia, sugestao, outros", max_length=120, required=True)
        self.multiple_input = discord.ui.TextInput(label="Permitir múltiplos tickets?", default="sim" if options.get("allow_multiple_open_tickets") else "não", max_length=10, required=True)
        self.transcript_input = discord.ui.TextInput(label="Transcript ao fechar?", default="sim" if options.get("transcript_on_close") else "não", max_length=10, required=True)
        self.add_item(self.enabled_input)
        self.add_item(self.multiple_input)
        self.add_item(self.transcript_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        raw = _input_value(self.enabled_input).lower()
        tokens = {token.strip() for token in raw.replace(";", ",").replace("/", ",").split(",") if token.strip()}
        mapping = {"parceria": "partnership", "denuncia": "report", "denúncia": "report", "sugestao": "suggestion", "sugestão": "suggestion", "outros": "other", "outro": "other"}
        enabled_values = set(mapping.get(token, token) for token in tokens)
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
        self.category_input = discord.ui.TextInput(label="ID da categoria dos tickets", default=str(channels.get("category_id") or ""), required=False, max_length=25)
        self.logs_input = discord.ui.TextInput(label="ID do canal de logs", default=str(channels.get("logs_channel_id") or ""), required=False, max_length=25)
        self.suggestions_input = discord.ui.TextInput(label="ID do canal de sugestões", default=str(channels.get("suggestions_channel_id") or ""), required=False, max_length=25)
        self.add_item(self.category_input)
        self.add_item(self.logs_input)
        self.add_item(self.suggestions_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
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
