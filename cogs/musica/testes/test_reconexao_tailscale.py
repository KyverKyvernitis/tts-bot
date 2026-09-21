from __future__ import annotations

import aiohttp
import pytest

from cogs.musica.agente_telefone import comandos, protocolo, transporte_http
from cogs.musica.agente_telefone.modelos import MusicWorkerEngineUnavailable
from cogs.musica.agente_telefone.roteamento import DestinoWorker


class _RespostaOK:
    status = 200

    async def text(self) -> str:
        return '{"ok": true, "recovered": true}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessaoQuebrada:
    closed = False

    def post(self, *args, **kwargs):
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")


class _SessaoRecuperada:
    closed = False

    def post(self, *args, **kwargs):
        return _RespostaOK()


@pytest.mark.asyncio
async def test_transporte_recicla_pool_e_retry_imediato_apos_closing_transport(monkeypatch) -> None:
    quebrada = _SessaoQuebrada()
    recuperada = _SessaoRecuperada()
    sessoes = iter([quebrada, recuperada])
    invalidadas = []

    async def obter():
        return next(sessoes)

    async def invalidar(session=None):
        invalidadas.append(session)

    monkeypatch.setattr(transporte_http, "obter_sessao_http", obter)
    monkeypatch.setattr(transporte_http, "invalidar_sessao_http", invalidar)
    monkeypatch.setattr(transporte_http, "_RECOVERY_DELAYS", (0.0, 0.0))

    result = await transporte_http.post_json_worker(
        url="http://100.64.0.10:8766/task",
        token="token",
        payload={"task": "music_agent_command", "command_id": "abc"},
        timeout_seconds=2.0,
    )

    assert result == {"ok": True, "recovered": True}
    assert invalidadas == [quebrada]


def test_comando_recebe_id_idempotente_unico() -> None:
    first = protocolo.montar_comando("play", guild_id=1, query="505")
    second = protocolo.montar_comando("play", guild_id=1, query="505")
    assert len(first["command_id"]) >= 20
    assert first["command_id"] != second["command_id"]


@pytest.mark.asyncio
async def test_erro_closing_transport_vira_indisponibilidade_transitoria(monkeypatch) -> None:
    destino = DestinoWorker("phone", "Phone", "http://100.64.0.10:8766", "token")
    monkeypatch.setattr(comandos, "destino_vinculado", lambda guild_id: destino)

    async def falhar(**kwargs):
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(comandos, "post_json_worker", falhar)

    with pytest.raises(MusicWorkerEngineUnavailable) as caught:
        await comandos.music_agent_command("play", guild_id=123, query="505")

    text = str(caught.value).lower()
    assert "reconectando" in text
    assert "closing transport" not in text
