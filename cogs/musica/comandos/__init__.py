"""Comandos e fluxos de interação do domínio de música."""

from .base import BaseComandosMusica
from .configuracoes import FluxoConfiguracoes
from .controle import FluxoControle
from .fila import FluxoFila
from .tocar import FluxoTocar

__all__ = [
    "BaseComandosMusica",
    "FluxoConfiguracoes",
    "FluxoControle",
    "FluxoFila",
    "FluxoTocar",
]
