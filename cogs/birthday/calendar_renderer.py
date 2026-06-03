from __future__ import annotations

from .constants import PT_MONTHS
from .helpers import (
    _birthday_count_label,
    _birthday_date,
    _calendar_display_name,
    _display_sort_key,
    _join_calendar_names,
)
from .models import CalendarEntry


def calendar_summary_line(entries: list[CalendarEntry]) -> str:
    if not entries:
        return ""
    upcoming = min(entries, key=lambda e: (e.next_dt, _display_sort_key(e.display_name)))
    return (
        f"-# {_birthday_count_label(len(entries))} · "
        f"próximo: `{_birthday_date(upcoming.day, upcoming.month)}` — "
        f"{_calendar_display_name(upcoming.display_name)}"
    )

def render_calendar_entries(
    entries: list[CalendarEntry],
    *,
    limit: int | None = None,
    compact: bool = False,
    include_summary: bool = False,
) -> str:
    if not entries:
        return ""

    if limit is None:
        ordered = sorted(entries, key=lambda e: (int(e.month), int(e.day), _display_sort_key(e.display_name)))
    else:
        ordered = sorted(entries, key=lambda e: (e.next_dt, _display_sort_key(e.display_name)))
    shown = ordered if limit is None else ordered[:max(0, int(limit))]

    month_order: list[int] = []
    month_groups: dict[int, list[CalendarEntry]] = {}
    for entry in shown:
        month = int(entry.month)
        if month not in month_groups:
            month_order.append(month)
        month_groups.setdefault(month, []).append(entry)

    lines: list[str] = []
    if include_summary:
        summary = calendar_summary_line(entries)
        if summary:
            lines.extend([summary, ""])

    for month_index, month in enumerate(month_order):
        month_entries = month_groups.get(month, [])
        if month_index:
            lines.append("")
        if not compact:
            lines.append(f"## 🗓️ {PT_MONTHS.get(month, f'{month:02d}')}")
        day_order: list[tuple[int, int]] = []
        day_groups: dict[tuple[int, int], list[CalendarEntry]] = {}
        for entry in sorted(month_entries, key=lambda e: (int(e.day), _display_sort_key(e.display_name))):
            key = (int(entry.day), int(entry.month))
            if key not in day_groups:
                day_order.append(key)
            day_groups.setdefault(key, []).append(entry)
        for day, day_month in day_order:
            day_entries = sorted(day_groups[(day, day_month)], key=lambda e: _display_sort_key(e.display_name))
            bullet = "• " if not compact else ""
            lines.append(f"{bullet}`{_birthday_date(day, day_month)}` — {_join_calendar_names(day_entries)}")

    hidden = len(ordered) - len(shown)
    if hidden > 0:
        lines.extend(["", f"-# + {_birthday_count_label(hidden)} fora desta prévia"])
    return "\n".join(line.rstrip() for line in lines).strip()
