"""DevAI — auditor automático de logs e gerador seguro de patches.

Carregado automaticamente pelo bot.py por ser um pacote dentro de cogs/.
O setup real fica em cog.py para manter imports pesados isolados.
"""
from __future__ import annotations


async def setup(bot):
    from .cog import setup as _setup
    await _setup(bot)
