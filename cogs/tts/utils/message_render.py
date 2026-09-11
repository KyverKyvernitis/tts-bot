"""Compatibilidade para o antigo caminho de renderização de mensagens do TTS."""
from __future__ import annotations

from ..mensagens.renderizacao import (
    anexar_descricoes_tts as append_tts_descriptions,
    renderizar_texto_tts_mensagem as render_message_tts_text,
)

__all__ = ["append_tts_descriptions", "render_message_tts_text"]
