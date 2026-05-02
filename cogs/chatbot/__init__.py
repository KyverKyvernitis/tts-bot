"""Chatbot cog — conversação IA via profiles gerenciados por staff.

Arquitetura e decisões em `cog.py`. Este módulo só expõe o setup
do discord.py extension.

Carregamento: este pacote é automaticamente importado pelo bot.py como
`cogs.chatbot` (por ter __init__.py e estar em cogs/).

Filtro de log de voz: instalamos um logging.Filter no setup pra suprimir
tracebacks ruidosos de `ConnectionClosed` com códigos 1000/1001/1006 —
esses são desconexões transitórias que o discord.py JÁ trata via reconexão
automática, mas que a biblioteca ainda registra como ERROR com traceback
completo, poluindo os logs sem motivo.
"""
from __future__ import annotations

import logging

from discord import errors

# Códigos de WebSocket close que são "esperados" e tratados pela biblioteca:
#   1000 = normal closure (desconexão limpa)
#   1001 = going away (servidor reiniciou ou cliente fechou)
#   1006 = abnormal closure (queda de rede transitória)
# A reconexão automática do discord.py cuida de todos esses. O traceback
# associado é só ruído — o bot vai voltar sozinho em segundos.
_RECOVERABLE_VOICE_CLOSE_CODES = frozenset({1000, 1001, 1006})

# Loggers do discord.py que emitem ConnectionClosed em desconexões de voz.
# Mantemos a lista explícita (em vez de prefix-match em "discord.") pra não
# silenciar acidentalmente outros componentes — só queremos o ruído de voz.
_VOICE_LOGGER_NAMES = frozenset({
    "discord.voice_state",
    "discord.voice_client",
    "discord.gateway",
})


class VoiceConnectionFilter(logging.Filter):
    """Suprime tracebacks de ConnectionClosed recuperáveis nos loggers de voz.

    Substitui o log ERROR (com traceback) por um log INFO com a mensagem
    enxuta. Não toca em closes inesperados (4xxx — políticas, kicks, etc).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name not in _VOICE_LOGGER_NAMES:
            return True
        if not record.exc_info:
            return True
        _exc_type, exc_value, _tb = record.exc_info
        if not isinstance(exc_value, errors.ConnectionClosed):
            return True
        code = getattr(exc_value, "code", None)
        if code not in _RECOVERABLE_VOICE_CLOSE_CODES:
            return True
        # É um close recuperável — emite INFO no lugar do ERROR original
        # e suprime o registro original (com traceback completo).
        logging.getLogger(record.name).info(
            "Voice WS close recuperável (code=%s): %s — discord.py vai reconectar automaticamente",
            code, exc_value,
        )
        return False


def _install_voice_filter() -> None:
    """Instala o filtro nos loggers de voz, idempotente."""
    flt = VoiceConnectionFilter()
    for name in _VOICE_LOGGER_NAMES:
        logger = logging.getLogger(name)
        # Evita instalar 2x se setup() rodar de novo (reload de extensão)
        if not any(isinstance(f, VoiceConnectionFilter) for f in logger.filters):
            logger.addFilter(flt)


async def setup(bot):
    """Setup do extension. Instala filtro de log + carrega o cog."""
    _install_voice_filter()
    from .cog import setup as _setup
    await _setup(bot)
