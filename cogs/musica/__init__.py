"""Domínio de música do bot.

A VPS coordena comandos, metadados, estado e comunicação com o Phone Worker.
A reprodução musical real pertence ao Phone Worker. Os módulos em ``legado``
são compatibilidade temporária durante a retirada do player antigo da VPS.

Os símbolos de compatibilidade são carregados sob demanda para que importar o
pacote não inicialize Discord, rede ou o roteador de áudio.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AudioRouter",
    "LoopMode",
    "MusicTrack",
    "MusicError",
    "MusicExtractionError",
    "MusicPlaybackError",
]


def __getattr__(name: str) -> Any:
    if name == "AudioRouter":
        from .legado.roteador_audio import AudioRouter

        return AudioRouter
    if name in {"LoopMode", "MusicTrack"}:
        from .nucleo import modelos

        return getattr(modelos, name)
    if name in {"MusicError", "MusicExtractionError", "MusicPlaybackError"}:
        from .nucleo import erros

        return getattr(erros, name)
    raise AttributeError(name)


async def setup(bot):
    """Entry-point descoberto automaticamente pelo loader de cogs."""
    from .modulo import setup as setup_modulo

    await setup_modulo(bot)
