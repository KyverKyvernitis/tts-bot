from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.musica import configuracao as config
from cogs.musica.legado.extrator_local import MusicExtractor
from cogs.musica.metadados.modelos import ApiTrackBatch, ApiTrackCandidate
from cogs.musica.nucleo.modelos import MusicTrack, PlaylistCursor
from cogs.musica.nucleo.playlist_virtual import PlaylistWindowPolicy, bounded_initial_window


SPOTIFY_PLAYLIST = "https://open.spotify.com/playlist/5swQ0HSpbndKvuoYXE9yjO"


def _track(idx: int) -> MusicTrack:
    return MusicTrack(
        title=f"Faixa {idx}",
        webpage_url="",
        uploader=f"Artista {idx}",
        source="Spotify público",
        extractor="metadata",
        requester_id=1,
    )


def test_policy_start_first_mantem_runway_completo_de_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MUSIC_PLAYLIST_WINDOW_SIZE", 25)
    monkeypatch.setattr(config, "MUSIC_PLAYLIST_LOW_WATERMARK", 8)
    # Mesmo uma configuração antiga de 1 não deve reabrir a corrida de skip/refill.
    monkeypatch.setattr(config, "MUSIC_PLAYLIST_STARTUP_SIZE", 1)

    policy = PlaylistWindowPolicy.from_config()
    assert policy.startup_size == 25
    assert policy.high_watermark == 25
    assert policy.low_watermark == 8

    tracks = [_track(idx) for idx in range(1, 101)]
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=SPOTIFY_PLAYLIST,
        title="Gigante",
        next_offset=100,
    )
    first, next_cursor = bounded_initial_window(tracks, cursor, policy=policy)

    assert [track.title for track in first] == [f"Faixa {idx}" for idx in range(1, 26)]
    assert next_cursor is not None
    assert next_cursor.next_offset == 25
    assert next_cursor.exhausted is False


@pytest.mark.asyncio
async def test_extractor_spotify_playlist_pede_janela_leve_inteira_no_start(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = MusicExtractor(max_playlist_items=100)
    monkeypatch.setattr(config, "MUSIC_PLAYLIST_LAZY_LOAD", True)
    monkeypatch.setattr(config, "MUSIC_PLAYLIST_WINDOW_SIZE", 25)
    monkeypatch.setattr(config, "MUSIC_PLAYLIST_STARTUP_SIZE", 1)

    seen_limits: list[int] = []
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url=SPOTIFY_PLAYLIST,
        title="Gigante",
        resource_id="5swQ0HSpbndKvuoYXE9yjO",
        next_offset=25,
        exhausted=False,
    )

    async def metadata(url: str, *, limit: int = 25):
        seen_limits.append(limit)
        return ApiTrackBatch(
            tracks=[
                ApiTrackCandidate(
                    title=f"Faixa {idx}",
                    artist="Artista",
                    duration=180.0,
                    source="Spotify público",
                    provider="spotify",
                )
                for idx in range(1, 26)
            ],
            title="Gigante",
            is_playlist=True,
            truncated=True,
            source="Spotify público",
            playlist_cursor=cursor,
        )

    monkeypatch.setattr(extractor.api, "metadata_batch_from_url", metadata)
    batch = await extractor.extract(SPOTIFY_PLAYLIST, requester_id=1, requester_name="Core")

    assert seen_limits == [25]
    assert len(batch.tracks) == 25
    assert batch.playlist_cursor is cursor


def test_refill_inicial_nao_e_agendado_quando_runway_ja_esta_cheio(monkeypatch: pytest.MonkeyPatch) -> None:
    from cogs.musica.reproducao import playlist_virtual as virtual_runtime

    calls: list[tuple[object, int, dict]] = []

    def schedule(router, guild_id: int, remote: dict) -> bool:
        calls.append((router, guild_id, remote))
        return False

    monkeypatch.setattr(virtual_runtime, "schedule_playlist_refill_if_needed", schedule)
    router = SimpleNamespace()
    remote = {
        "status": "playing",
        "virtual_playlist": {
            "cursor": {"provider": "spotify_public", "source_url": SPOTIFY_PLAYLIST, "next_offset": 25},
            "materialized_before": 24,
        },
    }

    assert virtual_runtime.schedule_playlist_refill_from_result(router, 77, {"state": remote}) is False
    assert calls == [(router, 77, remote)]


def test_refill_inicial_ignora_resultado_sem_playlist_virtual(monkeypatch: pytest.MonkeyPatch) -> None:
    from cogs.musica.reproducao import playlist_virtual as virtual_runtime

    called = False

    def schedule(*args, **kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(virtual_runtime, "schedule_playlist_refill_if_needed", schedule)
    router = SimpleNamespace()

    assert virtual_runtime.schedule_playlist_refill_from_result(router, 77, {"state": {"status": "playing"}}) is False
    assert called is False
