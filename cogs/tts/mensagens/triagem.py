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
    has_speech_after_prefix,
    match_prefix_control_command,
)


@dataclass(frozen=True)
class DecisaoTriagemMensagem:
    # Resultado do gate. Os flags são exclusivos: ou processa TTS, ou despacha
    # comando prefixado, ou ignora — nunca os três ao mesmo tempo.
    should_process_tts: bool
    should_dispatch_prefix_command: bool
    guild_defaults: dict[str, Any]
    forced_engine: str | None = None
    active_prefix: str | None = None
    prefix_command: Any | None = None
    reason: str = ""


def _limpar_prefixo(value: object) -> str:
    return str(value or "").strip()[:8]


def _prefixos_unicos(*values: object) -> list[str]:
    prefixes: list[str] = []
    for value in values:
        prefix = _limpar_prefixo(value)
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


async def match_import_cd_message(cog: Any, message: Any):
    """Recognize CD imports independently of speech, including owner DMs.

    The suffix check keeps disabled-TTS and updater paths free of configuration
    reads for ordinary messages. Permission and attachment checks belong to the
    command handler; this helper never downloads or imports an attachment.
    """
    raw = str(getattr(message, "content", "") or "").strip()
    if not raw.casefold().endswith("importcd"):
        return None
    if getattr(getattr(message, "author", None), "bot", False):
        return None
    guard = getattr(getattr(cog, "bot", None), "antibot_should_block_message", None)
    if callable(guard) and bool(guard(message)):
        return None
    guild = getattr(message, "guild", None)
    defaults = {}
    if guild is not None:
        db = cog._get_db()
        defaults = (await cog._maybe_await(db.get_guild_tts_defaults(guild.id)) if db else {}) or {}
    prefix = str(defaults.get("bot_prefix") or getattr(config, "BOT_PREFIX", "_") or "_")
    command = match_prefix_control_command(raw, prefix)
    return command if command is not None and command.kind == "importcd" else None


