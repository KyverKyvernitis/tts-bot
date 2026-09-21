from __future__ import annotations

import pytest

from cogs.musica.legado.extrator_local import MusicExtractor
from cogs.musica.metadados.fontes.spotify_publico_parser import parse_embed_document
from cogs.musica.metadados.modelos import ApiTrackBatch, ApiTrackCandidate
from cogs.musica.metadados.provedores_api import MusicApiProviders
from cogs.musica.nucleo.modelos import MusicTrack, PlaylistCursor
from cogs.musica.nucleo.playlist_virtual import (
    PlaylistWindowPolicy,
    bounded_initial_window,
    should_refill_playlist,
)


SPOTIFY_PLAYLIST = "https://open.spotify.com/playlist/5swQ0HSpbndKvuoYXE9yjO"


def _playlist_html(count: int) -> str:
    rows = "".join(
        f"<h3>Faixa {idx}</h3><h4>Artista {idx}</h4><span>03:{idx % 60:02d}</span>"
        for idx in range(1, count + 1)
    )
    return f"<html><body><h1>Playlist Gigante</h1>{rows}</body></html>"


def _track(idx: int) -> MusicTrack:
    return MusicTrack(
        title=f"Faixa {idx}",
        webpage_url="",
        requester_id=1,
        uploader=f"Artista {idx}",
        source="Spotify público",
        extractor="metadata",
    )


def test_parser_publico_suporta_offset_sem_materializar_prefixo() -> None:
    doc = parse_embed_document(_playlist_html(80), offset=50, limit=7)

    assert len(doc.rows) == 7
    assert [row.title for row in doc.rows] == [f"Faixa {idx}" for idx in range(51, 58)]


@pytest.mark.asyncio
async def test_provider_spotify_expoe_cursor_e_proxima_janela(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True
    providers.spotify_public_fallback_max_tracks = 100
    content = _playlist_html(30)

    async def oembed(url: str):
        return {"title": "Playlist Gigante", "thumbnail": "https://img.example/cover.jpg"}

    async def text(url: str, *, headers=None, max_bytes: int = 5_000_000):
        return content

    monkeypatch.setattr(providers, "_spotify_public_oembed", oembed)
    monkeypatch.setattr(providers, "_to_thread_text", text)

    first = await providers.spotify_public_batch_from_url(SPOTIFY_PLAYLIST, limit=25)
    assert first is not None
    assert len(first.tracks) == 25
    assert first.playlist_cursor is not None
    assert first.playlist_cursor.next_offset == 25
    assert first.playlist_cursor.exhausted is False
    assert first.truncated is True

    second = await providers.metadata_playlist_window(first.playlist_cursor, limit=25)
    assert second is not None
    assert [item.title for item in second.tracks] == [f"Faixa {idx}" for idx in range(26, 31)]
    assert second.playlist_cursor is not None
    assert second.playlist_cursor.next_offset == 30
    assert second.playlist_cursor.exhausted is True
    assert second.truncated is False


def test_janela_virtual_limita_materializacao_sem_perder_cursor() -> None:
    tracks = [_track(idx) for idx in range(1, 101)]
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=SPOTIFY_PLAYLIST,
        title="Grande",
        resource_id="5swQ0HSpbndKvuoYXE9yjO",
        next_offset=100,
        exhausted=False,
    )
    policy = PlaylistWindowPolicy(low_watermark=8, high_watermark=25, startup_size=25)

    window, next_cursor = bounded_initial_window(tracks, cursor, policy=policy)

    assert len(window) == 25
    assert next_cursor is not None
    assert next_cursor.next_offset == 25
    assert next_cursor.exhausted is False


def test_backpressure_so_pede_refill_abaixo_da_marca_baixa() -> None:
    cursor = PlaylistCursor(provider="spotify_public", source_url=SPOTIFY_PLAYLIST, next_offset=25)
    policy = PlaylistWindowPolicy(low_watermark=8, high_watermark=25)

    assert should_refill_playlist(9, cursor, policy=policy) is False
    assert should_refill_playlist(8, cursor, policy=policy) is True
    assert policy.refill_limit(8) == 17
    assert should_refill_playlist(0, cursor.advanced(0, exhausted=True), policy=policy) is False


@pytest.mark.asyncio
async def test_extractor_continua_playlist_so_com_metadata_e_preserva_repeticoes(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=SPOTIFY_PLAYLIST,
        title="Repetições",
        resource_id="5swQ0HSpbndKvuoYXE9yjO",
        next_offset=25,
    )
    next_cursor = cursor.advanced(2, exhausted=False)

    async def next_window(received: PlaylistCursor, *, limit: int):
        assert received is cursor
        assert limit == 25
        return ApiTrackBatch(
            tracks=[
                ApiTrackCandidate(title="Mesmo Som", artist="Artista", duration=180.0, provider="spotify", source="Spotify público"),
                ApiTrackCandidate(title="Mesmo Som", artist="Artista", duration=180.0, provider="spotify", source="Spotify público"),
            ],
            title="Repetições",
            is_playlist=True,
            truncated=True,
            source="Spotify público",
            playlist_cursor=next_cursor,
        )

    monkeypatch.setattr(extractor.api, "metadata_playlist_window", next_window)
    batch = await extractor.continue_playlist_window(cursor, requester_id=1, requester_name="Core")

    assert len(batch.tracks) == 2
    assert [track.title for track in batch.tracks] == ["Artista - Mesmo Som", "Artista - Mesmo Som"]
    assert all(track.extractor == "metadata" for track in batch.tracks)
    assert batch.playlist_cursor is next_cursor
    assert batch.truncated is True


