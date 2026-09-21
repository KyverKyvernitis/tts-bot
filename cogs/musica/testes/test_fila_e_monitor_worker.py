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
from cogs.musica.agente_telefone.monitor import (
    _assinatura_painel_remoto,
    _deve_atualizar_painel,
)
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


def test_monitor_ignora_telemetria_volatil_na_assinatura_do_painel() -> None:
    base = {
        "status": "playing",
        "paused": False,
        "confirmed_playing": True,
        "voice_connected": True,
        "player_present": True,
        "position_ms": 10_000,
        "status_age_seconds": 1.0,
        "updated_at": 100.0,
        "current": {
            "title": "A",
            "webpage_url": "https://example.invalid/a",
            "duration": 120,
            "uploader": "Artista",
        },
        "queue_size": 1,
        "queue": [{"title": "B", "webpage_url": "https://example.invalid/b", "duration": 90}],
    }
    later = dict(base)
    later.update(position_ms=80_000, status_age_seconds=71.0, updated_at=170.0)
    assert _assinatura_painel_remoto(base) == _assinatura_painel_remoto(later)

    changed = dict(later)
    changed["queue_size"] = 2
    assert _assinatura_painel_remoto(base) != _assinatura_painel_remoto(changed)


def test_monitor_so_refresca_painel_sem_mudanca_no_intervalo_periodico() -> None:
    assinatura = ("playing", "track")
    assert _deve_atualizar_painel(
        assinatura=assinatura,
        assinatura_anterior=assinatura,
        agora=20.0,
        ultimo_refresh=10.0,
        painel_existe=True,
        refresh_seconds=30.0,
    ) is False
    assert _deve_atualizar_painel(
        assinatura=assinatura,
        assinatura_anterior=assinatura,
        agora=41.0,
        ultimo_refresh=10.0,
        painel_existe=True,
        refresh_seconds=30.0,
    ) is True
    assert _deve_atualizar_painel(
        assinatura=("paused", "track"),
        assinatura_anterior=assinatura,
        agora=20.0,
        ultimo_refresh=10.0,
        painel_existe=True,
        refresh_seconds=30.0,
    ) is True
    assert _deve_atualizar_painel(
        assinatura=assinatura,
        assinatura_anterior=assinatura,
        agora=20.0,
        ultimo_refresh=10.0,
        painel_existe=False,
        refresh_seconds=30.0,
    ) is True


@pytest.mark.asyncio
async def test_painel_reposta_quando_starting_ja_atualizou_panel_key_antes_do_playing() -> None:
    state = MusicGuildState()
    state.now_message = object()
    calls: list[tuple] = []

    class RouterFake:
        def get_state(self, guild_id: int):
            assert guild_id == 321
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
            # Reproduz o comportamento real: o snapshot "starting" já edita o
            # painel existente e grava a chave da nova faixa antes de "playing".
            state.panel_track_key = self._panel_key_for_track(state.current)

        def start_music_agent_monitor(self, guild_id: int, **kwargs):
            calls.append(("monitor", guild_id, kwargs))

    router = RouterFake()
    remote_track = {
        "title": "Arctic Monkeys - 505",
        "webpage_url": "https://www.youtube.com/watch?v=qU9mHegkTc4",
        "duration": 252,
    }

    await sincronizar_estado_agente(
        router,
        321,
        agent_state={
            "status": "starting",
            "confirmed_playing": False,
            "playback_token": 11,
            "current": remote_track,
            "queue": [],
            "queue_size": 0,
        },
        voice_channel_id=999,
        text_channel_id=888,
        create_panel=True,
    )
    assert state.panel_track_key == remote_track["webpage_url"]
    assert not any(call[0] == "started" for call in calls)
    assert calls[-2][0] == "panel" or calls[-1][0] in {"panel", "monitor"}

    calls.clear()
    await sincronizar_estado_agente(
        router,
        321,
        agent_state={
            "status": "playing",
            "confirmed_playing": True,
            "voice_connected": True,
            "player_present": True,
            "playback_token": 11,
            "current": remote_track,
            "queue": [],
            "queue_size": 0,
        },
        voice_channel_id=999,
        text_channel_id=888,
        create_panel=True,
    )

    assert state.agent_started_playback_token == 11
    assert any(call[0] == "started" for call in calls)
    assert ("panel", 321, True, True) in calls
