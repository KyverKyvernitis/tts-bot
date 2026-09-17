from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from cogs.musica.agente_telefone.conversao import estado_da_guild_no_payload, faixa_do_payload
from cogs.musica.reproducao.sincronizacao import sincronizar_fila_remota


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
