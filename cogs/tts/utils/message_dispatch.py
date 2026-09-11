"""Compatibilidade para o antigo caminho de despacho de mensagens do TTS.

O wrapper preserva inclusive o contrato de monkeypatch do builder legado.
"""
from __future__ import annotations

from typing import Any

from ..mensagens.despacho import (
    ResultadoDespachoMensagem as MessageDispatchResult,
    despachar_mensagem_tts,
)
from .message_payload import MessageTTSPayload, build_message_tts_payload


async def dispatch_message_tts(
    cog: Any,
    message: Any,
    *,
    guild_defaults: dict | None,
    active_prefix: str,
    forced_engine: str,
) -> MessageDispatchResult:
    return await despachar_mensagem_tts(
        cog,
        message,
        guild_defaults=guild_defaults,
        active_prefix=active_prefix,
        forced_engine=forced_engine,
        construir_payload=build_message_tts_payload,
    )


__all__ = [
    "MessageDispatchResult",
    "MessageTTSPayload",
    "build_message_tts_payload",
    "dispatch_message_tts",
]