async def analisar_mensagem_para_tts(cog: Any, message: Any) -> DecisaoTriagemMensagem:
    # Filtros baratos primeiro — evita tocar o DB por mensagem de bot ou DM.
    # Importar um CD administra dados do catálogo e não depende da síntese.
    # Os demais comandos e mensagens mantêm os filtros históricos abaixo.
    if (not getattr(config, "TTS_ENABLED", True)
            or getattr(message, "guild", None) is None):
        import_command = await match_import_cd_message(cog, message)
        if import_command is not None:
            return DecisaoTriagemMensagem(False, True, {},
                prefix_command=import_command, reason="prefix_command")
    if not getattr(config, "TTS_ENABLED", True):
        return DecisaoTriagemMensagem(False, False, {}, reason="tts_disabled")
    if getattr(getattr(message, "author", None), "bot", False):
        return DecisaoTriagemMensagem(False, False, {}, reason="author_bot")
    if getattr(message, "guild", None) is None:
        return DecisaoTriagemMensagem(False, False, {}, reason="no_guild")
    # A armadilha é despachada primeiro em bot.py, mas listeners de Cog são
    # executados pelo discord.py em paralelo. Este gate síncrono impede que uma
    # mensagem capturada alcance a fila de áudio mesmo nessa corrida.
    bot = getattr(cog, "bot", None)
    antibot_guard = getattr(bot, "antibot_should_block_message", None)
    if callable(antibot_guard) and bool(antibot_guard(message)):
        return DecisaoTriagemMensagem(False, False, {}, reason="antibot_guard")
    if not getattr(message, "content", None):
        return DecisaoTriagemMensagem(False, False, {}, reason="empty_content")

    # Defaults do servidor e roteamento de prefixos vêm do DB.
    db = cog._get_db()
    guild_defaults = await cog._maybe_await(db.get_guild_tts_defaults(message.guild.id)) if db else {}
    guild_defaults = guild_defaults or {}
    routing = build_prefix_routing_config(
        guild_defaults,
        bot_prefix_default=str(getattr(config, "BOT_PREFIX", "_") or "_"),
        atts_prefix_default=str(getattr(config, "TTS_ATTS_PREFIX", "%") or "%"),
        teto_prefix_default=str(getattr(config, "TTS_TETO_PREFIX", "'") or "'"),
    )

    # Comandos `_join`, `_leave`, `_clear`, etc passam por aqui antes de qualquer
    # tentativa de TTS — assim a mensagem `_join` não é falada.
    prefix_command = match_prefix_control_command(message.content, routing.bot_prefix)
    if prefix_command is not None:
        return DecisaoTriagemMensagem(
            should_process_tts=False,
            should_dispatch_prefix_command=True,
            guild_defaults=guild_defaults,
            prefix_command=prefix_command,
            reason="prefix_command",
        )

    # Comandos registrados na Bot, como base/status, pertencem a
    # process_commands. Apenas analisa o contexto: não executa callbacks nem
    # checks e evita falar o comando se um prefixo de voz também casar.
    get_context = getattr(bot, "get_context", None)
    if callable(get_context):
        context = await get_context(message)
        if bool(getattr(context, "valid", False)):
            return DecisaoTriagemMensagem(
                False, False, guild_defaults, reason="registered_bot_command"
            )

    if not bool(guild_defaults.get("enabled", True)):
        return DecisaoTriagemMensagem(False, False, guild_defaults, reason="tts_guild_disabled")

    # Compatibilidade com o prefixo antigo único. Servidores antigos podem ter
    # só `tts_prefix=,`; nesse caso o split novo cria gTTS=`,` e Edge=`,` ao
    # mesmo tempo, e a ordem antiga acabava forçando Edge silenciosamente.
    # Quando há conflito com o prefixo legado, removemos só o prefixo da fala e
    # deixamos `resolve_tts()` escolher a engine efetiva do usuário/servidor.
    legacy_prefixes = _prefixos_unicos(
        guild_defaults.get("tts_prefix"),
        getattr(config, "TTS_PREFIX", ""),
    )
    speech_prefixes = routing.speech_prefixes()
    unique_speech_prefixes = frozenset(speech_prefixes)
    for legacy_prefix in legacy_prefixes:
        if (
            legacy_prefix
            and legacy_prefix != routing.bot_prefix
            and message.content.startswith(legacy_prefix)
            and legacy_prefix in unique_speech_prefixes
            and len(unique_speech_prefixes) < len(speech_prefixes)
        ):
            if not has_speech_after_prefix(message, legacy_prefix):
                return DecisaoTriagemMensagem(False, False, guild_defaults, reason="prefix_without_speech")
            return DecisaoTriagemMensagem(
                should_process_tts=True,
                should_dispatch_prefix_command=False,
                guild_defaults=guild_defaults,
                forced_engine=None,
                active_prefix=legacy_prefix,
                reason="legacy_tts_prefix_matched",
            )

    # Casa um dos prefixos de fala (ATTS / Teto / gTTS / Edge). Se nenhum casar,
    # a mensagem é texto comum e o gate ignora.
    forced_engine, active_prefix = match_engine_prefix(
        message.content,
        atts_prefix=routing.atts_prefix,
        teto_prefix=routing.teto_prefix,
        edge_prefix=routing.edge_prefix,
        gtts_prefix=routing.gtts_prefix,
    )
    if forced_engine and active_prefix and not has_speech_after_prefix(message, active_prefix):
        return DecisaoTriagemMensagem(False, False, guild_defaults, reason="prefix_without_speech")
    if forced_engine == "teto" and not getattr(config, "TTS_TETO_ENABLED", True):
        return DecisaoTriagemMensagem(False, False, guild_defaults, forced_engine=forced_engine,
                                     active_prefix=active_prefix, reason="teto_disabled")
    if not forced_engine or not active_prefix:
        for legacy_prefix in legacy_prefixes:
            if legacy_prefix and legacy_prefix != routing.bot_prefix and message.content.startswith(legacy_prefix):
                if not has_speech_after_prefix(message, legacy_prefix):
                    return DecisaoTriagemMensagem(False, False, guild_defaults, reason="prefix_without_speech")
                return DecisaoTriagemMensagem(
                    should_process_tts=True,
                    should_dispatch_prefix_command=False,
                    guild_defaults=guild_defaults,
                    forced_engine=None,
                    active_prefix=legacy_prefix,
                    reason="legacy_tts_prefix_matched",
                )
        return DecisaoTriagemMensagem(False, False, guild_defaults, reason="no_engine_prefix")

    return DecisaoTriagemMensagem(
        should_process_tts=True,
        should_dispatch_prefix_command=False,
        guild_defaults=guild_defaults,
        forced_engine=forced_engine,
        active_prefix=active_prefix,
        reason="tts_prefix_matched",
    )
