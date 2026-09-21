import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.musica.nucleo.modelos import ExtractedBatch, MusicTrack, PlaylistCursor
from cogs.musica.reproducao import playlist_virtual as virtual_runtime


SPOTIFY_PLAYLIST = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"


def _track(number: int) -> MusicTrack:
    return MusicTrack(
        title=f"Faixa {number}",
        webpage_url="",
        requester_id=1,
        uploader="Artista",
        source="Spotify público",
        extractor="metadata",
        original_url=SPOTIFY_PLAYLIST,
        display_title=f"Faixa {number}",
        display_uploader="Artista",
        display_source="Spotify público",
    )


def _remote(cursor: PlaylistCursor, *, materialized: int = 0, waiting: bool = True) -> dict:
    return {
        "status": "queued" if waiting else "playing",
        "voice_channel_id": 10,
        "text_channel_id": 20,
        "last_event": "playlist_refill_needed" if waiting else "playing",
        "virtual_playlist": {
            "cursor": cursor.public(),
            "materialized_before": materialized,
            "waiting": waiting,
            "requester_id": 1,
            "requester_name": "Core",
        },
    }


@pytest.mark.asyncio
async def test_refill_retry_transitorio_tem_backoff_e_reseta_apos_sucesso(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = PlaylistCursor(provider="spotify_public", source_url=SPOTIFY_PLAYLIST, next_offset=25)
    state = SimpleNamespace(
        virtual_playlist_refill_task=None,
        music_operation_generation=0,
        last_voice_channel_id=10,
        last_text_channel_id=20,
    )
    attempts = 0

    class Extractor:
        async def continue_playlist_window(self, received, *, requester_id, requester_name, limit):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("spotify temporariamente indisponível")
            return ExtractedBatch(
                tracks=[_track(26)],
                query=SPOTIFY_PLAYLIST,
                is_playlist=True,
                playlist_title="Grande",
                truncated=True,
                playlist_cursor=received.advanced(1, exhausted=False),
            )

    class Router:
        extractor = Extractor()

        def get_state(self, guild_id):
            return state

        def current_music_operation_generation(self, guild_id):
            return state.music_operation_generation

        async def sync_music_agent_state(self, *args, **kwargs):
            return None

    async def command(action, **kwargs):
        return {"ok": True, "state": {"status": "playing"}}

    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_RETRY_MAX_SECONDS", 0.002)
    monkeypatch.setattr(virtual_runtime, "music_agent_command", command)

    router = Router()
    assert virtual_runtime.schedule_playlist_refill_if_needed(router, 99, _remote(cursor)) is True
    await state.virtual_playlist_refill_task

    assert attempts == 3
    assert state.virtual_playlist_refill_failures == 0
    assert state.virtual_playlist_refill_retry_not_before == 0.0
    assert state.virtual_playlist_refill_cursor_key.endswith("|26")


@pytest.mark.asyncio
async def test_refill_falha_entra_em_cooldown_sem_novo_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = PlaylistCursor(provider="spotify_public", source_url=SPOTIFY_PLAYLIST, next_offset=50)
    state = SimpleNamespace(
        virtual_playlist_refill_task=None,
        music_operation_generation=0,
        last_voice_channel_id=10,
        last_text_channel_id=20,
    )

    class Extractor:
        async def continue_playlist_window(self, *args, **kwargs):
            raise RuntimeError("offline")

    class Router:
        extractor = Extractor()

        def get_state(self, guild_id):
            return state

        def current_music_operation_generation(self, guild_id):
            return state.music_operation_generation

    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_RETRY_BASE_SECONDS", 5.0)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_FAILURE_COOLDOWN_MAX_SECONDS", 20.0)

    router = Router()
    remote = _remote(cursor)
    assert virtual_runtime.schedule_playlist_refill_if_needed(router, 7, remote) is True
    await state.virtual_playlist_refill_task

    assert state.virtual_playlist_refill_failures == 1
    assert state.virtual_playlist_refill_retry_not_before > time.monotonic()
    # O monitor pode continuar recebendo snapshots, mas não cria task nem request
    # novo enquanto o cooldown monotônico estiver ativo.
    assert virtual_runtime.schedule_playlist_refill_if_needed(router, 7, remote) is False
    assert state.virtual_playlist_refill_task is None


