from __future__ import annotations

import pytest

from cogs.musica.legado.extrator_local import MusicExtractor
from cogs.musica.metadados.fontes.spotify_publico import SpotifyPublicoMixin
from cogs.musica.metadados.modelos import ApiTrackBatch, ApiTrackCandidate
from cogs.musica.nucleo.erros import MusicExtractionError
from cogs.musica.nucleo.modelos import PlaylistCursor


PLAYLIST = "https://open.spotify.com/playlist/0123456789ABCDEFGHIJKL"


def _page(count: int, total: int) -> str:
    rows = "".join(
        f"<h3>Faixa {index}</h3><h4>Artista</h4><span>03:00</span>"
        for index in range(count)
    )
    return (
        f'<h1>Playlist</h1>{rows}<script type="application/json">'
        f'{{"trackCount":{count},"playlistV2":{{"content":{{"totalCount":{total},"items":[]}}}}}}'
        "</script>"
    )


def test_contagem_da_playlist_prevalece_sobre_as_linhas_do_preview() -> None:
    provider = object.__new__(SpotifyPublicoMixin)
    assert provider._spotify_public_total_tracks_from_content(_page(26, 200), minimum=25) == 200


@pytest.mark.asyncio
async def test_janela_curta_preserva_cursor_quando_total_e_maior() -> None:
    class Provider(SpotifyPublicoMixin):
        spotify_public_fallback_enabled = True
        spotify_public_fallback_max_tracks = 100

        async def _to_thread_text(self, url: str, max_bytes: int = 0) -> str:
            return _page(24, 200)

    batch = await Provider()._spotify_public_page_batch(
        "playlist", "0123456789ABCDEFGHIJKL", limit=25, original_url=PLAYLIST
    )
    assert batch is not None and len(batch.tracks) == 24
    assert batch.playlist_cursor is not None
    assert batch.playlist_cursor.next_offset == 24
    assert batch.playlist_cursor.total_tracks == 200
    assert batch.playlist_cursor.exhausted is False


@pytest.mark.asyncio
async def test_playlist_maior_que_tres_janelas_chega_ate_a_ultima_faixa() -> None:
    class Provider(SpotifyPublicoMixin):
        spotify_public_fallback_enabled = True
        spotify_public_fallback_max_tracks = 100

        async def _to_thread_text(self, url: str, max_bytes: int = 0) -> str:
            return _page(80, 80)

    provider = Provider()
    offset = 0
    titles: list[str] = []
    while True:
        batch = await provider._spotify_public_page_batch(
            "playlist", "0123456789ABCDEFGHIJKL", limit=25,
            original_url=PLAYLIST, offset=offset,
        )
        assert batch is not None and batch.playlist_cursor is not None
        titles.extend(track.title for track in batch.tracks)
        assert batch.playlist_cursor.next_offset > offset
        offset = batch.playlist_cursor.next_offset
        if batch.playlist_cursor.exhausted:
            break
    assert titles == [f"Faixa {index}" for index in range(80)]


@pytest.mark.asyncio
async def test_pagina_vazia_nao_descarta_playlist_com_faixas_pendentes(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    cursor = PlaylistCursor(
        provider="spotify_public", source_url=PLAYLIST, next_offset=25, total_tracks=200
    )

    async def unavailable(received: PlaylistCursor, *, limit: int):
        return None

    monkeypatch.setattr(extractor.api, "metadata_playlist_window", unavailable)
    with pytest.raises(MusicExtractionError):
        await extractor.continue_playlist_window(cursor, requester_id=1)
    assert cursor.exhausted is False


@pytest.mark.asyncio
async def test_provider_sem_cursor_mantem_total_ja_conhecido(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    cursor = PlaylistCursor(
        provider="spotify_public", source_url=PLAYLIST, next_offset=25, total_tracks=200
    )

    async def short_page(received: PlaylistCursor, *, limit: int):
        return ApiTrackBatch(
            tracks=[ApiTrackCandidate(title="Faixa 26", artist="Artista", source="Spotify público")],
            title="Playlist", is_playlist=True,
        )

    monkeypatch.setattr(extractor.api, "metadata_playlist_window", short_page)
    batch = await extractor.continue_playlist_window(cursor, requester_id=1)
    assert len(batch.tracks) == 1
    assert batch.playlist_cursor is not None
    assert batch.playlist_cursor.next_offset == 26
    assert batch.playlist_cursor.exhausted is False
