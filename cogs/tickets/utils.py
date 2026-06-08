from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import discord

from .constants import DEFAULT_REPORT_TYPES, PUBLIC_OPTIONS, TICKET_KINDS, default_ticket_config


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate(text: object, limit: int, *, suffix: str = "…") -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    if limit <= len(suffix):
        return value[:limit]
    return value[: limit - len(suffix)] + suffix


def clean_accent_hex(raw: object, *, fallback: str = "#5865F2") -> str:
    value = str(raw or "").strip()
    if not value:
        value = fallback
    if value.startswith("#"):
        value = value[1:]
    elif value.lower().startswith("0x"):
        value = value[2:]
    if len(value) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in value):
        return f"#{value.upper()}"
    fallback = str(fallback or "#5865F2").strip()
    if fallback.startswith("#"):
        return fallback.upper()
    return "#5865F2"


def accent_color(raw: object, *, fallback: str = "#5865F2") -> discord.Color:
    hex_value = clean_accent_hex(raw, fallback=fallback)
    return discord.Color(int(hex_value[1:], 16))


def parse_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on", "sim", "s", "ligado", "ativado"}:
        return True
    if text in {"0", "false", "no", "n", "off", "não", "nao", "desligado", "desativado"}:
        return False
    return default


def id_from_mention_or_text(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.search(r"\d{15,25}", text)
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


def normalize_report_types(values: object) -> list[str]:
    if isinstance(values, str):
        raw_items = re.split(r"[\n;,]+", values)
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
    else:
        raw_items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        label = truncate(str(item or "").strip(), 80, suffix="")
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(label)
        if len(result) >= 10:
            break
    return result or list(DEFAULT_REPORT_TYPES)


def sanitize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    base = default_ticket_config()
    raw = cfg if isinstance(cfg, dict) else {}

    for section in ("panel", "channels", "roles", "enabled", "options", "texts"):
        payload = raw.get(section) if isinstance(raw.get(section), dict) else {}
        base[section].update(payload)

    base["panel"]["channel_id"] = int(base["panel"].get("channel_id") or 0)
    base["panel"]["message_id"] = int(base["panel"].get("message_id") or 0)
    base["panel"]["title"] = truncate(base["panel"].get("title") or "🎫 Atendimento", 200, suffix="")
    base["panel"]["description"] = truncate(base["panel"].get("description") or "Escolha abaixo o tipo de atendimento.", 1800, suffix="")
    base["panel"]["placeholder"] = truncate(base["panel"].get("placeholder") or "Escolha uma opção", 100, suffix="")
    base["panel"]["accent_color"] = clean_accent_hex(base["panel"].get("accent_color"))

    for key in ("category_id", "logs_channel_id", "suggestions_channel_id"):
        base["channels"][key] = int(base["channels"].get(key) or 0)
    for key in ("staff_role_id", "partnership_staff_role_id", "report_staff_role_id", "other_staff_role_id"):
        base["roles"][key] = int(base["roles"].get(key) or 0)
    for key in TICKET_KINDS:
        base["enabled"][key] = bool(base["enabled"].get(key, True))

    base["options"]["allow_multiple_open_tickets"] = bool(base["options"].get("allow_multiple_open_tickets", False))
    base["options"]["transcript_on_close"] = bool(base["options"].get("transcript_on_close", True))

    for key, value in list(base["texts"].items()):
        base["texts"][key] = truncate(str(value or ""), 1800, suffix="")

    base["report_types"] = normalize_report_types(raw.get("report_types") or base.get("report_types"))
    try:
        base["next_ticket_number"] = max(1, int(raw.get("next_ticket_number") or 1))
    except Exception:
        base["next_ticket_number"] = 1

    active_tickets = raw.get("active_tickets") or []
    normalized_active: list[dict[str, Any]] = []
    if isinstance(active_tickets, list):
        seen_channels: set[int] = set()
        for item in active_tickets:
            if not isinstance(item, dict):
                continue
            channel_id = int(item.get("channel_id") or 0)
            user_id = int(item.get("user_id") or 0)
            if not channel_id or not user_id or channel_id in seen_channels:
                continue
            kind = str(item.get("kind") or "other")
            if kind not in {"partnership", "report", "other"}:
                kind = "other"
            seen_channels.add(channel_id)
            normalized_active.append({
                "ticket_id": int(item.get("ticket_id") or 0),
                "channel_id": channel_id,
                "control_message_id": int(item.get("control_message_id") or 0),
                "user_id": user_id,
                "kind": kind,
                "created_at": str(item.get("created_at") or ""),
                "label": str(item.get("label") or PUBLIC_OPTIONS.get(kind, {}).get("label") or kind),
            })
    base["active_tickets"] = normalized_active[-300:]
    return base


def slugify_channel_part(value: object, *, fallback: str = "ticket") -> str:
    text = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return truncate(ascii_text or fallback, 48, suffix="").strip("-") or fallback


def is_staff(member: discord.Member | None, cfg: dict[str, Any] | None = None) -> bool:
    if member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms and (perms.administrator or perms.manage_guild or perms.manage_channels):
        return True
    cfg = cfg or {}
    roles_cfg = cfg.get("roles") or {}
    allowed = {int(roles_cfg.get(key) or 0) for key in roles_cfg}
    allowed.discard(0)
    if not allowed:
        return False
    return any(int(getattr(role, "id", 0) or 0) in allowed for role in getattr(member, "roles", []) or [])


def member_display(member: discord.abc.User | discord.Member | None) -> str:
    if member is None:
        return "desconhecido"
    mention = getattr(member, "mention", None)
    if mention:
        return str(mention)
    return str(member)