@pytest.mark.asyncio
async def test_refill_concorrencia_global_do_router_e_limitada(monkeypatch: pytest.MonkeyPatch) -> None:
    states = {
        1: SimpleNamespace(virtual_playlist_refill_task=None, music_operation_generation=0, last_voice_channel_id=10, last_text_channel_id=20),
        2: SimpleNamespace(virtual_playlist_refill_task=None, music_operation_generation=0, last_voice_channel_id=10, last_text_channel_id=20),
    }
    active = 0
    max_active = 0

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.01)
                return ExtractedBatch(
                    tracks=[_track(cursor.next_offset + 1)],
                    query=SPOTIFY_PLAYLIST,
                    is_playlist=True,
                    playlist_title="Grande",
                    truncated=True,
                    playlist_cursor=cursor.advanced(1, exhausted=False),
                )
            finally:
                active -= 1

    class Router:
        extractor = Extractor()

        def get_state(self, guild_id):
            return states[guild_id]

        def current_music_operation_generation(self, guild_id):
            return states[guild_id].music_operation_generation

        async def sync_music_agent_state(self, *args, **kwargs):
            return None

    async def command(action, **kwargs):
        return {"ok": True, "state": {"status": "playing"}}

    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(virtual_runtime, "music_agent_command", command)

    router = Router()
    cursor1 = PlaylistCursor(provider="spotify_public", source_url=SPOTIFY_PLAYLIST, next_offset=25)
    cursor2 = PlaylistCursor(provider="spotify_public", source_url=SPOTIFY_PLAYLIST + "?g=2", next_offset=25)
    assert virtual_runtime.schedule_playlist_refill_if_needed(router, 1, _remote(cursor1)) is True
    assert virtual_runtime.schedule_playlist_refill_if_needed(router, 2, _remote(cursor2)) is True
    await asyncio.gather(states[1].virtual_playlist_refill_task, states[2].virtual_playlist_refill_task)

    assert max_active == 1


@pytest.mark.asyncio
async def test_cancelamento_do_refill_limpa_backoff() -> None:
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    state = SimpleNamespace(
        virtual_playlist_refill_task=task,
        virtual_playlist_refill_cursor_key="spotify|x|25",
        virtual_playlist_refill_failures=3,
        virtual_playlist_refill_retry_not_before=time.monotonic() + 30,
    )

    virtual_runtime.cancel_playlist_refill(state)
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert state.virtual_playlist_refill_task is None
    assert state.virtual_playlist_refill_cursor_key == ""
    assert state.virtual_playlist_refill_failures == 0
    assert state.virtual_playlist_refill_retry_not_before == 0.0


@pytest.mark.asyncio
async def test_refill_reenvia_comando_sem_reextrair_janela(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = PlaylistCursor(provider="spotify_public", source_url=SPOTIFY_PLAYLIST, next_offset=75)
    state = SimpleNamespace(
        virtual_playlist_refill_task=None,
        music_operation_generation=0,
        last_voice_channel_id=10,
        last_text_channel_id=20,
    )
    extract_calls = 0
    command_calls = 0

    class Extractor:
        async def continue_playlist_window(self, received, *, requester_id, requester_name, limit):
            nonlocal extract_calls
            extract_calls += 1
            return ExtractedBatch(
                tracks=[_track(76)],
                query=SPOTIFY_PLAYLIST,
                is_playlist=True,
                playlist_title="Grande",
                truncated=True,
                playlist_cursor=received.advanced(1, exhausted=False),
            )

    class Router:
        extractor = Extractor()

        def get_state(self, guild_id):
            return state

        def current_music_operation_generation(self, guild_id):
            return state.music_operation_generation

        async def sync_music_agent_state(self, *args, **kwargs):
            return None

    async def command(action, **kwargs):
        nonlocal command_calls
        command_calls += 1
        if command_calls == 1:
            # Simula resposta perdida/Tailscale oscilando. O CAS do cursor torna
            # a repetição idempotente no Phone Worker.
            raise RuntimeError("closing transport")
        return {"ok": True, "ignored": True, "state": {"status": "playing"}}

    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_COMMAND_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(virtual_runtime.config, "MUSIC_PLAYLIST_REFILL_RETRY_MAX_SECONDS", 0.001)
    monkeypatch.setattr(virtual_runtime, "music_agent_command", command)

    router = Router()
    assert virtual_runtime.schedule_playlist_refill_if_needed(router, 77, _remote(cursor)) is True
    await state.virtual_playlist_refill_task

    assert extract_calls == 1
    assert command_calls == 2
    assert state.virtual_playlist_refill_failures == 0
    assert state.virtual_playlist_refill_retry_not_before == 0.0
