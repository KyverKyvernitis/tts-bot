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


def _clean_prefix(value: object) -> str:
    return str(value or "").strip()[:8]


def _unique_prefixes(*values: object) -> list[str]:
    prefixes: list[str] = []
    for value in values:
        prefix = _clean_prefix(value)
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


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

    # Prefixo experimental legado (%): agora testa Android TTS nativo por padrão.
    # Guild 0 libera o prefixo em todos os servidores; use TTS_PIPER_EXPERIMENT_GUILD_ID se quiser restringir.
    piper_enabled = bool(getattr(config, "TTS_PIPER_EXPERIMENT_ENABLED", False))
    piper_prefix = str(getattr(config, "TTS_PIPER_EXPERIMENT_PREFIX", "/") or "/")
    piper_guild_id = int(getattr(config, "TTS_PIPER_EXPERIMENT_GUILD_ID", 0) or 0)
    if piper_enabled and piper_prefix and message.content.startswith(piper_prefix):
        guild_ok = piper_guild_id <= 0 or int(getattr(message.guild, "id", 0) or 0) == piper_guild_id
        spoken = message.content[len(piper_prefix):].strip()
        if guild_ok and spoken:
            return MessageGateDecision(
                should_process_tts=True,
                should_dispatch_prefix_command=False,
                guild_defaults=guild_defaults,
                forced_engine=str(getattr(config, "TTS_PIPER_EXPERIMENT_ENGINE", "android_native") or "android_native").strip().lower().replace("-", "_"),
                active_prefix=piper_prefix,
                reason="native_tts_experimental_prefix_matched",
            )

    # Compatibilidade com o prefixo antigo único. Servidores antigos podem ter
    # só `tts_prefix=,`; nesse caso o split novo cria gTTS=`,` e Edge=`,` ao
    # mesmo tempo, e a ordem antiga acabava forçando Edge silenciosamente.
    # Quando há conflito com o prefixo legado, removemos só o prefixo da fala e
    # deixamos `resolve_tts()` escolher a engine efetiva do usuário/servidor.
    legacy_prefixes = _unique_prefixes(
        guild_defaults.get("tts_prefix"),
        getattr(config, "TTS_PREFIX", ""),
    )
    for legacy_prefix in legacy_prefixes:
        if (
            legacy_prefix
            and legacy_prefix != routing.bot_prefix
            and message.content.startswith(legacy_prefix)
            and legacy_prefix in {routing.gtts_prefix, routing.edge_prefix, routing.gcloud_prefix}
            and len({routing.gtts_prefix, routing.edge_prefix, routing.gcloud_prefix}) < 3
        ):
            return MessageGateDecision(
                should_process_tts=True,
                should_dispatch_prefix_command=False,
                guild_defaults=guild_defaults,
                forced_engine=None,
                active_prefix=legacy_prefix,
                reason="legacy_tts_prefix_matched",
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
        for legacy_prefix in legacy_prefixes:
            if legacy_prefix and legacy_prefix != routing.bot_prefix and message.content.startswith(legacy_prefix):
                return MessageGateDecision(
                    should_process_tts=True,
                    should_dispatch_prefix_command=False,
                    guild_defaults=guild_defaults,
                    forced_engine=None,
                    active_prefix=legacy_prefix,
                    reason="legacy_tts_prefix_matched",
                )
        return MessageGateDecision(False, False, guild_defaults, reason="no_engine_prefix")

    return MessageGateDecision(
        should_process_tts=True,
        should_dispatch_prefix_command=False,
        guild_defaults=guild_defaults,
        forced_engine=forced_engine,
        active_prefix=active_prefix,
        reason="tts_prefix_matched",
    )
