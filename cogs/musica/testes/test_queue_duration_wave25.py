from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from cogs.musica.interface.componentes import _queue_preview_text
from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import ExtractedBatch, MusicTrack, PlaylistCursor
from cogs.musica.reproducao.duracao_fila import schedule_virtual_duration_scan
from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente


SOURCE = "https://open.spotify.com/playlist/example"
KEY = ("occurrence", "spotify_public", SOURCE)


def _track(index: int, duration: float | None = 180) -> MusicTrack:
    return MusicTrack(title=f"Faixa {index}", duration=duration, requester_id=1,
                      webpage_url=f"https://example.com/{index}")


def _virtual(start: int, end: int, *, block_end: int | None = None) -> dict:
    return {
        "active": True, "provider": "spotify_public", "source_url": SOURCE,
        "instance_id": "occurrence", "next_offset": start, "total_tracks": 50,
        "block_end_offset": block_end, "remaining": end - start,
    }


def _state() -> MusicGuildState:
    state = MusicGuildState()
    materialized = [_track(index) for index in range(25)]
    state.forward_queue = deque(materialized[:4])
    virtual = _virtual(25, 50)
    state.agent_queue_layout = ([{"kind": "track", "track": track} for track in materialized]
                                + [{"kind": "virtual", "virtual": virtual}])
    state.agent_virtual_playlists = [virtual]
    state.agent_virtual_playlist = virtual
    state.agent_remote_queue_size = 50
    return state


@pytest.mark.asyncio
async def test_total_exato_apos_ler_segundo_lote_sem_materializar_player() -> None:
    state = _state()
    calls: list[tuple[int, int]] = []
    updates: list[str] = []

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            calls.append((cursor.next_offset, limit))
            tracks = [_track(index) for index in range(cursor.next_offset, min(50, cursor.next_offset + limit))]
            return ExtractedBatch(tracks=tracks, query=SOURCE,
                                  playlist_cursor=cursor.advanced(len(tracks), exhausted=True))

    class Router:
        extractor = Extractor()

        async def update_panel(self, guild_id, *, create):
            updates.append(_queue_preview_text(state))

    assert "calculando duração…" in _queue_preview_text(state)
    schedule_virtual_duration_scan(Router(), 1, state)
    task = state.agent_virtual_duration_task
    assert task is not None
    await task

    assert calls == [(25, 25)]
    assert state.agent_queue_layout[-1]["kind"] == "virtual"
    assert "**Fila** · 50 músicas · 2:30:00" in _queue_preview_text(state)
    assert updates and "2:30:00" in updates[-1]

    # Remover uma faixa virtual e dividir o cursor (p. ex. após shuffle)
    # usa os segundos da fonte sem somar a faixa removida novamente.
    first = _virtual(25, 36, block_end=36)
    second = _virtual(37, 50, block_end=50)
    state.agent_virtual_playlists = [first, second]
    state.agent_virtual_playlist = first
    state.agent_queue_layout = state.agent_queue_layout[:-1] + [
        {"kind": "virtual", "virtual": first}, {"kind": "virtual", "virtual": second},
        {"kind": "track", "track": _track(99, 30)},
    ]
    state.agent_remote_queue_size = 50
    assert "**Fila** · 50 músicas · 2:27:30" in _queue_preview_text(state)
    schedule_virtual_duration_scan(Router(), 1, state)
    assert state.agent_virtual_duration_task is None
    assert calls == [(25, 25)]


@pytest.mark.asyncio
async def test_duracao_desconhecida_nunca_vira_numero_falso() -> None:
    state = _state()

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            tracks = [_track(index, None if index == 39 else 180)
                      for index in range(cursor.next_offset, 50)]
            return ExtractedBatch(tracks=tracks, query=SOURCE,
                                  playlist_cursor=cursor.advanced(len(tracks), exhausted=True))

    async def update_panel(*args, **kwargs):
        pass

    router = SimpleNamespace(extractor=Extractor(), update_panel=update_panel)
    schedule_virtual_duration_scan(router, 1, state)
    await state.agent_virtual_duration_task
    assert (KEY, 39) in state.agent_virtual_duration_unknown
    assert "duração indisponível" in _queue_preview_text(state)
    assert "2:30:00" not in _queue_preview_text(state)


