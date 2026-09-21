from __future__ import annotations

import pytest

from cogs.musica.metadados.modelos import ApiTrackBatch, ApiTrackCandidate, PlayInputKind
from cogs.musica.metadados.provedores import classify_play_input
from cogs.musica.metadados.provedores_api import MusicApiProviders


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("arctic monkeys 505", PlayInputKind.SEARCH),
        ("https://www.youtube.com/watch?v=abc1234", PlayInputKind.DIRECT_TRACK),
        ("https://www.youtube.com/playlist?list=PL123", PlayInputKind.DIRECT_PLAYLIST),
        ("https://open.spotify.com/track/3BxXcWY0ZYkNBhiOvy6vWr?si=x", PlayInputKind.DIRECT_TRACK),
        ("https://open.spotify.com/playlist/0iKqZC22v5iQwRDBX2zB9L?si=x", PlayInputKind.DIRECT_PLAYLIST),
        ("https://open.spotify.com/album/1234567890abcdefghijkl", PlayInputKind.DIRECT_PLAYLIST),
    ],
)
def test_classificacao_direct_play(query: str, expected: PlayInputKind) -> None:
    assert classify_play_input(query) is expected


@pytest.mark.asyncio
async def test_metadata_spotify_usa_somente_resolver_publico(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = MusicApiProviders()
    providers.enabled = True
    called = {"public": 0, "api": 0}
    expected = ApiTrackBatch(
        tracks=[ApiTrackCandidate(title="Notion", artist="The Rare Occasions", provider="spotify")],
        title="Notion",
        is_playlist=False,
        source="Spotify público",
    )

    async def public(url: str, *, limit: int = 25):
        called["public"] += 1
        return expected

    async def official(*args, **kwargs):
        called["api"] += 1
        raise AssertionError("Spotify Web API não pode participar do playback")

    monkeypatch.setattr(providers, "spotify_public_batch_from_url", public)
    monkeypatch.setattr(providers, "spotify_batch_from_url", official)

    result = await providers.metadata_batch_from_url(
        "https://open.spotify.com/track/3BxXcWY0ZYkNBhiOvy6vWr",
        limit=1,
    )

    assert result is expected
    assert called == {"public": 1, "api": 0}


@pytest.mark.asyncio
async def test_resolver_publico_nao_toca_oauth_nem_api(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True
    providers.spotify_public_fallback_max_tracks = 100
    expected = ApiTrackBatch(
        tracks=[ApiTrackCandidate(title="Faixa", artist="Artista", provider="spotify")],
        title="Playlist",
        is_playlist=True,
        source="Spotify público",
    )

    async def page(kind: str, item_id: str, *, limit: int, original_url: str):
        assert kind == "playlist"
        assert item_id == "0iKqZC22v5iQwRDBX2zB9L"
        return expected

    async def forbidden(*args, **kwargs):
        raise AssertionError("OAuth/API Spotify foi chamado")

    monkeypatch.setattr(providers, "_spotify_public_page_batch", page)
    monkeypatch.setattr(providers, "_spotify_public_json", forbidden)
    monkeypatch.setattr(providers, "spotify_token", forbidden)
    monkeypatch.setattr(providers, "spotify_user_token", forbidden)

    result = await providers.spotify_public_batch_from_url(
        "https://open.spotify.com/playlist/0iKqZC22v5iQwRDBX2zB9L?si=x",
        limit=25,
    )
    assert result is expected


def test_playback_nao_orienta_refresh_token() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "legado" / "extrator_local.py").read_text(encoding="utf-8")
    assert "gere um SPOTIFY_REFRESH_TOKEN" not in source
    assert "Spotify API recusou essa playlist" not in source
