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
    should_process_tts: bool
    should_dispatch_prefix_command: bool
    guild_defaults: dict[str, Any]
    forced_engine: str | None = None
    active_prefix: str | None = None
    prefix_command: Any | None = None


async def analyze_message_for_tts(cog: Any, message: Any) -> MessageGateDecision:
    if not getattr(config, "TTS_ENABLED", True):
        return MessageGateDecision(False, False, {})
    if getattr(getattr(message, "author", None), "bot", False):
        return MessageGateDecision(False, False, {})
    if getattr(message, "guild", None) is None:
        return MessageGateDecision(False, False, {})
    if not getattr(message, "content", None):
        return MessageGateDecision(False, False, {})

    db = cog._get_db()
    guild_defaults = await cog._maybe_await(db.get_guild_tts_defaults(message.guild.id)) if db else {}
    guild_defaults = guild_defaults or {}
    routing = build_prefix_routing_config(
        guild_defaults,
        bot_prefix_default=str(getattr(config, "BOT_PREFIX", "_") or "_"),
        gcloud_prefix_default=str(getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'") or "'"),
    )

    prefix_command = match_prefix_control_command(message.content, routing.bot_prefix)
    if prefix_command is not None:
        return MessageGateDecision(
            should_process_tts=False,
            should_dispatch_prefix_command=True,
            guild_defaults=guild_defaults,
            prefix_command=prefix_command,
        )

    forced_engine, active_prefix = match_engine_prefix(
        message.content,
        edge_prefix=routing.edge_prefix,
        gtts_prefix=routing.gtts_prefix,
        gcloud_prefix=routing.gcloud_prefix,
    )
    if not forced_engine or not active_prefix:
        return MessageGateDecision(False, False, guild_defaults)

    return MessageGateDecision(
        should_process_tts=True,
        should_dispatch_prefix_command=False,
        guild_defaults=guild_defaults,
        forced_engine=forced_engine,
        active_prefix=active_prefix,
    )
