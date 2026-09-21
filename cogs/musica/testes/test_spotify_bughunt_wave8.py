from __future__ import annotations

import json

import pytest

from cogs.musica.legado.extrator_local import MusicExtractor
from cogs.musica.metadados.fontes.spotify_publico_parser import parse_embed_document
from cogs.musica.metadados.modelos import ApiTrackBatch, ApiTrackCandidate, PlayInputKind
from cogs.musica.metadados.provedores import classify_play_input, describe_url
from cogs.musica.metadados.provedores_api import MusicApiProviders
from cogs.musica.nucleo.modelos import PlaylistCursor


PLAYLIST_ID = "5swQ0HSpbndKvuoYXE9yjO"
TRACK_ID = "4PTG3Z6ehGkBFwjybzWkR8"
PLAYLIST = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"


def _playlist_html(count: int, *, title: str = "Playlist Pública") -> str:
    rows = "".join(
        f"<h3>Faixa {idx}</h3><h4>Artista {idx}</h4><span>03:{idx % 60:02d}</span>"
        for idx in range(1, count + 1)
    )
    return f"<html><head><title>{title} | Spotify</title></head><body><h1>{title}</h1>{rows}</body></html>"


def _json_track(title: str, artist: str, track_id: str = TRACK_ID) -> dict:
    return {
        "type": "track",
        "name": title,
        "uri": f"spotify:track:{track_id}",
        "duration_ms": 180_000,
        "artists": [{"name": artist}],
    }


def test_links_spotify_com_locale_embed_e_tracking_continuam_direct_play() -> None:
    playlist_url = f"https://open.spotify.com/intl-pt/playlist/{PLAYLIST_ID}?si=abc123&utm_source=share"
    track_url = f"https://open.spotify.com/embed/track/{TRACK_ID}?si=abc123"

    playlist_profile = describe_url(playlist_url)
    track_profile = describe_url(track_url)

    assert playlist_profile.resource_type == "playlist"
    assert playlist_profile.resource_id == PLAYLIST_ID
    assert playlist_profile.canonical == PLAYLIST
    assert classify_play_input(playlist_url) is PlayInputKind.DIRECT_PLAYLIST

    assert track_profile.resource_type == "track"
    assert track_profile.resource_id == TRACK_ID
    assert track_profile.canonical == f"https://open.spotify.com/track/{TRACK_ID}"
    assert classify_play_input(track_url) is PlayInputKind.DIRECT_TRACK


def test_resource_spotify_do_provider_tolera_prefixos_publicos() -> None:
    providers = MusicApiProviders()

    assert providers._spotify_resource(f"https://open.spotify.com/intl-pt/playlist/{PLAYLIST_ID}?si=x") == (
        "playlist",
        PLAYLIST_ID,
    )
    assert providers._spotify_resource(f"https://open.spotify.com/embed/track/{TRACK_ID}") == (
        "track",
        TRACK_ID,
    )


def test_json_publico_preserva_repeticoes_legitimas_sem_duplicar_wrapper() -> None:
    providers = MusicApiProviders()
    first = _json_track("Mesmo Som", "Mesmo Artista")
    second = _json_track("Mesmo Som", "Mesmo Artista")
    payload = {"items": [{"track": first}, {"track": second}]}

    tracks = providers._spotify_public_candidates_from_json(payload, url=PLAYLIST, limit=10)

    assert len(tracks) == 2
    assert [track.title for track in tracks] == ["Mesmo Som", "Mesmo Som"]
    assert [track.artist for track in tracks] == ["Mesmo Artista", "Mesmo Artista"]

    one = providers._spotify_public_candidates_from_json({"track": first}, url=PLAYLIST, limit=10)
    assert len(one) == 1