@pytest.mark.asyncio
async def test_provider_que_pula_uma_posicao_nao_produz_total_incorreto() -> None:
    state = _state()

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            return ExtractedBatch(tracks=[_track(25)], query=SOURCE,
                                  playlist_cursor=PlaylistCursor(provider="spotify_public",
                                                                 source_url=SOURCE, next_offset=27))

    async def update_panel(*args, **kwargs):
        pass

    router = SimpleNamespace(extractor=Extractor(), update_panel=update_panel)
    schedule_virtual_duration_scan(router, 1, state)
    await state.agent_virtual_duration_task
    assert KEY in state.agent_virtual_duration_failed_until
    assert "duração indisponível" in _queue_preview_text(state)


@pytest.mark.asyncio
async def test_playlist_sem_contagem_inicial_descobre_duracao_e_quantidade_exatas() -> None:
    state = _state()
    state.agent_queue_layout[-1]["virtual"]["total_tracks"] = None
    state.agent_queue_layout[-1]["virtual"]["remaining"] = None
    state.agent_remote_queue_size = 25
    calls: list[int] = []

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            calls.append(cursor.next_offset)
            tracks = [_track(index) for index in range(cursor.next_offset, min(cursor.next_offset + limit, 100))]
            return ExtractedBatch(tracks=tracks, query=SOURCE,
                                  playlist_cursor=cursor.advanced(len(tracks), exhausted=cursor.next_offset + len(tracks) == 100))

    async def update_panel(*args, **kwargs):
        pass

    router = SimpleNamespace(extractor=Extractor(), update_panel=update_panel)
    assert "25+ músicas · calculando duração…" in _queue_preview_text(state)
    schedule_virtual_duration_scan(router, 1, state)
    await state.agent_virtual_duration_task
    assert calls == [25, 75]
    assert "**Fila** · 100 músicas · 5:00:00" in _queue_preview_text(state)


@pytest.mark.asyncio
async def test_limpar_fila_cancela_leitura_antiga_e_apaga_cache() -> None:
    state = _state()
    started = asyncio.Event()

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            started.set()
            await asyncio.Event().wait()

    router = SimpleNamespace(extractor=Extractor())
    schedule_virtual_duration_scan(router, 1, state)
    old_task = state.agent_virtual_duration_task
    await started.wait()
    state.agent_queue_layout.clear()
    state.agent_virtual_playlists.clear()
    state.agent_virtual_playlist.clear()
    schedule_virtual_duration_scan(router, 1, state)
    try:
        await old_task
    except asyncio.CancelledError:
        pass
    assert state.agent_virtual_duration_task is None
    assert state.agent_virtual_duration_cache == {}


@pytest.mark.asyncio
async def test_snapshot_do_worker_dispara_calculo_sem_esperar_refill() -> None:
    state = MusicGuildState()
    cursor = PlaylistCursor(provider="spotify_public", source_url=SOURCE,
                            next_offset=25, total_tracks=50, instance_id="occurrence")
    physical = [
        {"title": f"Faixa {index}", "duration": 180, "requester_id": 1,
         "webpage_url": f"https://example.com/{index}"}
        for index in range(25)
    ]
    remote = {
        "status": "queued", "queue_size": 25, "logical_queue_size": 50,
        "queue": physical[:4],
        "queue_layout": ([{"kind": "track", "track": item} for item in physical]
                         + [{"kind": "virtual", "cursor": cursor.public(), "remaining": 25}]),
        "virtual_playlist": {"cursor": cursor.public(), "remaining": 25, "materialized_before": 25},
    }

    class Router:
        def __init__(self):
            self.extractor = self

        def get_state(self, guild_id):
            return state

        def _panel_key_for_track(self, track):
            return track.title if track else ""

        def _set_current_status(self, value, status):
            value.current_status = status

        def _reactivate_panel_controls_now(self, *args):
            pass

        def start_music_agent_monitor(self, *args, **kwargs):
            pass

        async def update_panel(self, *args, **kwargs):
            pass

        async def continue_playlist_window(self, received, *, requester_id, requester_name, limit):
            tracks = [_track(index) for index in range(received.next_offset, 50)]
            return ExtractedBatch(tracks=tracks, query=SOURCE,
                                  playlist_cursor=received.advanced(len(tracks), exhausted=True))

    await sincronizar_estado_agente(Router(), 1, agent_state=remote, create_panel=False)
    task = state.agent_virtual_duration_task
    assert task is not None
    await task
    assert "**Fila** · 50 músicas · 2:30:00" in _queue_preview_text(state)