@pytest.mark.asyncio
async def test_extractor_marca_cursor_como_esgotado_quando_provider_nao_tem_mais(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=SPOTIFY_PLAYLIST,
        title="Fim",
        next_offset=25,
    )

    async def empty(received: PlaylistCursor, *, limit: int):
        return None

    monkeypatch.setattr(extractor.api, "metadata_playlist_window", empty)
    batch = await extractor.continue_playlist_window(cursor, requester_id=1)

    assert batch.tracks == []
    assert batch.playlist_cursor is not None
    assert batch.playlist_cursor.exhausted is True
    assert batch.playlist_cursor.next_offset == 25

@pytest.mark.asyncio
async def test_extractor_propaga_cursor_da_primeira_janela(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=SPOTIFY_PLAYLIST,
        title="Inicial",
        resource_id="5swQ0HSpbndKvuoYXE9yjO",
        next_offset=25,
    )

    async def initial(url: str, *, limit: int = 25):
        assert limit == 1
        return ApiTrackBatch(
            tracks=[ApiTrackCandidate(title="Primeira", artist="Artista", duration=180.0, provider="spotify", source="Spotify público")],
            title="Inicial",
            is_playlist=True,
            truncated=True,
            source="Spotify público",
            playlist_cursor=cursor,
        )

    monkeypatch.setattr(extractor.api, "metadata_batch_from_url", initial)
    batch = await extractor.extract(SPOTIFY_PLAYLIST, requester_id=1, requester_name="Core")

    assert batch.is_playlist is True
    assert batch.playlist_cursor is cursor
    assert batch.truncated is True

@pytest.mark.asyncio
async def test_refill_reusa_snapshot_do_monitor_e_respeita_backpressure(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from cogs.musica.nucleo.modelos import ExtractedBatch
    from cogs.musica.reproducao import playlist_virtual as virtual_runtime

    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=SPOTIFY_PLAYLIST,
        title="Grande",
        next_offset=25,
    )
    state = SimpleNamespace(
        virtual_playlist_refill_task=None,
        music_operation_generation=0,
        last_voice_channel_id=10,
        last_text_channel_id=20,
    )
    calls = []

    class Extractor:
        async def continue_playlist_window(self, received, *, requester_id, requester_name, limit):
            assert received.next_offset == 25
            assert limit == 17  # alvo 25 - 8 já materializadas
            return ExtractedBatch(
                tracks=[_track(26), _track(27)],
                query=SPOTIFY_PLAYLIST,
                is_playlist=True,
                playlist_title="Grande",
                truncated=True,
                playlist_cursor=cursor.advanced(2, exhausted=False),
            )

    class Router:
        extractor = Extractor()

        def get_state(self, guild_id):
            return state

        def current_music_operation_generation(self, guild_id):
            return state.music_operation_generation

        async def sync_music_agent_state(self, *args, **kwargs):
            calls.append(("sync", args, kwargs))

    async def command(action, **kwargs):
        calls.append((action, kwargs))
        return {"ok": True, "state": {"status": "playing"}}

    monkeypatch.setattr(virtual_runtime, "music_agent_command", command)
    remote = {
        "status": "playing",
        "voice_channel_id": 10,
        "text_channel_id": 20,
        "virtual_playlist": {
            "cursor": cursor.public(),
            "materialized_before": 8,
            "waiting": False,
            "requester_id": 1,
            "requester_name": "Core",
        },
    }

    assert virtual_runtime.schedule_playlist_refill_if_needed(Router(), 99, remote) is True
    task = state.virtual_playlist_refill_task
    assert task is not None
    await task

    command_call = next(item for item in calls if item[0] == "playlist_refill")
    assert command_call[1]["expected_cursor"]["next_offset"] == 25
    assert command_call[1]["next_cursor"]["next_offset"] == 27
    assert len(command_call[1]["tracks"]) == 2
    assert state.virtual_playlist_refill_task is None


def test_refill_nao_agenda_acima_da_marca_baixa() -> None:
    from types import SimpleNamespace
    from cogs.musica.reproducao import playlist_virtual as virtual_runtime

    cursor = PlaylistCursor(provider="spotify_public", source_url=SPOTIFY_PLAYLIST, next_offset=25)
    state = SimpleNamespace(virtual_playlist_refill_task=None, music_operation_generation=0)

    class Router:
        def get_state(self, guild_id):
            return state

    remote = {
        "virtual_playlist": {
            "cursor": cursor.public(),
            "materialized_before": 9,
            "waiting": False,
        }
    }
    assert virtual_runtime.schedule_playlist_refill_if_needed(Router(), 1, remote) is False