@pytest.mark.asyncio
async def test_colecao_prefere_ordem_server_rendered_a_json_hidratado(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True
    providers.spotify_public_fallback_max_tracks = 100

    misleading = {
        "items": [
            {"track": _json_track("JSON B", "Artista B", "7ouMYWpwJ422jRcDASZB7P")},
            {"track": _json_track("JSON A", "Artista A", "0VjIjW4GlUZAMYd2vXMi3b")},
        ]
    }
    content = (
        "<html><head>"
        f'<script type="application/json">{json.dumps(misleading)}</script>'
        "</head><body><h1>Ordem Real</h1>"
        "<h3>Primeira</h3><h4>Artista 1</h4><span>03:01</span>"
        "<h3>Segunda</h3><h4>Artista 2</h4><span>03:02</span>"
        "</body></html>"
    )

    async def oembed(url: str):
        return {"title": "Ordem Real", "thumbnail": ""}

    async def text(url: str, *, headers=None, max_bytes: int = 6_000_000):
        return content

    monkeypatch.setattr(providers, "_spotify_public_oembed", oembed)
    monkeypatch.setattr(providers, "_to_thread_text", text)

    batch = await providers.spotify_public_batch_from_url(PLAYLIST, limit=10)

    assert batch is not None
    assert [track.title for track in batch.tracks] == ["Primeira", "Segunda"]
    assert [track.artist for track in batch.tracks] == ["Artista 1", "Artista 2"]


@pytest.mark.asyncio
async def test_continuacao_nao_paga_oembed_e_prioriza_embed_em_qualquer_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True
    providers.spotify_public_fallback_max_tracks = 100
    seen_urls: list[str] = []

    async def forbidden_oembed(url: str):
        raise AssertionError("refill não deve consultar oEmbed")

    async def text(url: str, *, headers=None, max_bytes: int = 6_000_000):
        seen_urls.append(url)
        return _playlist_html(80)

    monkeypatch.setattr(providers, "_spotify_public_oembed", forbidden_oembed)
    monkeypatch.setattr(providers, "_to_thread_text", text)

    batch = await providers.spotify_public_batch_from_url(PLAYLIST, limit=5, offset=55)

    assert batch is not None
    assert [track.title for track in batch.tracks] == [f"Faixa {idx}" for idx in range(56, 61)]
    assert batch.playlist_cursor is not None
    assert batch.playlist_cursor.next_offset == 60
    assert seen_urls == ["https://open.spotify.com/embed/playlist/5swQ0HSpbndKvuoYXE9yjO"]


@pytest.mark.asyncio
async def test_provider_nao_impoe_teto_total_de_100_quando_pagina_publica_expoe_mais(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True
    providers.spotify_public_fallback_max_tracks = 100
    content = _playlist_html(160)

    async def forbidden_oembed(url: str):
        raise AssertionError("continuação não deve consultar oEmbed")

    async def text(url: str, *, headers=None, max_bytes: int = 6_000_000):
        return content

    monkeypatch.setattr(providers, "_spotify_public_oembed", forbidden_oembed)
    monkeypatch.setattr(providers, "_to_thread_text", text)

    window = await providers.spotify_public_batch_from_url(PLAYLIST, limit=25, offset=125)
    assert window is not None
    assert [track.title for track in window.tracks] == [f"Faixa {idx}" for idx in range(126, 151)]
    assert window.playlist_cursor is not None
    assert window.playlist_cursor.next_offset == 150
    assert window.playlist_cursor.exhausted is False

    tail = await providers.metadata_playlist_window(window.playlist_cursor, limit=25)
    assert tail is not None
    assert [track.title for track in tail.tracks] == [f"Faixa {idx}" for idx in range(151, 161)]
    assert tail.playlist_cursor is not None
    assert tail.playlist_cursor.next_offset == 160
    assert tail.playlist_cursor.exhausted is True


def test_parser_grande_retorna_so_a_janela_pedida() -> None:
    document = parse_embed_document(_playlist_html(2_000), offset=1_500, limit=4)

    assert [row.title for row in document.rows] == [
        "Faixa 1501",
        "Faixa 1502",
        "Faixa 1503",
        "Faixa 1504",
    ]
    assert len(document.rows) == 4


@pytest.mark.asyncio
async def test_extractor_forca_progresso_se_provider_devolver_cursor_estagnado(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=PLAYLIST,
        title="Grande",
        resource_id=PLAYLIST_ID,
        next_offset=50,
    )
    stalled = PlaylistCursor(
        provider="spotify_public",
        source_url=PLAYLIST,
        title="Grande",
        resource_id=PLAYLIST_ID,
        next_offset=50,
        exhausted=False,
    )

    async def next_window(received: PlaylistCursor, *, limit: int):
        return ApiTrackBatch(
            tracks=[ApiTrackCandidate(title="Faixa 51", artist="Artista", duration=180.0, provider="spotify")],
            title="Grande",
            is_playlist=True,
            truncated=True,
            source="Spotify público",
            playlist_cursor=stalled,
        )

    monkeypatch.setattr(extractor.api, "metadata_playlist_window", next_window)

    batch = await extractor.continue_playlist_window(cursor, requester_id=1, requester_name="Core")

    assert len(batch.tracks) == 1
    assert batch.playlist_cursor is not None
    assert batch.playlist_cursor.next_offset == 51
    assert batch.playlist_cursor.exhausted is False


@pytest.mark.asyncio
async def test_offset_avanca_por_itens_lidos_mesmo_se_metadata_for_descartada(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    cursor = PlaylistCursor(provider="spotify_public", source_url=PLAYLIST, next_offset=10)

    async def next_window(received: PlaylistCursor, *, limit: int):
        return ApiTrackBatch(
            tracks=[ApiTrackCandidate(title="", artist="", provider="spotify")],
            title="Grande",
            is_playlist=True,
            truncated=True,
            source="Spotify público",
            playlist_cursor=None,
        )

    monkeypatch.setattr(extractor.api, "metadata_playlist_window", next_window)

    batch = await extractor.continue_playlist_window(cursor, requester_id=1)

    assert batch.tracks == []
    assert batch.playlist_cursor is not None
    assert batch.playlist_cursor.next_offset == 11
