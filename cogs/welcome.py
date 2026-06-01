from __future__ import annotations

# Compatibilidade: a extensão agora vive no pacote cogs/welcome/.
# O import de `cogs.welcome` usa o pacote com __init__.py; este arquivo permanece leve
# para patches e leituras antigas que esperem cogs/welcome.py.
from .welcome.cog import WelcomeCog, setup

__all__ = ["WelcomeCog", "setup"]
