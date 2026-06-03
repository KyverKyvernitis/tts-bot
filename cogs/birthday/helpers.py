from __future__ import annotations

import calendar
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import discord

from .constants import (
    DATE_RE,
    DEFAULT_TIMEZONE,
    MAX_CALENDAR_DAY_LINE,
    MAX_CALENDAR_NAME_DISPLAY,
    MAX_TEXT_DISPLAY,
    VARIABLES_BY_TEMPLATE,
    VAR_RE,
)


def _template_variable_hint(template_key: str) -> str:
    variables = VARIABLES_BY_TEMPLATE.get(template_key, tuple())
    if not variables:
        return ""
    rendered = ", ".join(f"${{{name}}}" for name in variables)
    return "Variáveis compatíveis: " + rendered


def _clean_public_calendar_body(text: str) -> str:
    lines = str(text or "").splitlines()
    kept: list[str] = []
    skip_next_instruction_line = False
    for line in lines:
        stripped = line.strip()
        normalized = stripped.casefold()
        if not stripped:
            kept.append("")
            continue
        if skip_next_instruction_line and normalized.startswith("abaixo"):
            skip_next_instruction_line = False
            continue
        skip_next_instruction_line = False
        if normalized == "mande seu aniversário na thread abaixo.":
            continue
        if "coloque sua data de aniversário" in normalized or "registrar seu aniversário" in normalized:
            skip_next_instruction_line = True
            continue
        if re.fullmatch(r"[─━—_\-=\s]{5,}", stripped):
            continue
        if "calend" in normalized and (
            normalized.startswith("#")
            or normalized.startswith("📅")
            or normalized.startswith("**📅")
        ):
            continue
        kept.append(line.rstrip())
    value = "\n".join(kept).strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value or "# 🎂 Aniversários"


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


def _member_display(member: discord.Member | None, user_id: int, fallback: str | None = None) -> str:
    if member is not None:
        return str(getattr(member, "display_name", None) or getattr(member, "name", None) or user_id)
    if fallback:
        return str(fallback)
    return f"Usuário {user_id}"


def _display_sort_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _calendar_display_name(value: str, *, limit: int = MAX_CALENDAR_NAME_DISPLAY) -> str:
    rendered = re.sub(r"\s+", " ", str(value or "")).strip()
    if not rendered:
        return "sem nome"
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(1, limit - 1)].rstrip() + "…"


def _birthday_count_label(count: int) -> str:
    total = int(count or 0)
    suffix = "aniversariante" if total == 1 else "aniversariantes"
    return f"{total} {suffix}"


def _join_calendar_names(entries: Iterable["CalendarEntry"], *, max_chars: int = MAX_CALENDAR_DAY_LINE) -> str:
    names = [_calendar_display_name(entry.display_name) for entry in entries]
    if not names:
        return "sem nome"
    selected: list[str] = []
    for name in names:
        candidate = ", ".join([*selected, name])
        if selected and len(candidate) > max_chars:
            remaining = len(names) - len(selected)
            return ", ".join(selected) + f" + {remaining}"
        selected.append(name)
    return ", ".join(selected)


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
