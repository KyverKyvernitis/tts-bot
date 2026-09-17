from __future__ import annotations


class MusicError(RuntimeError):
    """Erro base do sistema de música."""


class MusicExtractionError(MusicError):
    """Erro amigável de extração/busca de música."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail or ""

    @property
    def user_message(self) -> str:
        return str(self)


class MusicPlaybackError(MusicError):
    """Erro amigável de reprodução/FFmpeg/voice."""
