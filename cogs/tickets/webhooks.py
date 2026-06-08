from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

WEBHOOK_NAME = "Atendimento"


async def _guild_avatar_bytes(guild: discord.Guild | None) -> bytes | None:
    if guild is None:
        return None
    icon = getattr(guild, "icon", None)
    if icon is None:
        return None
    try:
        return await icon.read()
    except Exception as exc:
        log.debug("[tickets] não consegui ler ícone do servidor gid=%s: %r", getattr(guild, "id", 0), exc)
        return None


def _guild_avatar_url(guild: discord.Guild | None) -> str | None:
    if guild is None:
        return None
    icon = getattr(guild, "icon", None)
    if icon is None:
        return None
    try:
        return str(icon.url)
    except Exception:
        return None


def _guild_webhook_name(guild: discord.Guild | None) -> str:
    if guild is None:
        return WEBHOOK_NAME
    name = str(getattr(guild, "name", "") or WEBHOOK_NAME).strip()
    return name[:80] or WEBHOOK_NAME


async def _sync_webhook_avatar(webhook: discord.Webhook, guild: discord.Guild | None) -> discord.Webhook:
    """Garante uma foto padrão no webhook visual.

    O envio ainda usa username/avatar_url por mensagem, mas alguns clientes do
    Discord cacheiam a foto padrão do webhook quando a mensagem tem Components
    V2. Atualizar o avatar do próprio webhook evita a regressão em que o nome do
    servidor aparece, mas a foto fica vazia/antiga.
    """
    if guild is None or getattr(guild, "icon", None) is None:
        return webhook
    try:
        if getattr(webhook, "avatar", None) is not None:
            return webhook
    except Exception:
        pass
    avatar = await _guild_avatar_bytes(guild)
    if not avatar:
        return webhook
    try:
        edited = await webhook.edit(
            name=WEBHOOK_NAME,
            avatar=avatar,
            reason="Atualizar foto do webhook visual do sistema de atendimento",
        )
        return edited or webhook
    except Exception as exc:
        log.debug("[tickets] não consegui atualizar avatar do webhook gid=%s wh=%s: %r", getattr(guild, "id", 0), getattr(webhook, "id", 0), exc)
        return webhook


async def _find_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook | None:
    guild = channel.guild
    me = getattr(guild, "me", None)
    if me is None:
        return None
    perms = channel.permissions_for(me)
    if not getattr(perms, "manage_webhooks", False):
        return None
    try:
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            if getattr(webhook, "name", "") == WEBHOOK_NAME and getattr(webhook, "token", None):
                return await _sync_webhook_avatar(webhook, guild)
    except Exception as exc:
        log.debug("[tickets] não consegui listar webhooks ch=%s: %r", channel.id, exc)
    try:
        webhook = await channel.create_webhook(name=WEBHOOK_NAME, reason="Webhook visual do sistema de atendimento")
        return await _sync_webhook_avatar(webhook, guild)
    except Exception as exc:
        log.debug("[tickets] não consegui criar webhook ch=%s: %r", channel.id, exc)
        return None


async def send_with_server_identity(
    cfg: dict[str, Any],
    channel: discord.abc.Messageable,
    *,
    content: str | None = None,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    file: discord.File | None = None,
    wait: bool = True,
) -> discord.Message | None:
    """Envia como webhook com nome/foto do servidor quando habilitado.

    Se a lib, a permissão ou o Discord recusarem webhook com componentes, cai
    automaticamente para channel.send. Assim o fluxo nunca para por causa da
    opção visual.
    """
    guild = getattr(channel, "guild", None)
    use_webhook = bool((cfg.get("options") or {}).get("use_server_webhook", False))
    if use_webhook and isinstance(channel, discord.TextChannel):
        webhook = await _find_or_create_webhook(channel)
        if webhook is not None:
            kwargs: dict[str, Any] = {
                "username": _guild_webhook_name(guild),
                "wait": wait,
            }
            avatar_url = _guild_avatar_url(guild)
            if avatar_url:
                kwargs["avatar_url"] = avatar_url
            if content is not None:
                kwargs["content"] = content
            if view is not None:
                kwargs["view"] = view
            if file is not None:
                kwargs["file"] = file
            try:
                return await webhook.send(**kwargs)
            except Exception as exc:
                log.debug("[tickets] envio por webhook falhou ch=%s: %r", getattr(channel, "id", 0), exc)

    send_kwargs: dict[str, Any] = {}
    if content is not None:
        send_kwargs["content"] = content
    if view is not None:
        send_kwargs["view"] = view
    if file is not None:
        send_kwargs["file"] = file
    try:
        return await channel.send(**send_kwargs)
    except Exception:
        log.exception("[tickets] envio normal falhou ch=%s", getattr(channel, "id", 0))
        return None
