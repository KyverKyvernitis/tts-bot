"""Sistema modular de música + TTS ducking.

Este pacote fica fora de cogs/ para não ser carregado como extension.
"""

from .audio_router import AudioRouter
from .models import LoopMode, MusicTrack

__all__ = ["AudioRouter", "LoopMode", "MusicTrack"]
