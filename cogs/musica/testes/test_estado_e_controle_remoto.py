from __future__ import annotations

from pathlib import Path

import pytest

from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.reproducao import controle_remoto


RAIZ = Path(__file__).resolve().parents[3]
MUSICA = RAIZ / "cogs" / "musica"


def test_estado_da_guild_fica_no_nucleo_e_nao_no_roteador_legado() -> None:
    estado = (MUSICA / "nucleo" / "estado.py").read_text(encoding="utf-8")
    legado = (MUSICA / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    assert "class MusicGuildState" in estado
    assert "class ControlVote" in estado
    assert "class MusicGuildState" not in legado
    assert "class ControlVote" not in legado
    assert "from ..nucleo.estado import ControlVote, MusicGuildState" in legado


def test_estado_remoto_tem_prioridade_no_tamanho_da_fila() -> None:
    state = MusicGuildState()
    state.current_backend = "agent"
    state.agent_remote_queue_size = 7
    assert state.queue_size() == 7


def test_controles_de_worker_estao_centralizados() -> None:
    controle = (MUSICA / "reproducao" / "controle_remoto.py").read_text(encoding="utf-8")
    for nome in ("pausar", "retomar", "pular", "parar", "ajustar_volume", "buscar_momento", "embaralhar", "alternar_repeticao", "anterior"):
        assert f"async def {nome}" in controle

    legado = (MUSICA / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    for chamada in ('_music_agent_command("volume"', '_music_agent_command("previous"'):
        assert chamada not in legado

    modulo = (MUSICA / "modulo.py").read_text(encoding="utf-8")
    interface = (MUSICA / "interface" / "componentes.py").read_text(encoding="utf-8")
    assert "await enviar_controle_remoto(" in modulo
    assert "await enviar_controle_remoto(" in interface


@pytest.mark.asyncio
async def test_enviar_controle_remoto_invalida_operacao_e_sincroniza(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas: list[tuple] = []

    class RouterFake:
        def cancel_pending_music_operations(self, guild_id: int, *, reason: str) -> None:
            chamadas.append(("cancel", guild_id, reason))

        async def sync_music_agent_state(self, guild_id: int, track, remote, **kwargs) -> None:
            chamadas.append(("sync", guild_id, track, remote, kwargs))

    async def comando_fake(action: str, **kwargs):
        chamadas.append(("command", action, kwargs))
        return {
            "ok": True,
            "state": {
                "status": "playing",
                "voice_channel_id": 55,
                "text_channel_id": 66,
            },
        }

    monkeypatch.setattr(controle_remoto, "music_agent_command", comando_fake)
    router = RouterFake()
    result = await controle_remoto.enviar_controle_remoto(
        router,
        "skip",
        guild_id=123,
        requester_id=9,
        requester_name="Core",
        voice_channel_id=1,
        text_channel_id=2,
    )

    assert result["ok"] is True
    assert chamadas[0] == ("cancel", 123, "agent_skip")
    assert chamadas[1][0:2] == ("command", "skip")
    assert chamadas[1][2]["guild_id"] == 123
    assert chamadas[2][0:2] == ("sync", 123)
    assert chamadas[2][4]["voice_channel_id"] == 55
    assert chamadas[2][4]["text_channel_id"] == 66
