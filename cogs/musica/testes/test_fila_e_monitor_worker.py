from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.fila import (
    chaves_da_faixa,
    itens_pendentes,
    obter_proxima_faixa,
    registrar_historico,
    substituir_fila_local,
    tem_pendentes,
)
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente


def faixa(titulo: str, url: str) -> MusicTrack:
    return MusicTrack(title=titulo, webpage_url=url, requester_id=1, original_url=url)


def test_fila_do_dominio_preserva_ordem_forward_antes_da_queue() -> None:
    state = MusicGuildState()
    a = faixa("A", "https://example.invalid/a")
    b = faixa("B", "https://example.invalid/b")
    c = faixa("C", "https://example.invalid/c")
    state.forward_queue.extend([a, b])
    state.queue.put_nowait(c)

    assert [item.title for item in itens_pendentes(state)] == ["A", "B", "C"]
    assert tem_pendentes(state) is True


def test_historico_nao_duplica_mesma_faixa_em_sequencia() -> None:
    state = MusicGuildState()
    track = faixa("A", "https://example.invalid/a")
    assert registrar_historico(state, track) is True
    assert registrar_historico(state, track) is False
    assert list(state.history) == [track]


def test_chaves_da_faixa_mantem_url_e_titulo() -> None:
    track = MusicTrack(title="Minha Musica", webpage_url="https://example.invalid/A", requester_id=1, duration=65)
    keys = chaves_da_faixa(track, lambda value: value.lower().replace(" ", "-"))
    assert "url:https://example.invalid/a" in keys
    assert "title:minha-musica:8" in keys


@pytest.mark.asyncio
async def test_proxima_faixa_prioriza_forward_queue() -> None:
    state = MusicGuildState()
    a = faixa("Anterior", "https://example.invalid/anterior")
    b = faixa("Normal", "https://example.invalid/normal")
    state.forward_queue.append(a)
    await state.queue.put(b)

    track, veio_da_queue = await obter_proxima_faixa(state, timeout=0.1)
    assert track is a
    assert veio_da_queue is False


@pytest.mark.asyncio
async def test_substituir_fila_local_nao_cria_player() -> None:
    state = MusicGuildState()
    await state.queue.put(faixa("Velha", "https://example.invalid/old"))
    novas = [faixa("A", "https://example.invalid/a"), faixa("B", "https://example.invalid/b")]
    await substituir_fila_local(state, novas, limite=10)
    assert [item.title for item in list(state.queue._queue)] == ["A", "B"]
    assert state.forward_queue == state.forward_queue.__class__(maxlen=state.forward_queue.maxlen)


@pytest.mark.asyncio
async def test_sincronizacao_worker_nao_depende_de_player_local() -> None:
    state = MusicGuildState()
    calls: list[tuple] = []

    class RouterFake:
        def get_state(self, guild_id: int):
            assert guild_id == 123
            return state

        def _panel_key_for_track(self, track):
            return (track.webpage_url if track else "") or (track.title if track else "")

        def _set_current_status(self, st, status: str):
            st.current_status = status

        def _reactivate_panel_controls_now(self, guild_id: int):
            calls.append(("reactivate", guild_id))

        def _schedule_agent_playback_started_effects(self, guild_id: int, key: str):
            calls.append(("started", guild_id, key))

        async def update_panel(self, guild_id: int, *, create: bool, repost: bool):
            calls.append(("panel", guild_id, create, repost))

        def start_music_agent_monitor(self, guild_id: int, **kwargs):
            calls.append(("monitor", guild_id, kwargs))

    router = RouterFake()
    result = await sincronizar_estado_agente(
        router,
        123,
        agent_state={
            "status": "playing",
            "confirmed_playing": True,
            "current": {
                "title": "Faixa remota",
                "webpage_url": "https://example.invalid/track",
                "duration": 120,
            },
            "queue": [],
            "queue_size": 0,
        },
        create_panel=True,
    )

    assert result is state
    assert state.current is not None and state.current.title == "Faixa remota"
    assert state.current_backend == "agent"
    assert state.current_status == "playing"
    assert state.current_source is None
    assert state.current_lavalink_player is None
    assert any(call[0] == "monitor" for call in calls)
