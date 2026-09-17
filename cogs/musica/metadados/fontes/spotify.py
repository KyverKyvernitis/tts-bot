from __future__ import annotations

from .spotify_api import SpotifyApiMixin
from .spotify_autenticacao import SpotifyAutenticacaoMixin
from .spotify_publico import SpotifyPublicoMixin


class ProvedorSpotifyMixin(SpotifyAutenticacaoMixin, SpotifyPublicoMixin, SpotifyApiMixin):
    """Fachada de metadados Spotify; nunca participa da reprodução de áudio."""

