"""Chatbot cog — conversação IA via profiles gerenciados por staff.

Arquitetura e decisões em `cog.py`. Este módulo só expõe o setup
do discord.py extension.

Carregamento: este pacote é automaticamente importado pelo bot.py como
`cogs.chatbot` (por ter __init__.py e estar em cogs/).
"""
from __future__ import annotations

# O setup efetivo fica em cog.py pra não carregar o discord.py antes do tempo
# quando profiles.py/memory.py/providers.py são importados standalone (testes).
async def setup(bot):
    from .cog import setup as _setup
    await _setup(bot)
