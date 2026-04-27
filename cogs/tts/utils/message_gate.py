"""Gate de mensagem do TTS — decide se a mensagem entra no pipeline.

Centraliza todos os filtros (TTS desligado, autor é bot, conteúdo vazio,
prefixo casado etc) num lugar só, retornando uma decisão estruturada que o
cog principal só interpreta. Evita ifs espalhados pelo `on_message`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import config

from ..prefix import (
    build_prefix_routing_config,
    match_engine_prefix,
    match_prefix_control_command,
)


@dataclass(frozen=True)
class MessageGateDecision:
    # Resultado do gate. Os flags são exclusivos: ou processa TTS, ou despacha
    # comando prefixado, ou ignora — nunca os três ao mesmo tempo.
    should_process_tts: bool
    should_dispatch_prefix_command: bool
    guild_defaults: dict[str, Any]
    forced_engine: str | None = None
    active_prefix: str | None = None
    prefix_command: Any | None = None
    reason: str = ""


async def analyze_message_for_tts(cog: Any, message: Any) -> MessageGateDecision:
    # Filtros baratos primeiro — evita tocar o DB por mensagem de bot ou DM.
    if not getattr(config, "TTS_ENABLED", True):
        return MessageGateDecision(False, False, {}, reason="tts_disabled")
    if getattr(getattr(message, "author", None), "bot", False):
        return MessageGateDecision(False, False, {}, reason="author_bot")
    if getattr(message, "guild", None) is None:
        return MessageGateDecision(False, False, {}, reason="no_guild")
    if not getattr(message, "content", None):
        return MessageGateDecision(False, False, {}, reason="empty_content")

    # Defaults do servidor e roteamento de prefixos vêm do DB.
    db = cog._get_db()
    guild_defaults = await cog._maybe_await(db.get_guild_tts_defaults(message.guild.id)) if db else {}
    guild_defaults = guild_defaults or {}
    routing = build_prefix_routing_config(
        guild_defaults,
        bot_prefix_default=str(getattr(config, "BOT_PREFIX", "_") or "_"),
        gcloud_prefix_default=str(getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'") or "'"),
    )

    # Comandos `_join`, `_leave`, `_clear`, etc passam por aqui antes de qualquer
    # tentativa de TTS — assim a mensagem `_join` não é falada.
    prefix_command = match_prefix_control_command(message.content, routing.bot_prefix)
    if prefix_command is not None:
        return MessageGateDecision(
            should_process_tts=False,
            should_dispatch_prefix_command=True,
            guild_defaults=guild_defaults,
            prefix_command=prefix_command,
            reason="prefix_command",
        )

    # Casa um dos três prefixos de fala (gTTS / Edge / gcloud). Se nenhum casar,
    # a mensagem é texto comum e o gate ignora.
    forced_engine, active_prefix = match_engine_prefix(
        message.content,
        edge_prefix=routing.edge_prefix,
        gtts_prefix=routing.gtts_prefix,
        gcloud_prefix=routing.gcloud_prefix,
    )
    if not forced_engine or not active_prefix:
        return MessageGateDecision(False, False, guild_defaults, reason="no_engine_prefix")

    return MessageGateDecision(
        should_process_tts=True,
        should_dispatch_prefix_command=False,
        guild_defaults=guild_defaults,
        forced_engine=forced_engine,
        active_prefix=active_prefix,
        reason="tts_prefix_matched",
    )
