from __future__ import annotations

import pytest

from cogs.musica.legado.extrator_local import MusicExtractor
from cogs.musica.metadados.direct_play import consulta_metadata_direct_play
from cogs.musica.metadados.modelos import ApiTrackBatch, ApiTrackCandidate
from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin


SPOTIFY_TRACK = "https://open.spotify.com/track/3BxXcWY0ZYkNBhiOvy6vWr?si=wave3"
SPOTIFY_PLAYLIST = "https://open.spotify.com/playlist/5swQ0HSpbndKvuoYXE9yjO?si=wave3"


def test_spotify_track_vira_busca_interna_de_um_unico_resultado() -> None:
    query = consulta_metadata_direct_play(
        titulo="Heaven Pierce Her - Castle Vein",
        artista="Heaven Pierce Her",
        fallback=SPOTIFY_TRACK,
    )

    assert query == "ytsearch1:Heaven Pierce Her - Castle Vein official audio"
    assert "spotify.com" not in query.lower()


def test_consulta_direct_play_preserva_prefixo_ja_fechado() -> None:
    ready = "ytsearch1:The Rare Occasions - Notion official audio"
    assert consulta_metadata_direct_play(titulo="Notion", artista="The Rare Occasions", fallback=ready) == ready


def test_music_agent_preserva_direct_query_e_nao_aplica_default_search() -> None:
    resolver = object.__new__(ResolucaoMixin)
    meta = {
        "title": "Heaven Pierce Her - Castle Vein",
        "display_title": "Heaven Pierce Her - Castle Vein",
        "uploader": "Heaven Pierce Her",
        "source": "Spotify público",
        "extractor": "metadata",
        "original_url": SPOTIFY_TRACK,
    }

    built = resolver._query_from_track_meta(meta, fallback_query=SPOTIFY_TRACK)
    assert built == "ytsearch1:Heaven Pierce Her - Castle Vein official audio"

    meta["query"] = "ytsearch1:Heaven Pierce Her - Castle Vein official audio"
    assert resolver._query_from_track_meta(meta, fallback_query="ignorado") == meta["query"]


@pytest.mark.asyncio
async def test_spotify_track_pede_apenas_um_item_ao_resolver_publico(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=73)
    seen: list[int] = []

    async def metadata_batch(url: str, *, limit: int = 25):
        seen.append(limit)
        return ApiTrackBatch(
            tracks=[
                ApiTrackCandidate(
                    title="Castle Vein",
                    artist="Heaven Pierce Her",
                    duration=203.0,
                    thumbnail="https://img.example/castle.jpg",
                    webpage_url=SPOTIFY_TRACK,
                    source="Spotify público",
                    provider="spotify",
                )
            ],
            title="Castle Vein",
            is_playlist=False,
            source="Spotify público",
        )

    monkeypatch.setattr(extractor.api, "metadata_batch_from_url", metadata_batch)
    batch = await extractor.extract(SPOTIFY_TRACK, requester_id=1, requester_name="Core")

    assert seen == [1]
    assert batch.is_playlist is False
    assert len(batch.tracks) == 1
    assert batch.tracks[0].extractor == "metadata"
    assert batch.tracks[0].original_url.startswith("https://open.spotify.com/track/")


@pytest.mark.asyncio
async def test_playlist_spotify_inicial_usa_start_first_em_vez_do_limite_total(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=37)
    seen: list[int] = []

    async def metadata_batch(url: str, *, limit: int = 25):
        seen.append(limit)
        return ApiTrackBatch(
            tracks=[
                ApiTrackCandidate(title="Um", artist="Artista 1", provider="spotify", source="Spotify público"),
                ApiTrackCandidate(title="Dois", artist="Artista 2", provider="spotify", source="Spotify público"),
            ],
            title="Playlist",
            is_playlist=True,
            source="Spotify público",
        )

    monkeypatch.setattr(extractor.api, "metadata_batch_from_url", metadata_batch)
    batch = await extractor.extract(SPOTIFY_PLAYLIST, requester_id=1, requester_name="Core")

    assert seen == [1]
    assert batch.is_playlist is True
    assert [track.uploader for track in batch.tracks] == ["Artista 1"]


def test_erro_spotify_nao_pede_api_ou_token() -> None:
    extractor = MusicExtractor()
    message = extractor._metadata_error_message("spotify", None)
    lower = message.lower()

    assert "configure a api" not in lower
    assert "refresh_token" not in lower
    assert "spotify web api/oauth" in lower
