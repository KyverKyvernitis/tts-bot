"""Chatbot cog — conversação IA via profiles gerenciados por staff.

Arquitetura e decisões em `cog.py`. Este módulo só expõe o setup
do discord.py extension.

Carregamento: este pacote é automaticamente importado pelo bot.py como
`cogs.chatbot` (por ter __init__.py e estar em cogs/).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# O setup efetivo fica em cog.py pra não carregar o discord.py antes do tempo
# quando profiles.py/memory.py/providers.py são importados standalone (testes).
async def setup(bot):
    try:
        from .cog import setup as _setup
    except ModuleNotFoundError as e:
        # Algumas bases podem vir sem o arquivo principal do cog ainda.
        # Nesse caso, não derrubamos o bot inteiro por causa de um pacote
        # incompleto: apenas pulamos o carregamento do chatbot.
        if e.name == f"{__name__}.cog":
            log.warning(
                "Chatbot ignorado: arquivo principal %s.cog não foi encontrado.",
                __name__,
            )
            return
        raise

    await _setup(bot)
