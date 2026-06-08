from __future__ import annotations

from typing import Any

import discord

from .constants import default_ticket_config

# Permissões expostas no editor. A lista é limitada de propósito para evitar
# permissões perigosas/irrelevantes no fluxo de ticket.
TICKET_PERMISSION_LABELS: dict[str, str] = {
    "view_channel": "Ver canal",
    "send_messages": "Enviar mensagens",
    "read_message_history": "Ler histórico",
    "attach_files": "Anexar arquivos",
    "embed_links": "Incorporar links",
    "add_reactions": "Adicionar reações",
    "manage_messages": "Gerenciar mensagens",
    "manage_channels": "Gerenciar canal",
    "mention_everyone": "Mencionar @everyone/@here",
}

SCOPE_LABELS: dict[str, str] = {
    "everyone": "@everyone",
    "staff": "cargos staff",
    "creator": "autor do ticket",
}


def default_permissions_config() -> dict[str, dict[str, bool]]:
    cfg = default_ticket_config()
    return {scope: dict(values) for scope, values in (cfg.get("permissions") or {}).items()}


def scope_permissions(cfg: dict[str, Any], scope: str) -> dict[str, bool]:
    defaults = default_permissions_config().get(scope, {})
    raw = (cfg.get("permissions") or {}).get(scope) if isinstance(cfg.get("permissions"), dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    result = dict(defaults)
    result.update({str(key): bool(value) for key, value in raw.items() if str(key) in defaults})
    return result


def set_scope_permissions(cfg: dict[str, Any], scope: str, values: dict[str, bool]) -> None:
    cfg.setdefault("permissions", {})
    defaults = default_permissions_config().get(scope, {})
    cfg["permissions"][scope] = {
        key: bool(values.get(key, defaults.get(key, False)))
        for key in defaults
    }


def reset_permissions(cfg: dict[str, Any]) -> None:
    cfg["permissions"] = default_permissions_config()


def permission_summary(cfg: dict[str, Any]) -> str:
    everyone = scope_permissions(cfg, "everyone")
    staff = scope_permissions(cfg, "staff")
    creator = scope_permissions(cfg, "creator")
    everyone_text = "privado" if not everyone.get("view_channel") else "pode ver"
    staff_text = "pode atender" if staff.get("view_channel") and staff.get("send_messages") else "limitado"
    creator_text = "pode conversar" if creator.get("view_channel") and creator.get("send_messages") else "limitado"
    return f"@everyone: {everyone_text}\nStaff: {staff_text}\nAutor: {creator_text}"


def permission_overwrite_from_scope(cfg: dict[str, Any], scope: str) -> discord.PermissionOverwrite:
    values = scope_permissions(cfg, scope)
    # None deixaria herdar da categoria. Aqui usamos booleano explícito porque
    # ticket precisa ser previsível e privado por padrão.
    allowed_keys = {
        "view_channel",
        "send_messages",
        "read_message_history",
        "attach_files",
        "embed_links",
        "add_reactions",
        "manage_messages",
        "manage_channels",
        "mention_everyone",
    }
    payload = {key: bool(value) for key, value in values.items() if key in allowed_keys}
    return discord.PermissionOverwrite(**payload)
