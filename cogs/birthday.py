from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

BIRTHDAY_THREAD_NAME = "🎂 Aniversários"
BIRTHDAY_DOC_CONFIG = "birthday_config"
BIRTHDAY_DOC_ENTRY = "birthday_entry"
BIRTHDAY_DOC_SENT = "birthday_sent"
DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s*$")
VAR_RE = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")
DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_DELETE_AFTER = 10
DEFAULT_REACTION = "✅"
MAX_TEXT_DISPLAY = 3900

DEFAULT_TEMPLATES: dict[str, str] = {
    "calendar": (
        "# 🎂 Aniversários\n"
        "Mande seu aniversário na thread abaixo.\n\n"
        "## 📅 Calendário\n"
        "${birthdaycalendar}"
    ),
    "saved": "Prontinho, ${usermention}. Seu aniversário foi salvo como **${birthdaydate}** 🎂",
    "updated": "Prontinho, ${usermention}. Atualizei seu aniversário para **${birthdaydate}** 🎂",
    "invalid": "Data inválida. Mande uma data no estilo **dia/mês**.",
    "announce_single": "🎂 Feliz aniversário, ${usermention}! Hoje é seu dia.",
    "announce_group": "🎂 Hoje temos ${birthdaycount} aniversariante(s)!\n\n${birthdaymentions}",
    "empty_calendar": "Nenhum aniversário cadastrado ainda.",
}

TEMPLATE_LABELS: dict[str, str] = {
    "calendar": "Mensagem do calendário",
    "saved": "Resposta de data salva",
    "updated": "Resposta de data atualizada",
    "invalid": "Resposta de data inválida",
    "announce_single": "Aviso individual",
    "announce_group": "Aviso agrupado",
    "empty_calendar": "Calendário vazio",
}

TEMPLATE_DESCRIPTIONS: dict[str, str] = {
    "calendar": "Mensagem pública acima da thread. É aplicada no canal assim que salvar.",
    "saved": "Resposta temporária quando alguém cadastra a data pela primeira vez.",
    "updated": "Resposta temporária quando alguém troca a própria data.",
    "invalid": "Resposta temporária quando a mensagem da thread não é uma data válida.",
    "announce_single": "Mensagem enviada no canal de avisos para uma pessoa.",
    "announce_group": "Mensagem enviada quando há mais de um aniversariante no dia.",
    "empty_calendar": "Texto usado quando ainda não há aniversários no calendário.",
}

VARIABLES_BY_TEMPLATE: dict[str, tuple[str, ...]] = {
    "calendar": (
        "guildname", "guildid", "guildmembercount", "birthdaycount", "birthdaycalendar",
        "birthdaycalendarcompact", "birthdaycalendarnext10", "birthdaycalendarnext20",
        "nextbirthdayname", "nextbirthdaydate", "nextbirthdaymention", "nowtimestamp",
        "nowdate", "nowtime", "announcechannel", "registerchannel",
    ),
    "saved": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull",
        "birthdayage", "birthdaytimestamp", "nowtimestamp",
    ),
    "updated": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull",
        "birthdayage", "birthdaytimestamp", "nowtimestamp",
    ),
    "invalid": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "usermessage", "validexample", "nowtimestamp",
    ),
    "announce_single": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull",
        "birthdayage", "birthdaytimestamp", "nowtimestamp", "guildname",
    ),
    "announce_group": (
        "birthdaycount", "birthdaymentions", "birthdaynames", "birthdaylist",
        "birthdaylistnumbered", "nowtimestamp", "guildname",
    ),
    "empty_calendar": ("guildname", "nowtimestamp"),
}

VARIABLE_HELP: dict[str, str] = {
    "usermention": "menciona o membro",
    "userid": "ID do membro",
    "username": "nome de usuário",
    "userdisplayname": "nome exibido no servidor",
    "usernickname": "apelido no servidor",
    "usermessage": "mensagem enviada na thread",
    "birthdayday": "dia do aniversário",
    "birthdaymonth": "mês do aniversário",
    "birthdayyear": "ano informado, se existir",
    "birthdaydate": "data no formato dia/mês",
    "birthdaydatefull": "data com ano quando existir",
    "birthdayage": "idade, se o ano foi informado",
    "birthdaytimestamp": "timestamp Discord do aniversário deste ano",
    "birthdaycount": "quantidade de aniversários cadastrados/no dia",
    "birthdaycalendar": "calendário completo ordenado pelos próximos aniversários",
    "birthdaycalendarcompact": "calendário em linhas curtas",
    "birthdaycalendarnext10": "próximos 10 aniversários",
    "birthdaycalendarnext20": "próximos 20 aniversários",
    "birthdaymentions": "menções dos aniversariantes do dia",
    "birthdaynames": "nomes dos aniversariantes do dia",
    "birthdaylist": "lista com marcadores dos aniversariantes do dia",
    "birthdaylistnumbered": "lista numerada dos aniversariantes do dia",
    "nextbirthdayname": "nome do próximo aniversariante",
    "nextbirthdaydate": "data do próximo aniversário",
    "nextbirthdaymention": "menção do próximo aniversariante",
    "guildname": "nome do servidor",
    "guildid": "ID do servidor",
    "guildmembercount": "quantidade de membros do servidor",
    "announcechannel": "canal de avisos configurado",
    "registerchannel": "canal do calendário configurado",
    "nowtimestamp": "timestamp atual para usar em <t:${nowtimestamp}:R>",
    "nowdate": "data atual",
    "nowtime": "horário atual",
    "validexample": "exemplo de data válida",
}

VARIABLE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "member": ("usermention", "userid", "username", "userdisplayname", "usernickname"),
    "birthday": ("birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull", "birthdayage", "birthdaytimestamp"),
    "calendar": ("birthdaycount", "birthdaycalendar", "birthdaycalendarcompact", "birthdaycalendarnext10", "birthdaycalendarnext20", "nextbirthdayname", "nextbirthdaydate", "nextbirthdaymention"),
    "server": ("guildname", "guildid", "guildmembercount", "announcechannel", "registerchannel"),
    "time": ("nowtimestamp", "nowdate", "nowtime"),
    "invalid": ("usermessage", "validexample"),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_ts() -> int:
    return int(time.time())


def _trim(text: Any, limit: int = MAX_TEXT_DISPLAY) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 20)].rstrip() + "\n…"


def _channel_mention(channel_id: int | None) -> str:
    try:
        cid = int(channel_id or 0)
    except Exception:
        cid = 0
    return f"<#{cid}>" if cid else "não configurado"


def _member_display(member: discord.Member | None, user_id: int) -> str:
    if member is not None:
        return str(getattr(member, "display_name", None) or getattr(member, "name", None) or user_id)
    return f"Usuário {user_id}"


def _birthday_date(day: int, month: int) -> str:
    return f"{int(day):02d}/{int(month):02d}"


def _birthday_date_full(day: int, month: int, year: int | None) -> str:
    base = _birthday_date(day, month)
    if year:
        return f"{base}/{int(year):04d}"
    return base


def _is_leap(year: int) -> bool:
    return calendar.isleap(int(year))


