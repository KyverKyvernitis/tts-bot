"""Presença de voz atual do Gateway, separada das capacidades de reprodução.

Conhecer uma call não significa escutar seu áudio ou estar autorizado a entrar.
Este snapshot nunca consulta REST nem cria/move uma conexão de voz.
"""
from __future__ import annotations

import re

import discord


def _label(value):
    return " ".join(re.sub(r"[\x00-\x1f\x7f<>`@]", " ", str(value or "")).split())[:80]


def _gateway_known(bot, guild):
    intents = getattr(bot, "intents", None)
    return not bool(getattr(guild, "unavailable", False)) and bool(getattr(intents, "voice_states", True))


def member_voice_snapshot(bot, guild, member, *, viewer=None):
    """Lê Member.voice, cuja propriedade usa os eventos atuais do servidor."""
    known = _gateway_known(bot, guild) and member is not None
    channel = getattr(getattr(member, "voice", None), "channel", None) if known else None
    connected = channel is not None if known else None
    result = {"known": known, "connected": connected, "channel_id": None,
              "channel_name": None, "channel_mention": None, "channel_type": None,
              "supported": False}
    if channel is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return result
    try:
        visible = viewer is not None and bool(getattr(channel.permissions_for(viewer), "view_channel", False))
    except (AttributeError, TypeError):
        visible = False
    if not visible:
        return result
    result.update(channel_id=str(channel.id), channel_name=_label(channel.name),
                  channel_mention=f"<#{channel.id}>",
                  channel_type="stage" if isinstance(channel, discord.StageChannel) else "voice",
                  supported=isinstance(channel, discord.VoiceChannel))
    return result


def build_voice_snapshot(bot, guild, requester):
    author = member_voice_snapshot(bot, guild, requester, viewer=requester)
    bot_member = getattr(guild, "me", None)
    own = member_voice_snapshot(bot, guild, bot_member, viewer=requester)
    same = bool(author["known"] and own["known"] and author["channel_id"]
                and author["channel_id"] == own["channel_id"])
    return {"author": author, "bot": own, "same_channel": same, "can_listen": False}
