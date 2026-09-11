"""Compatibilidade para o antigo caminho do gate de mensagens do TTS.

A implementação canônica vive em :mod:`cogs.tts.mensagens.triagem`.
"""
from __future__ import annotations

# Mantido aqui porque testes e integrações existentes fazem patch de
# ``cogs.tts.utils.message_gate.config``. É o mesmo módulo compartilhado pela
# implementação canônica, portanto o contrato de patch continua válido.
import config

from ..mensagens.triagem import (
    DecisaoTriagemMensagem as MessageGateDecision,
    analisar_mensagem_para_tts as analyze_message_for_tts,
)

__all__ = ["MessageGateDecision", "analyze_message_for_tts"]
