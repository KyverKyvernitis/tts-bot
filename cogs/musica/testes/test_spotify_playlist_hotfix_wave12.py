from __future__ import annotations

from pathlib import Path

from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin


PLAYLIST = "https://open.spotify.com/playlist/1ilYP3llKeMPXrGl7cmWqQ?si=abc"
TRACK = "https://open.spotify.com/track/2koS4fD3kzizdnzWzyrxyT?si=abc"


def test_interface_nao_expoe_ytsearch_como_hyperlink() -> None:
    root = Path(__file__).resolve().parents[1]
    components = (root / "interface" / "componentes.py").read_text(encoding="utf-8")
    assert "def _public_track_link_url" in components
    assert 'if not value.lower().startswith(("http://", "https://"))' in components
    assert 'if profile.resource_type in {"playlist", "album"}' in components
    assert "url = _public_track_link_url(track)" in components
    # Os dois renderers (legado e V2) usam a mesma validação.
    assert components.count("url = _public_track_link_url(track)") >= 2


def test_playlist_spotify_nao_usa_stream_cache_global() -> None:
    resolver = object.__new__(ResolucaoMixin)
    meta = {
        "title": "Cavetown - Home",
        "uploader": "Cavetown",
        "source": "Spotify público",
        "extractor": "spotify",
        "webpage_url": "",
        "original_url": PLAYLIST,
        "query": "ytsearch1:Cavetown - Home official audio",
    }
    assert resolver._metadata_playlist_stream_cache_allowed(meta["query"], meta) is False


def test_track_spotify_individual_pode_usar_stream_cache() -> None:
    resolver = object.__new__(ResolucaoMixin)
    meta = {
        "title": "Marcos Valle - Estrelar",
        "source": "Spotify público",
        "extractor": "spotify",
        "original_url": TRACK,
    }
    assert resolver._metadata_playlist_stream_cache_allowed("ytsearch1:Marcos Valle - Estrelar official audio", meta) is True


def test_playlist_metadata_prefetcha_uma_faixa_a_frente_sem_cache_global() -> None:
    root = Path(__file__).resolve().parents[1]
    playback = (root / "runtime_telefone" / "agente" / "reproducao.py").read_text(encoding="utf-8")
    assert "metadata_playlist_next = not stream_cache_allowed" in playback
    assert "if metadata_playlist_next:" in playback
    assert "delay = 0.0" in playback
