from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

WEBHOOK_NAME = "Atendimento"

# Webhook é por canal. Mantemos apenas um cache leve em memória para não editar
# o mesmo webhook em toda mensagem, mas cada canal continua tendo o próprio
# webhook visual. Ao reiniciar, o primeiro envio em cada canal sincroniza de novo.
_SYNCED_WEBHOOK_IDS: set[int] = set()


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


async def _sync_webhook_identity(webhook: discord.Webhook, guild: discord.Guild | None) -> discord.Webhook:
    """Sincroniza o webhook visual com a identidade do servidor.

    O Discord permite `username`/`avatar_url` por envio, mas alguns clientes
    móveis cacheiam/ignoram a foto por mensagem quando a mensagem usa
    Components V2. Por isso o webhook do canal também precisa ter a foto do
    servidor como avatar padrão.

    Regressão corrigida: antes a sincronização parava quando o webhook já tinha
    qualquer avatar. Em canais que já tinham um webhook antigo, o nome do
    servidor aparecia, mas a foto não. Agora cada webhook de cada canal é
    sincronizado pelo menos uma vez por processo.
    """
    wid = int(getattr(webhook, "id", 0) or 0)
    if wid and wid in _SYNCED_WEBHOOK_IDS:
        return webhook

    edit_kwargs: dict[str, Any] = {
        "name": WEBHOOK_NAME,
        "reason": "Sincronizar webhook visual do sistema de atendimento",
    }
    avatar = await _guild_avatar_bytes(guild)
    if avatar:
        edit_kwargs["avatar"] = avatar

    try:
        edited = await webhook.edit(**edit_kwargs)
        if wid:
            _SYNCED_WEBHOOK_IDS.add(wid)
        return edited or webhook
    except Exception as exc:
        # Se não der para editar o avatar padrão, o envio ainda tentará usar
        # avatar_url por mensagem. Não quebramos o fluxo por aparência.
        log.debug(
            "[tickets] não consegui sincronizar webhook gid=%s ch=%s wh=%s: %r",
            getattr(guild, "id", 0),
            getattr(webhook, "channel_id", 0),
            wid,
            exc,
        )
        return webhook


async def _find_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook | None:
    guild = channel.guild
    me = getattr(guild, "me", None)
    if me is None:
        return None
    perms = channel.permissions_for(me)
    if not getattr(perms, "manage_webhooks", False):
        log.debug("[tickets] sem permissão Gerenciar Webhooks ch=%s", getattr(channel, "id", 0))
        return None

    # Webhooks são por canal: nunca reutilize um webhook encontrado em outro
    # canal. `channel.webhooks()` normalmente já filtra, mas a checagem deixa a
    # intenção explícita e protege contra mudanças de lib/cache.
    channel_id = int(getattr(channel, "id", 0) or 0)
    try:
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            webhook_channel_id = int(getattr(webhook, "channel_id", 0) or 0)
            if webhook_channel_id and webhook_channel_id != channel_id:
                continue
            if getattr(webhook, "name", "") == WEBHOOK_NAME and getattr(webhook, "token", None):
                return await _sync_webhook_identity(webhook, guild)
    except Exception as exc:
        log.debug("[tickets] não consegui listar webhooks ch=%s: %r", channel_id, exc)

    try:
        create_kwargs: dict[str, Any] = {
            "name": WEBHOOK_NAME,
            "reason": "Webhook visual do sistema de atendimento",
        }
        avatar = await _guild_avatar_bytes(guild)
        if avatar:
            create_kwargs["avatar"] = avatar
        webhook = await channel.create_webhook(**create_kwargs)
        return await _sync_webhook_identity(webhook, guild)
    except Exception as exc:
        log.debug("[tickets] não consegui criar webhook ch=%s: %r", channel_id, exc)
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