def _valid_birthday(day: int, month: int, year: int | None = None) -> bool:
    if month < 1 or month > 12 or day < 1:
        return False
    ref_year = int(year or 2024)  # 2024 permite 29/02 quando o ano é omitido.
    try:
        datetime(ref_year, month, day)
    except ValueError:
        return False
    current_year = _utcnow().year
    if year is not None and (year < 1900 or year > current_year):
        return False
    return True


def _parse_date(raw: str) -> tuple[int, int, int | None] | None:
    match = DATE_RE.fullmatch(str(raw or ""))
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else None
    if not _valid_birthday(day, month, year):
        return None
    return day, month, year


def _age_for(year: int | None, *, day: int, month: int, now: datetime) -> str:
    if not year:
        return ""
    age = int(now.year) - int(year)
    if (now.month, now.day) < (int(month), int(day)):
        age -= 1
    return str(max(0, age))


def _next_occurrence(day: int, month: int, *, now: datetime, leap_mode: str = "feb28") -> datetime:
    year = int(now.year)
    d = int(day)
    m = int(month)
    if m == 2 and d == 29 and not _is_leap(year):
        if str(leap_mode) == "mar01":
            m, d = 3, 1
        else:
            m, d = 2, 28
    candidate = datetime(year, m, d, 0, 0, tzinfo=now.tzinfo)
    if candidate.date() < now.date():
        year += 1
        d = int(day)
        m = int(month)
        if m == 2 and d == 29 and not _is_leap(year):
            if str(leap_mode) == "mar01":
                m, d = 3, 1
            else:
                m, d = 2, 28
        candidate = datetime(year, m, d, 0, 0, tzinfo=now.tzinfo)
    return candidate


def _birthday_timestamp(day: int, month: int, *, now: datetime, leap_mode: str = "feb28") -> int:
    return int(_next_occurrence(day, month, now=now, leap_mode=leap_mode).timestamp())


def _find_unknown_variables(template: str, allowed: Iterable[str]) -> list[str]:
    allowed_set = set(allowed)
    found = {m.group(1) for m in VAR_RE.finditer(str(template or ""))}
    return sorted(v for v in found if v not in allowed_set)


def _replace_vars(template: str, values: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values.get(key) or "")
        return match.group(0)
    return VAR_RE.sub(repl, str(template or ""))


def _make_notice_view(title: str, body: str | list[str], *, ok: bool = True) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    if isinstance(body, list):
        body_text = "\n".join(str(x) for x in body if str(x).strip())
    else:
        body_text = str(body or "")
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(_trim(f"# {title}\n{body_text}")),
        accent_color=discord.Color.green() if ok else discord.Color.red(),
    ))
    return view


@dataclass(slots=True)
class CalendarEntry:
    user_id: int
    day: int
    month: int
    year: int | None
    display_name: str
    mention: str
    next_dt: datetime


