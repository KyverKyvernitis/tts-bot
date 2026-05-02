"""Forms cog — sistema de formulário com botão persistente.

Triggers (mensagem inteira, case-insensitive, staff-only):
- form / formulário / formulario  → recria mensagem do form no canal de form;
                                     ou inicia setup wizard se canais não foram
                                     configurados ainda.
- c                                → abre painel de customização (apenas se
                                     setup já foi feito; funciona no canal de
                                     form OU no canal de respostas).

Detalhes em `cog.py`. Este módulo só expõe o setup do extension.
"""
from __future__ import annotations


async def setup(bot):
    from .cog import setup as _setup
    await _setup(bot)
