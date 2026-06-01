from __future__ import annotations

import re
import uuid
from typing import Any

import discord

from ..config.defaults import *


__all__ = [
    "_trim",
    "_channel_mention",
    "_role_list",
    "_user_mention",
    "_parse_hex",
    "_color_from_hex",
    "_clean_url",
    "_image_mode",
    "_media_mode",
    "_has_custom_embed",
    "_clean_invite_code",
    "_status_label",
    "_template_changed",
    "_safe_webhook_name",
    "_new_rule_id",
    "_make_notice_view",
    "_advanced_modal_supported",
    "_modal_values",
    "_modal_value",
    "_id_from_text",
]

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
    return f"<#{cid}>" if cid else "não escolhido"


def _role_list(guild: discord.Guild | None, role_ids: list[int], *, empty: str = "nenhum") -> str:
    values: list[str] = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id)) if guild is not None else None
        values.append(role.mention if role is not None else f"cargo {role_id}")
    return ", ".join(values) if values else empty


def _user_mention(user_id: int | str | None) -> str:
    try:
        uid = int(user_id or 0)
    except Exception:
        uid = 0
    return f"<@{uid}>" if uid else "não escolhido"


def _parse_hex(value: Any, fallback: str = DEFAULT_ACCENT) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    if not HEX_RE.fullmatch(raw):
        raw = fallback
    raw = raw.upper()
    if not raw.startswith("#"):
        raw = f"#{raw}"
    return raw


def _color_from_hex(value: Any, fallback: str = DEFAULT_ACCENT) -> discord.Color:
    raw = _parse_hex(value, fallback).lstrip("#")
    try:
        return discord.Color(int(raw, 16))
    except Exception:
        return discord.Color.blurple()


def _clean_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("attachment://"):
        return raw[:1000]
    if not URL_RE.fullmatch(raw):
        return ""
    return raw[:1000]


def _image_mode(value: Any, *, fallback: str = "none") -> str:
    mode = str(value or fallback).strip().lower()
    return mode if mode in EMBED_MAIN_IMAGE_MODE_LABELS else fallback


def _media_mode(value: Any, *, fallback: str = "custom") -> str:
    mode = str(value or fallback).strip().lower()
    return mode if mode in MEDIA_MODE_LABELS else fallback


def _has_custom_embed(embed: dict[str, Any] | None) -> bool:
    data = dict(embed or {}) if isinstance(embed, dict) else {}
    for key, default in DEFAULT_EMBED.items():
        if str(data.get(key) or "") != str(default or ""):
            return True
    return False


def _clean_invite_code(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = INVITE_CODE_RE.match(raw)
    if not match:
        return ""
    return match.group(1)[:64]


def _status_label(value: bool) -> str:
    return "Ligado" if value else "Desligado"


def _template_changed(cfg: dict[str, Any]) -> bool:
    public = dict(cfg.get("public") or {})
    return any(str(public.get(k) or "") != str(DEFAULT_PUBLIC.get(k) or "") for k in DEFAULT_PUBLIC)


def _safe_webhook_name(value: Any, fallback: str = DEFAULT_WEBHOOK_NAME) -> str:
    raw = str(value or "").strip() or fallback
    raw = re.sub(r"\s+", " ", raw)
    raw = raw.replace("discord", "disc0rd").replace("Discord", "Disc0rd")
    raw = raw.replace("clyde", "cly.de").replace("Clyde", "Cly.de")
    return raw[:80] or fallback


def _new_rule_id() -> str:
    return uuid.uuid4().hex[:10]


def _make_notice_view(title: str, body: str | list[str], *, ok: bool = True) -> discord.ui.LayoutView:
    body_text = "\n".join(str(item) for item in body) if isinstance(body, list) else str(body or "")
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(_trim(f"# {title}\n{body_text}")),
        accent_color=discord.Color.green() if ok else discord.Color.red(),
    ))
    return view



def _advanced_modal_supported(*components: str) -> bool:
    needed = components or ("Label", "RadioGroup", "CheckboxGroup")
    return all(hasattr(discord.ui, name) for name in needed)


def _modal_values(component: Any) -> list[str]:
    values = getattr(component, "values", None)
    if values is None:
        value = getattr(component, "value", None)
        if value is None:
            return []
        return [str(value)]
    if isinstance(values, (str, int)):
        return [str(values)]
    try:
        return [str(item) for item in values if str(item)]
    except TypeError:
        return [str(values)] if values else []


def _modal_value(component: Any, default: str = "") -> str:
    values = _modal_values(component)
    return values[0] if values else default


def _id_from_text(value: Any) -> int:
    raw = str(value or "").strip()
    match = re.search(r"(\d{15,25})", raw)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0
