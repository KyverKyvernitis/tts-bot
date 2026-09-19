from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.musica.agente_telefone import estado as estado_remoto
from cogs.musica.agente_telefone.protocolo import (
    faixa_para_payload,
    montar_comando,
    montar_consulta_status,
)
from cogs.musica.nucleo.modelos import MusicTrack


RAIZ = Path(__file__).resolve().parents[3]
MUSICA = RAIZ / "cogs" / "musica"


def test_protocolo_serializa_faixa_sem_fazer_rede() -> None:
    track = MusicTrack(
        title="Faixa teste",
        webpage_url="https://example.invalid/faixa",
        original_url="https://example.invalid/original",
        requester_id=10,
        requester_name="Core",
        duration=123,
        uploader="Artista",
        thumbnail="https://example.invalid/capa.jpg",
        source="YouTube",
        extractor="worker-ytdlp",
    )
    track.resolved_audio_ext = "opus"
    track.resolved_audio_codec = "opus"
    track.resolved_audio_abr = 160

    payload = faixa_para_payload(track)

    assert payload["title"] == "Faixa teste"
    assert payload["requester_id"] == 10
    assert payload["resolved_audio_ext"] == "opus"
    assert payload["resolved_audio_abr"] == 160


def test_montar_comando_define_query_da_faixa_e_preserva_extras() -> None:
    track = MusicTrack(
        title="Faixa",
        webpage_url="https://example.invalid/faixa",
        requester_id=1,
    )
    payload = montar_comando(
        "play",
        guild_id=11,
        voice_channel_id=22,
        text_channel_id=33,
        track=track,
        volume=75,
        timeout_seconds=4,
    )

    assert payload["task"] == "music_agent_command"
    assert payload["action"] == "play"
    assert payload["guild_id"] == 11
    assert payload["query"] == "https://example.invalid/faixa"
    assert payload["track"]["title"] == "Faixa"
    assert payload["volume"] == 75
    assert payload["timeout_seconds"] == 4.0


def test_consulta_status_tem_contrato_minimo() -> None:
    payload = montar_consulta_status(timeout_seconds=1.5)
    assert payload == {
        "task": "music_agent_status",
        "action": "status",
        "timeout_seconds": 1.5,
    }

def test_consulta_status_de_guild_pede_payload_compacto() -> None:
    payload = montar_consulta_status(timeout_seconds=1.5, guild_id=927, compact=True)
    assert payload == {
        "task": "music_agent_status",
        "action": "status",
        "timeout_seconds": 1.5,
        "guild_id": 927,
        "compact": True,
    }


def test_estado_remoto_decide_controles_sem_conhecer_player_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(estado_remoto.config, "MUSIC_AGENT_ENABLED", True, raising=False)
    router = SimpleNamespace(music_worker_only_enabled=lambda: True)
    state = SimpleNamespace(
        current_backend="agent",
        agent_remote_queue_size=0,
        last_voice_channel_id=0,
        current=None,
    )
    assert estado_remoto.usar_controles_fila_remota(router, state) is True


@pytest.mark.asyncio
async def test_refresh_remoto_consulta_e_sincroniza(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas: list[tuple] = []
    state = SimpleNamespace(
        current="track-local",
        last_voice_channel_id=44,
        last_text_channel_id=55,
    )

    async def status_fake(*, timeout_seconds=None, guild_id=0):
        chamadas.append(("status", timeout_seconds, guild_id))
        return {
            "guilds": {
                "123": {
                    "status": "playing",
                    "voice_channel_id": 77,
                    "text_channel_id": 88,
                }
            }
        }

    class RouterFake:
        def get_state(self, guild_id: int):
            assert guild_id == 123
            return state

        async def sync_music_agent_state(self, guild_id: int, track, remote, **kwargs):
            chamadas.append(("sync", guild_id, track, remote, kwargs))

    monkeypatch.setattr(estado_remoto, "music_agent_status", status_fake)
    remote = await estado_remoto.atualizar_estado_controle_remoto(
        RouterFake(),
        123,
        create_panel=True,
        timeout_seconds=1.25,
    )

    assert remote["status"] == "playing"
    assert chamadas[0] == ("status", 1.25, 123)
    assert chamadas[1][0:3] == ("sync", 123, "track-local")
    assert chamadas[1][4]["voice_channel_id"] == 77
    assert chamadas[1][4]["text_channel_id"] == 88
    assert chamadas[1][4]["create_panel"] is True


def test_transporte_e_estado_remoto_ficam_fora_do_roteador_legado() -> None:
    comandos = (MUSICA / "agente_telefone" / "comandos.py").read_text(encoding="utf-8")
    protocolo = (MUSICA / "agente_telefone" / "protocolo.py").read_text(encoding="utf-8")
    estado = (MUSICA / "agente_telefone" / "estado.py").read_text(encoding="utf-8")
    legado = (MUSICA / "legado" / "roteador_audio.py").read_text(encoding="utf-8")

    assert "montar_comando(" in comandos
    assert "def faixa_para_payload" in protocolo
    assert "def usar_controles_fila_remota" in estado
    assert "def atualizar_estado_controle_remoto" in estado
    assert "_music_agent_status(" not in legado
    assert "_agent_guild_state_from_status" not in legado
