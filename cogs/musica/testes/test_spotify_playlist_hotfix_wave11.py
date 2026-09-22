from __future__ import annotations

from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin


PLAYLIST = "https://open.spotify.com/playlist/4HQZyAM6swVZZZn308z4v8"


def _meta(title: str) -> dict:
    return {
        "title": title,
        "display_title": f"Cavetown - {title}",
        "uploader": "Cavetown",
        "display_uploader": "Cavetown",
        "source": "Spotify público",
        "display_source": "Spotify público",
        "extractor": "spotify",
        "webpage_url": "",
        "original_url": PLAYLIST,
        "duration": 240.0,
        "query": f"ytsearch1:Cavetown - {title} official audio",
    }


def test_playlist_item_separa_origem_spotify_da_identidade_tocavel() -> None:
    resolver = object.__new__(ResolucaoMixin)
    meta = _meta("Home")

    lazy = resolver._agent_track_from_metadata(meta, body={}, fallback_query=PLAYLIST)
    assert lazy.query == "ytsearch1:Cavetown - Home official audio"
    assert lazy.webpage_url == ""
    assert lazy.original_url == PLAYLIST

    resolved = resolver._agent_track_from_resolved(
        {
            "title": "Cavetown - Home",
            "uploader": "Cavetown",
            "webpage_url": "https://www.youtube.com/watch?v=home123",
            "stream_url": "https://rr.example/home-audio",
            "audio_codec": "opus",
        },
        query=lazy.query,
        track_meta=meta,
        body={},
    )
    assert resolved.webpage_url == "https://www.youtube.com/watch?v=home123"
    assert resolved.original_url == PLAYLIST
    assert resolved.public()["original_url"] == PLAYLIST


def test_cache_metadata_tem_namespace_novo_e_identidade_por_query() -> None:
    resolver = object.__new__(ResolucaoMixin)
    juliet = _meta("Juliet")
    home = _meta("Home")
    a = resolver._resolve_cache_key(juliet["query"], juliet)
    b = resolver._resolve_cache_key(home["query"], home)
    assert a.startswith("metadata:v2:spotify:")
    assert b.startswith("metadata:v2:spotify:")
    assert a != b

