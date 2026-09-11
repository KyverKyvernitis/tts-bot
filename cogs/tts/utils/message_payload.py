"""Compatibilidade para o antigo caminho de preparação de payload do TTS."""
from __future__ import annotations

from ..mensagens.preparacao import (
    PayloadTTSMensagem as MessageTTSPayload,
    preparar_payload_tts_mensagem as build_message_tts_payload,
)

__all__ = ["MessageTTSPayload", "build_message_tts_payload"]