class BirthdayMessageModal(discord.ui.Modal):
    def __init__(self, view: "BirthdayAdminView", template_key: str):
        label = TEMPLATE_LABELS.get(template_key, template_key)
        super().__init__(title=label[:45])
        self.panel = view
        self.template_key = template_key
        cfg = view.cog._normalize_config(view.config)
        current = cfg["templates"].get(template_key) or DEFAULT_TEMPLATES.get(template_key, "")
        self.body_input = discord.ui.TextInput(
            label="Mensagem",
            style=discord.TextStyle.paragraph,
            default=str(current)[:4000],
            required=True,
            max_length=4000,
        )
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        template = str(self.body_input.value or "").strip()
        if not template:
            await interaction.response.send_message(
                view=_make_notice_view("Mensagem vazia", "Escreva a mensagem antes de salvar.", ok=False),
                ephemeral=True,
            )
            return
        allowed = VARIABLES_BY_TEMPLATE.get(self.template_key, tuple(VARIABLE_HELP.keys()))
        unknown = _find_unknown_variables(template, allowed)
        if unknown:
            self.panel.pending_template_key = self.template_key
            self.panel.pending_template_value = template
            self.panel.notice = "Variáveis desconhecidas: " + ", ".join(f"${{{v}}}" for v in unknown)
            self.panel.go_to("confirm_template")
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        await self.panel.cog._set_template(interaction.guild, self.template_key, template)
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = "Mensagem salva."
        self.panel.return_to("messages")
        self.panel._rebuild()
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class BirthdayTimeModal(discord.ui.Modal):
    def __init__(self, view: "BirthdayAdminView"):
        super().__init__(title="Horário dos avisos")
        self.panel = view
        cfg = view.cog._normalize_config(view.config)
        hour = int(cfg.get("announce_hour", 9) or 9)
        minute = int(cfg.get("announce_minute", 0) or 0)
        timezone_name = str(cfg.get("timezone") or DEFAULT_TIMEZONE)
        self.time_input = discord.ui.TextInput(
            label="Horário",
            placeholder="09:00",
            default=f"{hour:02d}:{minute:02d}",
            required=True,
            max_length=5,
        )
        self.tz_input = discord.ui.TextInput(
            label="Fuso horário",
            placeholder="America/Sao_Paulo",
            default=timezone_name[:100],
            required=True,
            max_length=100,
        )
        self.add_item(self.time_input)
        self.add_item(self.tz_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_time = str(self.time_input.value or "").strip()
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw_time)
        if not m:
            await interaction.response.send_message(
                view=_make_notice_view("Horário inválido", "Use um horário no formato `09:00`.", ok=False),
                ephemeral=True,
            )
            return
        hour = int(m.group(1))
        minute = int(m.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await interaction.response.send_message(
                view=_make_notice_view("Horário inválido", "Use um horário entre `00:00` e `23:59`.", ok=False),
                ephemeral=True,
            )
            return
        tz_name = str(self.tz_input.value or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
        try:
            ZoneInfo(tz_name)
        except Exception:
            await interaction.response.send_message(
                view=_make_notice_view("Fuso inválido", "Use um nome de fuso válido, como `America/Sao_Paulo`.", ok=False),
                ephemeral=True,
            )
            return
        await self.panel.cog._update_config(interaction.guild.id, {"announce_hour": hour, "announce_minute": minute, "timezone": tz_name})
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = "Horário salvo."
        self.panel.return_to("preferences")
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class BirthdayPreferencesModal(discord.ui.Modal):
    def __init__(self, view: "BirthdayAdminView"):
        super().__init__(title="Preferências")
        self.panel = view
        cfg = view.cog._normalize_config(view.config)
        opts = cfg["options"]
        if not all(hasattr(discord.ui, attr) for attr in ("Label", "CheckboxGroup", "RadioGroup")):
            raise RuntimeError("discord.py 2.7+ é necessário para as preferências modernas.")

        self.flags_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=5)
        for value, label, desc, default in (
            ("temporary_reply", "Responder e apagar depois", "A resposta do bot some depois de alguns segundos.", bool(opts.get("temporary_reply", True))),
            ("show_age", "Mostrar idade nos avisos", "Só funciona quando a pessoa informou o ano.", bool(opts.get("show_age", True))),
            ("group_announcements", "Juntar aniversariantes do dia", "Uma mensagem para todos no mesmo dia.", bool(opts.get("group_announcements", True))),
            ("delete_on_leave", "Remover quando sair do servidor", "Limpa o calendário quando o membro sai.", bool(opts.get("delete_on_leave", True))),
        ):
            self.flags_group.add_option(label=label, value=value, description=desc, default=default)

        self.leap_group = discord.ui.RadioGroup(required=True)
        leap_mode = str(opts.get("leap_day_mode") or "feb28")
        self.leap_group.add_option(label="Avisar em 28/02", value="feb28", description="Para aniversários em 29/02 fora de ano bissexto.", default=leap_mode != "mar01")
        self.leap_group.add_option(label="Avisar em 01/03", value="mar01", description="Para aniversários em 29/02 fora de ano bissexto.", default=leap_mode == "mar01")

        self.reaction_input = discord.ui.TextInput(
            label="Reação em data válida",
            placeholder="✅",
            default=str(opts.get("valid_reaction") or DEFAULT_REACTION)[:20],
            required=True,
            max_length=20,
        )
        self.delete_after_input = discord.ui.TextInput(
            label="Apagar resposta do bot após segundos",
            placeholder="10",
            default=str(int(opts.get("delete_after_seconds") or DEFAULT_DELETE_AFTER)),
            required=True,
            max_length=3,
        )

        self.add_item(discord.ui.Label(
            text="Comportamento",
            description="Marque o que deve ficar ativo.",
            component=self.flags_group,
        ))
        self.add_item(discord.ui.Label(
            text="Aniversários em 29/02",
            component=self.leap_group,
        ))
        self.add_item(self.reaction_input)
        self.add_item(self.delete_after_input)

    async def on_submit(self, interaction: discord.Interaction):
        selected = set(getattr(self.flags_group, "values", None) or [])
        try:
            delete_after = int(str(self.delete_after_input.value or "10").strip())
        except Exception:
            delete_after = DEFAULT_DELETE_AFTER
        delete_after = max(1, min(delete_after, 120))
        reaction = str(self.reaction_input.value or DEFAULT_REACTION).strip() or DEFAULT_REACTION
        options = {
            "temporary_reply": "temporary_reply" in selected,
            "show_age": "show_age" in selected,
            "group_announcements": "group_announcements" in selected,
            "delete_on_leave": "delete_on_leave" in selected,
            "leap_day_mode": str(getattr(self.leap_group, "value", None) or "feb28"),
            "valid_reaction": reaction,
            "delete_after_seconds": delete_after,
        }
        await self.panel.cog._update_config(interaction.guild.id, {"options": options})
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = "Preferências salvas."
        self.panel.return_to("preferences")
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class _AdminMainSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="O que você quer configurar?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Cadastro", value="register", emoji="📍", description="Canal onde fica o calendário."),
                discord.SelectOption(label="Avisos", value="announce", emoji="📢", description="Canal e horário dos parabéns."),
                discord.SelectOption(label="Mensagens", value="messages", emoji="💬", description="Textos configuráveis do fluxo."),
                discord.SelectOption(label="Variáveis", value="variables", emoji="📌", description="Lista de variáveis disponíveis."),
                discord.SelectOption(label="Preferências", value="preferences", emoji="⚙️", description="Reação, idade, 29/02 e agrupamento."),
                discord.SelectOption(label="Testes", value="tests", emoji="🧪", description="Prévia sem esperar o dia."),
                discord.SelectOption(label="Aniversariantes", value="entries", emoji="📋", description="Ver e gerenciar aniversariantes."),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        target = str(self.values[0])
        if target == "entries":
            self.panel.entries_page = 0
            await self.panel._reload_entries()
        self.panel.go_to(target)
        self.panel.notice = ""
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _BackButton(discord.ui.Button):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.panel.go_back()
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _RegisterChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal onde o calendário vai ficar",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        channel = await self.panel.cog._resolve_text_channel(interaction.guild, selected)
        if channel is None:
            await interaction.response.send_message(
                view=_make_notice_view("Canal inválido", "Escolha um canal de texto.", ok=False), ephemeral=True
            )
            return
        missing = self.panel.cog._missing_channel_permissions(channel, for_register=True)
        if missing:
            await interaction.response.send_message(
                view=_make_notice_view("Permissões insuficientes", missing, ok=False), ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.panel.cog._set_register_channel(interaction.guild, channel)
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = f"Cadastro ajustado para {channel.mention}."
        self.panel.screen = "register"
        self.panel._rebuild()
        await interaction.message.edit(view=self.panel)


class _AnnounceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal onde os parabéns serão enviados",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        channel = await self.panel.cog._resolve_text_channel(interaction.guild, selected)
        if channel is None:
            await interaction.response.send_message(
                view=_make_notice_view("Canal inválido", "Escolha um canal de texto.", ok=False), ephemeral=True
            )
            return
        missing = self.panel.cog._missing_channel_permissions(channel, for_register=False)
        if missing:
            await interaction.response.send_message(
                view=_make_notice_view("Permissões insuficientes", missing, ok=False), ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.panel.cog._update_config(interaction.guild.id, {"announce_channel_id": int(channel.id)})
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = f"Avisos ajustados para {channel.mention}."
        self.panel.screen = "announce"
        self.panel._rebuild()
        await interaction.message.edit(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class _TemplateSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        current = panel.selected_template
        options = []
        for key, label in TEMPLATE_LABELS.items():
            options.append(discord.SelectOption(
                label=label,
                value=key,
                description=TEMPLATE_DESCRIPTIONS.get(key, "")[:100],
                default=(key == current),
            ))
        super().__init__(placeholder="Mensagem para editar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.panel.selected_template = str(self.values[0])
        self.panel.notice = ""
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _TemplateActionSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Ação para a mensagem selecionada",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Editar mensagem", value="edit", emoji="✏️"),
                discord.SelectOption(label="Ver prévia", value="preview", emoji="👀"),
                discord.SelectOption(label="Ver variáveis compatíveis", value="vars", emoji="📌"),
                discord.SelectOption(label="Restaurar padrão", value="restore", emoji="↩️"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        key = self.panel.selected_template
        if action == "edit":
            await interaction.response.send_modal(BirthdayMessageModal(self.panel, key))
            return
        if action == "preview":
            await self.panel.cog._send_template_preview(interaction, key)
            return
        if action == "vars":
            self.panel.go_to("template_vars")
            self.panel.notice = ""
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        if action == "restore":
            self.panel.go_to("confirm_restore")
            self.panel.notice = ""
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return


class _RestoreConfirmSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Escolha como continuar",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Restaurar mensagem padrão", value="restore", emoji="↩️"),
                discord.SelectOption(label="Ver prévia antes", value="preview", emoji="👀"),
                discord.SelectOption(label="Manter como está", value="cancel", emoji="✨"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        key = self.panel.selected_template
        if action == "preview":
            await self.panel.cog._send_template_preview(interaction, key, use_default=True)
            return
        if action == "cancel":
            self.panel.return_to("messages")
            self.panel.notice = "Nada foi alterado."
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        await self.panel.cog._set_template(interaction.guild, key, DEFAULT_TEMPLATES.get(key, ""))
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.return_to("messages")
        self.panel.notice = "Mensagem padrão restaurada."
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class _UnknownTemplateConfirmSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Variáveis desconhecidas encontradas",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Editar de novo", value="edit", emoji="✏️"),
                discord.SelectOption(label="Salvar mesmo assim", value="save", emoji="✅"),
                discord.SelectOption(label="Ver variáveis compatíveis", value="vars", emoji="📌"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        key = self.panel.pending_template_key or self.panel.selected_template
        if action == "edit":
            await interaction.response.send_modal(BirthdayMessageModal(self.panel, key))
            return
        if action == "vars":
            self.panel.selected_template = key
            self.panel.go_to("template_vars")
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        await self.panel.cog._set_template(interaction.guild, key, self.panel.pending_template_value or "")
        self.panel.pending_template_key = ""
        self.panel.pending_template_value = ""
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.return_to("messages")
        self.panel.notice = "Mensagem salva."
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class _VariableCategorySelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        labels = {
            "member": ("Membro", "Variáveis do usuário."),
            "birthday": ("Aniversário", "Data, idade e timestamp."),
            "calendar": ("Calendário", "Lista pública e próximos aniversários."),
            "server": ("Servidor", "Nome, canais e contagem."),
            "time": ("Horário", "Timestamp e horário atual."),
            "invalid": ("Mensagem inválida", "Texto enviado e exemplo válido."),
        }
        options = [discord.SelectOption(label=label, value=key, description=desc) for key, (label, desc) in labels.items()]
        super().__init__(placeholder="Categoria de variáveis", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.panel.variable_category = str(self.values[0])
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _PreferencesActionSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="O que ajustar?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Preferências gerais", value="prefs", emoji="⚙️", description="Reação, 29/02, agrupamento e limpeza."),
                discord.SelectOption(label="Horário dos avisos", value="time", emoji="🕘", description="Hora e fuso horário."),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "time":
            await interaction.response.send_modal(BirthdayTimeModal(self.panel))
            return
        try:
            await interaction.response.send_modal(BirthdayPreferencesModal(self.panel))
        except RuntimeError as exc:
            await interaction.response.send_message(view=_make_notice_view("Indisponível", str(exc), ok=False), ephemeral=True)


class _TestActionSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Teste que deseja fazer",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Prévia do calendário", value="calendar", emoji="📅"),
                discord.SelectOption(label="Resposta de data salva", value="saved", emoji="✅"),
                discord.SelectOption(label="Resposta de data inválida", value="invalid", emoji="⚠️"),
                discord.SelectOption(label="Aviso individual", value="single", emoji="🎂"),
                discord.SelectOption(label="Aviso agrupado", value="group", emoji="🎉"),
                discord.SelectOption(label="Enviar teste no canal de avisos", value="send", emoji="📨"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog._handle_test_action(interaction, str(self.values[0]))


class _EntriesPageButton(discord.ui.Button):
    def __init__(self, panel: "BirthdayAdminView", *, direction: str):
        self.panel = panel
        self.direction = direction
        total_pages = max(1, (len(panel.entries_cache) + panel.entries_per_page - 1) // panel.entries_per_page)
        if direction == "prev":
            label = "Anterior"
            emoji = "⬅️"
            disabled = panel.entries_page <= 0
        else:
            label = "Próxima"
            emoji = "➡️"
            disabled = panel.entries_page >= total_pages - 1
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        total_pages = max(1, (len(self.panel.entries_cache) + self.panel.entries_per_page - 1) // self.panel.entries_per_page)
        if self.direction == "prev":
            self.panel.entries_page = max(0, self.panel.entries_page - 1)
        else:
            self.panel.entries_page = min(total_pages - 1, self.panel.entries_page + 1)
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class BirthdayAdminView(discord.ui.LayoutView):
    def __init__(self, cog: "BirthdayCog", *, owner_id: int, guild_id: int, config: dict[str, Any]):
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.config = cog._normalize_config(config)
        self.screen = "home"
        self.screen_history: list[str] = []
        self.notice = ""
        self.selected_template = "calendar"
        self.variable_category = "member"
        self.pending_template_key = ""
        self.pending_template_value = ""
        self.entries_page = 0
        self.entries_per_page = 10
        self.entries_cache: list[CalendarEntry] = []
        self._rebuild()


    def go_to(self, screen: str, *, remember: bool = True):
        if remember and self.screen != screen:
            self.screen_history.append(self.screen)
        self.screen = screen

    def go_back(self):
        previous = self.screen_history.pop() if self.screen_history else "home"
        self.screen = previous
        self.notice = ""

    def return_to(self, screen: str):
        while self.screen_history and self.screen_history[-1] == screen:
            self.screen_history.pop()
        self.screen = screen

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            try:
                await interaction.response.send_message(
                    view=_make_notice_view("Painel em uso", "Esse painel pertence a quem abriu o comando.", ok=False),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return False
        if not self.cog._can_manage(interaction.user):
            try:
                await interaction.response.send_message(
                    view=_make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return False
        return True

    def _clear(self):
        for item in list(self.children):
            self.remove_item(item)

    def _base_lines(self) -> list[str]:
        cfg = self.config
        opts = cfg["options"]
        register_channel_id = int(cfg.get("register_channel_id") or 0)
        announce_channel_id = int(cfg.get("announce_channel_id") or 0)
        count = int(cfg.get("birthday_count") or 0)
        hour = int(cfg.get("announce_hour", 9) or 9)
        minute = int(cfg.get("announce_minute", 0) or 0)
        lines = [
            "# 🎂 Aniversários",
            "",
            f"**Cadastro**\n{_channel_mention(register_channel_id)}",
            "",
            f"**Avisos**\n{_channel_mention(announce_channel_id)} às `{hour:02d}:{minute:02d}`",
            "",
            f"**Calendário**\n{count} aniversariante(s) salvo(s)",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        lines.extend(["", "Escolha abaixo o que deseja ajustar."])
        return lines

    def _rebuild(self):
        self._clear()
        if self.screen == "home":
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay("\n".join(self._base_lines())),
                discord.ui.Separator(),
                discord.ui.ActionRow(_AdminMainSelect(self)),
                accent_color=discord.Color.blurple(),
            ))
            return
        if self.screen == "register":
            self._build_register()
            return
        if self.screen == "announce":
            self._build_announce()
            return
        if self.screen == "messages":
            self._build_messages()
            return
        if self.screen == "template_vars":
            self._build_template_vars()
            return
        if self.screen == "confirm_restore":
            self._build_restore_confirm()
            return
        if self.screen == "confirm_template":
            self._build_unknown_template_confirm()
            return
        if self.screen == "variables":
            self._build_variables()
            return
        if self.screen == "preferences":
            self._build_preferences()
            return
        if self.screen == "tests":
            self._build_tests()
            return
        if self.screen == "entries":
            self._build_entries()
            return
        self.screen = "home"
        self._rebuild()

    def _build_register(self):
        channel_id = int(self.config.get("register_channel_id") or 0)
        lines = [
            "# 📍 Cadastro",
            "Escolha o canal onde o calendário público vai aparecer.",
            "",
            f"**Canal atual**\n{_channel_mention(channel_id)}",
            "",
            "Os membros vão enviar a data no espaço de aniversários abaixo da mensagem pública.",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_RegisterChannelSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_announce(self):
        channel_id = int(self.config.get("announce_channel_id") or 0)
        hour = int(self.config.get("announce_hour", 9) or 9)
        minute = int(self.config.get("announce_minute", 0) or 0)
        lines = [
            "# 📢 Avisos",
            "Escolha onde o bot vai mandar os parabéns.",
            "",
            f"**Canal atual**\n{_channel_mention(channel_id)}",
            f"**Horário**\n`{hour:02d}:{minute:02d}`",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_AnnounceChannelSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_messages(self):
        key = self.selected_template
        template = self.config["templates"].get(key) or DEFAULT_TEMPLATES.get(key, "")
        unknown = _find_unknown_variables(template, VARIABLES_BY_TEMPLATE.get(key, tuple()))
        lines = [
            "# 💬 Mensagens",
            TEMPLATE_DESCRIPTIONS.get(key, "Escolha uma mensagem para editar."),
            "",
            f"**Selecionada:** {TEMPLATE_LABELS.get(key, key)}",
            "",
            "**Prévia do template salvo:**",
            "```",
            _trim(template, 900),
            "```",
        ]
        if unknown:
            lines.extend(["", "⚠️ Variáveis desconhecidas: " + ", ".join(f"${{{v}}}" for v in unknown)])
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_TemplateSelect(self)),
            discord.ui.ActionRow(_TemplateActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_template_vars(self):
        key = self.selected_template
        allowed = VARIABLES_BY_TEMPLATE.get(key, tuple())
        lines = [f"# 📌 Variáveis para {TEMPLATE_LABELS.get(key, key)}", ""]
        for var in allowed:
            lines.append(f"`${{{var}}}` — {VARIABLE_HELP.get(var, 'variável disponível')}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_TemplateActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_restore_confirm(self):
        key = self.selected_template
        lines = [
            "# Restaurar padrão",
            f"Mensagem: **{TEMPLATE_LABELS.get(key, key)}**",
            "",
            "Escolha abaixo como continuar.",
        ]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_RestoreConfirmSelect(self)),
            accent_color=discord.Color.orange(),
        ))

    def _build_unknown_template_confirm(self):
        lines = ["# Variáveis desconhecidas", self.notice or "Essa mensagem usa variáveis que não existem para esse tipo de configuração.", "", "Escolha como continuar."]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_UnknownTemplateConfirmSelect(self)),
            accent_color=discord.Color.orange(),
        ))

    def _build_variables(self):
        category = self.variable_category
        lines = ["# 📌 Variáveis", "Escolha uma categoria para ver o que pode ser usado nas mensagens.", ""]
        for var in VARIABLE_CATEGORIES.get(category, tuple()):
            lines.append(f"`${{{var}}}` — {VARIABLE_HELP.get(var, 'variável disponível')}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_VariableCategorySelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_preferences(self):
        cfg = self.config
        opts = cfg["options"]
        hour = int(cfg.get("announce_hour", 9) or 9)
        minute = int(cfg.get("announce_minute", 0) or 0)
        lines = [
            "# ⚙️ Preferências",
            f"**Horário dos avisos:** `{hour:02d}:{minute:02d}`",
            f"**Fuso:** `{cfg.get('timezone') or DEFAULT_TIMEZONE}`",
            f"**Reação em data válida:** {opts.get('valid_reaction') or DEFAULT_REACTION}",
            f"**Resposta temporária:** {int(opts.get('delete_after_seconds') or DEFAULT_DELETE_AFTER)}s",
            f"**Idade nos avisos:** {'sim' if opts.get('show_age', True) else 'não'}",
            f"**Avisos agrupados:** {'sim' if opts.get('group_announcements', True) else 'não'}",
            f"**29/02:** {'01/03' if opts.get('leap_day_mode') == 'mar01' else '28/02'}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_PreferencesActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_tests(self):
        lines = [
            "# 🧪 Testes",
            "Veja uma prévia das mensagens sem esperar chegar o dia.",
            "",
            "Os testes usam você como exemplo e não alteram o calendário.",
        ]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_TestActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_entries(self):
        entries = self.entries_cache
        start = self.entries_page * self.entries_per_page
        page_entries = entries[start:start + self.entries_per_page]
        total_pages = max(1, (len(entries) + self.entries_per_page - 1) // self.entries_per_page)
        lines = ["# 📋 Aniversariantes", f"{len(entries)} aniversariante(s) salvo(s)", f"Página {self.entries_page + 1}/{total_pages}", ""]
        if not page_entries:
            lines.append("Nenhum aniversariante encontrado.")
        else:
            for entry in page_entries:
                lines.append(f"• {entry.display_name} — {_birthday_date(entry.day, entry.month)}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_EntriesPageButton(self, direction="prev"), _EntriesPageButton(self, direction="next"), _BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    async def _reload_entries(self):
        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            self.entries_cache = []
            return
        entries = await self.cog._calendar_entries(guild, cleanup_missing=False)
        self.entries_cache = entries


class BirthdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sync_locks: dict[int, asyncio.Lock] = {}
        self._last_tick_key: str | None = None

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    async def cog_load(self):
        await self._ensure_indexes()
        self.birthday_daily_loop.start()

    async def cog_unload(self):
        self.birthday_daily_loop.cancel()

    async def _ensure_indexes(self):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        try:
            await db.coll.create_index([("type", 1), ("guild_id", 1)], name="birthday_type_guild")
            await db.coll.create_index([("type", 1), ("guild_id", 1), ("user_id", 1)], name="birthday_entry_user")
            await db.coll.create_index([("type", 1), ("guild_id", 1), ("month", 1), ("day", 1)], name="birthday_entry_date")
        except Exception as exc:
            log.warning("falha ao criar índices de aniversário: %s", exc)

    def _normalize_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        cfg = dict(config or {})
        cfg.setdefault("type", BIRTHDAY_DOC_CONFIG)
        cfg.setdefault("templates", {})
        templates = dict(DEFAULT_TEMPLATES)
        templates.update({k: str(v) for k, v in dict(cfg.get("templates") or {}).items() if k in DEFAULT_TEMPLATES})
        cfg["templates"] = templates
        opts = {
            "allow_update": True,
            "temporary_reply": True,
            "show_age": True,
            "group_announcements": True,
            "delete_on_leave": True,
            "leap_day_mode": "feb28",
            "valid_reaction": DEFAULT_REACTION,
            "delete_after_seconds": DEFAULT_DELETE_AFTER,
        }
        opts.update(dict(cfg.get("options") or {}))
        cfg["options"] = opts
        cfg.setdefault("announce_hour", 9)
        cfg.setdefault("announce_minute", 0)
        cfg.setdefault("timezone", DEFAULT_TIMEZONE)
        return cfg

    async def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return self._normalize_config({"guild_id": int(guild_id)})
        doc = await db.coll.find_one({"type": BIRTHDAY_DOC_CONFIG, "guild_id": int(guild_id)}, {"_id": 0})
        cfg = self._normalize_config(doc or {"guild_id": int(guild_id)})
        try:
            cfg["birthday_count"] = await db.coll.count_documents({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id)})
        except Exception:
            cfg["birthday_count"] = 0
        return cfg

    async def _save_config(self, guild_id: int, config: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        cfg = self._normalize_config(config)
        cfg["guild_id"] = int(guild_id)
        cfg["type"] = BIRTHDAY_DOC_CONFIG
        await db.coll.update_one(
            {"type": BIRTHDAY_DOC_CONFIG, "guild_id": int(guild_id)},
            {"$set": cfg},
            upsert=True,
        )

    async def _update_config(self, guild_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        cfg = await self._get_config(int(guild_id))
        for key, value in updates.items():
            if key == "options":
                opts = dict(cfg.get("options") or {})
                opts.update(dict(value or {}))
                cfg["options"] = opts
            elif key == "templates":
                templates = dict(cfg.get("templates") or {})
                templates.update(dict(value or {}))
                cfg["templates"] = templates
            else:
                cfg[key] = value
        await self._save_config(int(guild_id), cfg)
        return cfg

    async def _set_template(self, guild: discord.Guild, key: str, template: str):
        cfg = await self._get_config(int(guild.id))
        templates = dict(cfg.get("templates") or {})
        templates[str(key)] = str(template or "")
        await self._update_config(int(guild.id), {"templates": templates})
        if key == "calendar":
            await self._sync_public_calendar(guild)

    def _can_manage(self, user: discord.abc.User) -> bool:
        perms = getattr(user, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        gid = int(guild_id)
        lock = self._sync_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._sync_locks[gid] = lock
        return lock


    async def _resolve_text_channel(self, guild: discord.Guild | None, selected: Any) -> discord.TextChannel | None:
        if guild is None or selected is None:
            return None
        if isinstance(selected, discord.TextChannel):
            return selected
        try:
            channel_id = int(getattr(selected, "id", 0) or 0)
        except Exception:
            channel_id = 0
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _missing_channel_permissions(self, channel: discord.TextChannel, *, for_register: bool) -> str:
        guild = channel.guild
        me = guild.me or (guild.get_member(int(self.bot.user.id)) if self.bot.user else None)
        if me is None:
            return "Não consegui verificar minhas permissões nesse canal."
        perms = channel.permissions_for(me)
        needed = [
            ("view_channel", "ver o canal"),
            ("send_messages", "enviar mensagens"),
        ]
        if for_register:
            needed.extend([
                ("create_public_threads", "criar threads públicas"),
                ("send_messages_in_threads", "enviar mensagens em threads"),
                ("read_message_history", "ler histórico de mensagens"),
                ("add_reactions", "adicionar reações"),
            ])
        missing = [label for attr, label in needed if not bool(getattr(perms, attr, False))]
        if not missing:
            return ""
        return "Não consigo usar esse canal ainda. Permissões necessárias: " + ", ".join(missing) + "."

    async def _set_register_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        cfg = await self._get_config(int(guild.id))
        current_channel_id = int(cfg.get("register_channel_id") or 0)
        message_id = int(cfg.get("register_message_id") or 0)
        thread_id = int(cfg.get("birthday_thread_id") or 0)
        cfg["register_channel_id"] = int(channel.id)
        if message_id and current_channel_id == int(channel.id):
            await self._save_config(int(guild.id), cfg)
            await self._sync_public_calendar(guild)
            return

        # Primeira publicação do sistema, ou troca explícita de canal. O painel não
        # oferece fluxo de recriação; quando o local muda, o novo local vira a fonte
        # salva e as próximas mudanças são sempre edições desse registro.
        view = await self._make_calendar_view(guild, cfg)
        msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        cfg["register_message_id"] = int(msg.id)
        try:
            thread = await msg.create_thread(name=BIRTHDAY_THREAD_NAME, auto_archive_duration=10080)
            cfg["birthday_thread_id"] = int(thread.id)
        except discord.HTTPException as exc:
            log.warning("não consegui criar thread de aniversários: %s", exc)
            cfg["birthday_thread_id"] = thread_id or 0
        await self._save_config(int(guild.id), cfg)
        await self._sync_public_calendar(guild)

    async def _calendar_entries(self, guild: discord.Guild, *, month_filter: int | None = None, cleanup_missing: bool = True) -> list[CalendarEntry]:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return []
        cfg = await self._get_config(int(guild.id))
        opts = cfg["options"]
        tz = ZoneInfo(str(cfg.get("timezone") or DEFAULT_TIMEZONE))
        now = datetime.now(tz)
        query: dict[str, Any] = {"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild.id)}
        if month_filter:
            query["month"] = int(month_filter)
        docs = []
        cursor = db.coll.find(query, {"_id": 0}).sort([("month", 1), ("day", 1)])
        async for doc in cursor:
            docs.append(doc)
        result: list[CalendarEntry] = []
        stale: list[int] = []
        for doc in docs:
            try:
                uid = int(doc.get("user_id") or 0)
                day = int(doc.get("day") or 0)
                month = int(doc.get("month") or 0)
            except Exception:
                continue
            if not uid or not _valid_birthday(day, month, None):
                continue
            member = guild.get_member(uid)
            if member is None and cleanup_missing:
                try:
                    member = await guild.fetch_member(uid)
                except discord.NotFound:
                    stale.append(uid)
                    continue
                except discord.HTTPException:
                    member = None
            if member is None and cleanup_missing:
                stale.append(uid)
                continue
            year_raw = doc.get("year")
            try:
                year = int(year_raw) if year_raw else None
            except Exception:
                year = None
            next_dt = _next_occurrence(day, month, now=now, leap_mode=str(opts.get("leap_day_mode") or "feb28"))
            result.append(CalendarEntry(
                user_id=uid,
                day=day,
                month=month,
                year=year,
                display_name=_member_display(member, uid),
                mention=f"<@{uid}>",
                next_dt=next_dt,
            ))
        if stale and cleanup_missing:
            await db.coll.delete_many({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild.id), "user_id": {"$in": stale}})
        result.sort(key=lambda e: (e.next_dt, e.display_name.casefold()))
        return result

    async def _render_calendar(self, guild: discord.Guild, *, limit: int | None = None, compact: bool = False) -> str:
        cfg = await self._get_config(int(guild.id))
        entries = await self._calendar_entries(guild, cleanup_missing=True)
        if not entries:
            return _replace_vars(cfg["templates"].get("empty_calendar") or DEFAULT_TEMPLATES["empty_calendar"], self._base_values(guild, cfg))
        shown = entries if limit is None else entries[:max(0, int(limit))]
        lines = []
        for entry in shown:
            if compact:
                lines.append(f"{_birthday_date(entry.day, entry.month)} — {entry.display_name}")
            else:
                lines.append(f"• {_birthday_date(entry.day, entry.month)} — {entry.display_name}")
        hidden = len(entries) - len(shown)
        if hidden > 0:
            lines.append(f"+ {hidden} aniversário(s) cadastrado(s)")
        return "\n".join(lines)

    def _base_values(self, guild: discord.Guild, cfg: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow()
        return {
            "guildname": getattr(guild, "name", "Servidor"),
            "guildid": int(getattr(guild, "id", 0) or 0),
            "guildmembercount": int(getattr(guild, "member_count", 0) or 0),
            "announcechannel": _channel_mention(cfg.get("announce_channel_id")),
            "registerchannel": _channel_mention(cfg.get("register_channel_id")),
            "nowtimestamp": int(now.timestamp()),
            "nowdate": now.strftime("%d/%m/%Y"),
            "nowtime": now.strftime("%H:%M"),
        }

    async def _calendar_values(self, guild: discord.Guild, cfg: dict[str, Any]) -> dict[str, Any]:
        entries = await self._calendar_entries(guild, cleanup_missing=True)
        values = self._base_values(guild, cfg)
        values["birthdaycount"] = len(entries)
        values["birthdaycalendar"] = await self._render_calendar(guild)
        values["birthdaycalendarcompact"] = await self._render_calendar(guild, compact=True)
        values["birthdaycalendarnext10"] = await self._render_calendar(guild, limit=10)
        values["birthdaycalendarnext20"] = await self._render_calendar(guild, limit=20)
        if entries:
            first = entries[0]
            values.update({
                "nextbirthdayname": first.display_name,
                "nextbirthdaydate": _birthday_date(first.day, first.month),
                "nextbirthdaymention": first.mention,
            })
        else:
            values.update({"nextbirthdayname": "", "nextbirthdaydate": "", "nextbirthdaymention": ""})
        return values

    async def _make_calendar_view(self, guild: discord.Guild, cfg: dict[str, Any] | None = None) -> discord.ui.LayoutView:
        cfg = self._normalize_config(cfg or await self._get_config(int(guild.id)))
        values = await self._calendar_values(guild, cfg)
        body = _replace_vars(cfg["templates"].get("calendar") or DEFAULT_TEMPLATES["calendar"], values)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim(body)),
            accent_color=discord.Color.blurple(),
        ))
        return view

    async def _fetch_public_message(self, guild: discord.Guild, cfg: dict[str, Any]) -> discord.Message | None:
        channel_id = int(cfg.get("register_channel_id") or 0)
        message_id = int(cfg.get("register_message_id") or 0)
        if not channel_id or not message_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            return await channel.fetch_message(message_id)
        except discord.HTTPException:
            return None

    async def _sync_public_calendar(self, guild: discord.Guild | None) -> bool:
        if guild is None:
            return False
        async with self._lock_for(int(guild.id)):
            cfg = await self._get_config(int(guild.id))
            msg = await self._fetch_public_message(guild, cfg)
            if msg is None:
                return False
            view = await self._make_calendar_view(guild, cfg)
            try:
                await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                return True
            except discord.HTTPException as exc:
                log.warning("falha ao editar calendário de aniversários guild=%s: %s", guild.id, exc)
                return False

    async def _send_temp_view(self, message: discord.Message, template_key: str, values: dict[str, Any], *, ok: bool = True):
        cfg = await self._get_config(int(message.guild.id))
        opts = cfg["options"]
        template = cfg["templates"].get(template_key) or DEFAULT_TEMPLATES.get(template_key, "")
        body = _replace_vars(template, values)
        view = _make_notice_view("🎂 Aniversários" if ok else "Aniversários", body, ok=ok)
        try:
            sent = await message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return
        if bool(opts.get("temporary_reply", True)):
            delay = int(opts.get("delete_after_seconds") or DEFAULT_DELETE_AFTER)
            asyncio.create_task(self._delete_later(sent, delay=delay))

    async def _delete_later(self, message: discord.Message, *, delay: int):
        await asyncio.sleep(max(1, int(delay)))
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    def _member_values(self, member: discord.Member | discord.User, *, day: int, month: int, year: int | None, cfg: dict[str, Any], user_message: str = "") -> dict[str, Any]:
        tz = ZoneInfo(str(cfg.get("timezone") or DEFAULT_TIMEZONE))
        now = datetime.now(tz)
        opts = cfg["options"]
        display_name = str(getattr(member, "display_name", None) or getattr(member, "name", None) or member.id)
        values = {
            "usermention": getattr(member, "mention", f"<@{int(member.id)}>") or f"<@{int(member.id)}>",
            "userid": int(member.id),
            "username": str(getattr(member, "name", "") or ""),
            "userdisplayname": display_name,
            "usernickname": display_name,
            "usermessage": user_message,
            "validexample": "23/09",
            "birthdayday": f"{int(day):02d}",
            "birthdaymonth": f"{int(month):02d}",
            "birthdayyear": str(year or ""),
            "birthdaydate": _birthday_date(day, month),
            "birthdaydatefull": _birthday_date_full(day, month, year),
            "birthdayage": _age_for(year, day=day, month=month, now=now),
            "birthdaytimestamp": _birthday_timestamp(day, month, now=now, leap_mode=str(opts.get("leap_day_mode") or "feb28")),
            "nowtimestamp": int(now.timestamp()),
        }
        return values

    async def _send_template_preview(self, interaction: discord.Interaction, key: str, *, use_default: bool = False):
        cfg = await self._get_config(int(interaction.guild.id))
        template = DEFAULT_TEMPLATES.get(key, "") if use_default else cfg["templates"].get(key, DEFAULT_TEMPLATES.get(key, ""))
        values = await self._preview_values(interaction.guild, interaction.user, cfg)
        rendered = _replace_vars(template, values)
        await interaction.response.send_message(
            view=_make_notice_view("Prévia", rendered, ok=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _preview_values(self, guild: discord.Guild, user: discord.Member | discord.User, cfg: dict[str, Any]) -> dict[str, Any]:
        values = await self._calendar_values(guild, cfg)
        values.update(self._member_values(user, day=23, month=9, year=2006, cfg=cfg, user_message="23/09"))
        values.update({
            "birthdaymentions": getattr(user, "mention", f"<@{int(user.id)}>"),
            "birthdaynames": str(getattr(user, "display_name", None) or getattr(user, "name", "")),
            "birthdaylist": f"• {getattr(user, 'mention', f'<@{int(user.id)}>')} — 23/09",
            "birthdaylistnumbered": f"1. {getattr(user, 'mention', f'<@{int(user.id)}>')} — 23/09",
            "birthdaycount": values.get("birthdaycount") or 1,
        })
        return values

    async def _handle_test_action(self, interaction: discord.Interaction, action: str):
        cfg = await self._get_config(int(interaction.guild.id))
        if action == "send":
            channel_id = int(cfg.get("announce_channel_id") or 0)
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(
                    view=_make_notice_view("Canal não configurado", "Escolha o canal de avisos antes de enviar um teste.", ok=False),
                    ephemeral=True,
                )
                return
            values = await self._preview_values(interaction.guild, interaction.user, cfg)
            body = _replace_vars(cfg["templates"].get("announce_single") or DEFAULT_TEMPLATES["announce_single"], values)
            await channel.send(content=body, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            await interaction.response.send_message(view=_make_notice_view("Teste enviado", f"Enviei a prévia em {channel.mention}.", ok=True), ephemeral=True)
            return
        key_map = {"calendar": "calendar", "saved": "saved", "invalid": "invalid", "single": "announce_single", "group": "announce_group"}
        await self._send_template_preview(interaction, key_map.get(action, "calendar"))

    async def _upsert_birthday(self, guild_id: int, user_id: int, *, day: int, month: int, year: int | None) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return False
        existing = await db.coll.find_one({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)}, {"_id": 0})
        now_iso = _utcnow().isoformat()
        update = {
            "type": BIRTHDAY_DOC_ENTRY,
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "day": int(day),
            "month": int(month),
            "year": int(year) if year else None,
            "updated_at": now_iso,
        }
        if not existing:
            update["created_at"] = now_iso
        await db.coll.update_one(
            {"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)},
            {"$set": update, "$setOnInsert": {"created_at": now_iso}},
            upsert=True,
        )
        return bool(existing)

    async def _remove_birthday(self, guild_id: int, user_id: int) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return False
        res = await db.coll.delete_many({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)})
        await db.coll.delete_many({"type": BIRTHDAY_DOC_SENT, "guild_id": int(guild_id), "user_id": int(user_id)})
        return bool(getattr(res, "deleted_count", 0))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        cfg = await self._get_config(int(message.guild.id))
        thread_id = int(cfg.get("birthday_thread_id") or 0)
        if not thread_id or int(getattr(message.channel, "id", 0) or 0) != thread_id:
            return
        parsed = _parse_date(message.content or "")
        if parsed is None:
            values = self._member_values(message.author, day=23, month=9, year=None, cfg=cfg, user_message=message.content or "")
            await self._send_temp_view(message, "invalid", values, ok=False)
            return
        day, month, year = parsed
        updated = await self._upsert_birthday(message.guild.id, message.author.id, day=day, month=month, year=year)
        opts = cfg["options"]
        reaction = str(opts.get("valid_reaction") or DEFAULT_REACTION).strip() or DEFAULT_REACTION
        try:
            await message.add_reaction(reaction)
        except discord.HTTPException:
            pass
        values = self._member_values(message.author, day=day, month=month, year=year, cfg=cfg, user_message=message.content or "")
        await self._send_temp_view(message, "updated" if updated else "saved", values, ok=True)
        await self._sync_public_calendar(message.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await self._get_config(int(member.guild.id))
        if not bool(cfg.get("options", {}).get("delete_on_leave", True)):
            return
        removed = await self._remove_birthday(int(member.guild.id), int(member.id))
        if removed:
            await self._sync_public_calendar(member.guild)

    @commands.command(name="birthday")
    @commands.guild_only()
    async def birthday_panel(self, ctx: commands.Context):
        if not self._can_manage(ctx.author):
            await ctx.reply(view=_make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False), mention_author=False)
            return
        cfg = await self._get_config(int(ctx.guild.id))
        view = BirthdayAdminView(self, owner_id=int(ctx.author.id), guild_id=int(ctx.guild.id), config=cfg)
        msg = await ctx.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        view.message = msg

    @tasks.loop(minutes=1)
    async def birthday_daily_loop(self):
        tick = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        if tick == self._last_tick_key:
            return
        self._last_tick_key = tick
        for guild in list(self.bot.guilds):
            try:
                await self._maybe_send_daily_announcements(guild)
            except Exception as exc:
                log.warning("falha no envio diário de aniversários guild=%s: %s", getattr(guild, "id", None), exc)

    @birthday_daily_loop.before_loop
    async def before_birthday_daily_loop(self):
        await self.bot.wait_until_ready()

    async def _maybe_send_daily_announcements(self, guild: discord.Guild):
        cfg = await self._get_config(int(guild.id))
        channel_id = int(cfg.get("announce_channel_id") or 0)
        if not channel_id:
            return
        tz = ZoneInfo(str(cfg.get("timezone") or DEFAULT_TIMEZONE))
        now = datetime.now(tz)
        if int(cfg.get("announce_hour", 9) or 9) != now.hour or int(cfg.get("announce_minute", 0) or 0) != now.minute:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.TextChannel):
            return
        entries = await self._entries_for_today(guild, now=now, cfg=cfg)
        if not entries:
            return
        unsent = []
        for entry in entries:
            if not await self._sent_this_year(guild.id, entry.user_id, now.year):
                unsent.append(entry)
        if not unsent:
            return
        opts = cfg["options"]
        if bool(opts.get("group_announcements", True)) and len(unsent) > 1:
            values = await self._announcement_group_values(guild, unsent, cfg)
            body = _replace_vars(cfg["templates"].get("announce_group") or DEFAULT_TEMPLATES["announce_group"], values)
            await channel.send(content=body, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            for entry in unsent:
                await self._mark_sent(guild.id, entry.user_id, now.year)
        else:
            for entry in unsent:
                member = guild.get_member(entry.user_id)
                if member is None:
                    continue
                values = self._member_values(member, day=entry.day, month=entry.month, year=entry.year, cfg=cfg)
                values.update(self._base_values(guild, cfg))
                body = _replace_vars(cfg["templates"].get("announce_single") or DEFAULT_TEMPLATES["announce_single"], values)
                await channel.send(content=body, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                await self._mark_sent(guild.id, entry.user_id, now.year)

    async def _entries_for_today(self, guild: discord.Guild, *, now: datetime, cfg: dict[str, Any]) -> list[CalendarEntry]:
        entries = await self._calendar_entries(guild, cleanup_missing=True)
        opts = cfg["options"]
        result = []
        for entry in entries:
            day = entry.day
            month = entry.month
            if month == 2 and day == 29 and not _is_leap(now.year):
                if str(opts.get("leap_day_mode") or "feb28") == "mar01":
                    day, month = 1, 3
                else:
                    day, month = 28, 2
            if day == now.day and month == now.month:
                result.append(entry)
        return result

    async def _announcement_group_values(self, guild: discord.Guild, entries: list[CalendarEntry], cfg: dict[str, Any]) -> dict[str, Any]:
        values = self._base_values(guild, cfg)
        mentions = [entry.mention for entry in entries]
        names = [entry.display_name for entry in entries]
        values.update({
            "birthdaycount": len(entries),
            "birthdaymentions": ", ".join(mentions),
            "birthdaynames": ", ".join(names),
            "birthdaylist": "\n".join(f"• {entry.mention} — {_birthday_date(entry.day, entry.month)}" for entry in entries),
            "birthdaylistnumbered": "\n".join(f"{i}. {entry.mention} — {_birthday_date(entry.day, entry.month)}" for i, entry in enumerate(entries, 1)),
        })
        return values

    async def _sent_this_year(self, guild_id: int, user_id: int, year: int) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return True
        doc = await db.coll.find_one(
            {
                "type": BIRTHDAY_DOC_ENTRY,
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "sent_years": int(year),
            },
            {"_id": 0, "user_id": 1},
        )
        return bool(doc)

    async def _mark_sent(self, guild_id: int, user_id: int, year: int):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        await db.coll.update_one(
            {"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)},
            {
                "$addToSet": {"sent_years": int(year)},
                "$set": {"last_sent_at": _utcnow().isoformat()},
            },
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
