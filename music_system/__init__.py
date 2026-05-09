"""Sistema modular de música + TTS.

Este pacote fica fora de cogs/ para não ser carregado como extension.
"""

from .audio_router import AudioRouter
from .models import LoopMode, MusicTrack
from .errors import MusicError, MusicExtractionError, MusicPlaybackError

__all__ = ["AudioRouter", "LoopMode", "MusicTrack", "MusicError", "MusicExtractionError", "MusicPlaybackError"]
