from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from cogs.musica.agente_telefone.conversao import estado_da_guild_no_payload, faixa_do_payload
from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente, sincronizar_fila_remota


class FilaLocalFalsa:
    def __init__(self) -> None:
        self.itens = [1, 2]

    def empty(self) -> bool:
        return not self.itens

    def get_nowait(self):
        return self.itens.pop(0)

    def task_done(self) -> None:
        return None


def test_converte_payload_do_agente_em_faixa() -> None:
    faixa = faixa_do_payload({
        "title": "Musica teste",
        "uploader": "Artista",
        "webpage_url": "https://example.invalid/faixa",
        "duration": 123.0,
        "thumbnail": "https://example.invalid/capa.jpg",
        "audio_abr": 160,
    })
    assert faixa is not None
    assert faixa.title == "Musica teste"
    assert faixa.uploader == "Artista"
    assert faixa.duration == 123.0
    assert faixa.resolved_audio_abr == 160


def test_extrai_estado_da_guild_sem_vazar_outras_guilds() -> None:
    payload = {"guilds": {"10": {"status": "playing"}, "20": {"status": "paused"}}}
    assert estado_da_guild_no_payload(payload, 10) == {"status": "playing"}
    assert estado_da_guild_no_payload(payload, 30) == {}


def test_sincroniza_fila_remota_apenas_como_espelho() -> None:
    state = SimpleNamespace(
        agent_remote_queue_size=0,
        forward_queue=deque(),
        queue=FilaLocalFalsa(),
    )
    sincronizar_fila_remota(
        state,
        {
            "queue_size": 2,
            "queue": [
                {"title": "A", "webpage_url": "https://example.invalid/a"},
                {"title": "B", "webpage_url": "https://example.invalid/b"},
            ],
        },
        limite_fila=10,
        limite_historico=10,
    )
    assert state.agent_remote_queue_size == 2
    assert [track.title for track in state.forward_queue] == ["A", "B"]
    assert state.queue.empty()


class _RouterSyncFalso:
    def __init__(self) -> None:
        self.state = MusicGuildState()

    def get_state(self, guild_id: int):
        return self.state

    @staticmethod
    def _panel_key_for_track(track) -> str:
        if track is None:
            return ""
        return str(getattr(track, "webpage_url", "") or getattr(track, "title", "") or "")

    @staticmethod
    def _set_current_status(state, status: str) -> None:
        state.current_status = status

    @staticmethod
    def _reactivate_panel_controls_now(guild_id: int) -> None:
        return None

    @staticmethod
    def _schedule_agent_playback_started_effects(guild_id: int, key: str) -> None:
        return None

    @staticmethod
    def _mark_voice_status_track_change(state) -> None:
        return None

    @staticmethod
    def _schedule_voice_status_track_sync(guild_id: int, **kwargs) -> None:
        return None

    async def update_panel(self, guild_id: int, **kwargs) -> None:
        return None

    @staticmethod
    def start_music_agent_monitor(guild_id: int, **kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_agente_nao_exibe_bitrate_default_antes_de_resolver_stream() -> None:
    router = _RouterSyncFalso()
    assert router.state.current_quality_kbps > 0  # default legado, não é evidência remota

    await sincronizar_estado_agente(
        router,
        10,
        agent_state={
            "status": "starting",
            "current": {
                "title": "Arctic Monkeys - Snap Out Of It",
                "source": "Spotify",
                "webpage_url": "https://open.spotify.com/track/teste",
            },
        },
        create_panel=False,
    )

    assert router.state.current_quality_kbps == 0
    assert router.state.current_quality_label == ""


@pytest.mark.asyncio
async def test_agente_exibe_bitrate_somente_quando_worker_reporta_stream() -> None:
    router = _RouterSyncFalso()

    await sincronizar_estado_agente(
        router,
        10,
        agent_state={
            "status": "playing",
            "confirmed_playing": True,
            "voice_connected": True,
            "player_present": True,
            "current": {
                "title": "Arctic Monkeys - Snap Out Of It",
                "source": "YouTube",
                "webpage_url": "https://youtube.invalid/watch?v=teste",
                "audio_abr": 136,
                "audio_codec": "opus",
            },
        },
        create_panel=False,
    )

    assert router.state.current_quality_kbps == 136
    assert router.state.current_quality_label == "Worker"
