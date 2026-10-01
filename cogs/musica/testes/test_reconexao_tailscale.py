from __future__ import annotations

import asyncio
import aiohttp
import pytest

from cogs.musica.agente_telefone import comandos, protocolo, transporte_http
from cogs.musica.agente_telefone.roteamento import DestinoWorker
from cogs.musica.agente_telefone.modelos import MusicWorkerEngineUnavailable


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
async def test_serialized_broken_pipe_retries_play_with_same_id_and_preserves_phone_owner(monkeypatch):
    from cogs.musica.agente_telefone import roteamento
    guild_id = 88721
    phone = DestinoWorker("phone", "Termux", "http://100.64.0.10:8766", "token")
    roteamento.vincular_guild_worker(guild_id, phone)
    sent = []
    async def post(**kwargs):
        sent.append(kwargs["payload"]["command_id"])
        if len(sent) <= 2:
            return {"ok": False, "error": "BrokenPipeError: [Errno 32] Broken pipe"}
        return {"ok": True, "state": {"status": "playing"}}
    monkeypatch.setattr(comandos, "post_json_worker", post)
    monkeypatch.setattr(comandos, "_resolver_destino_retry", lambda original, guild: asyncio.sleep(0, result=original))
    try:
        response = await comandos.music_agent_command("enqueue_many", guild_id=guild_id, query="playlist", command_id="playlist-delivery")
        assert response["deferred"]
        assert roteamento.destino_vinculado(guild_id) == phone
        await asyncio.wait_for(comandos._DEFERRED_TASKS[guild_id], timeout=2)
        assert sent == ["playlist-delivery"] * 3
        assert comandos.estado_comando_diferido(guild_id)["status"] == "delivered"
    finally:
        await comandos.cancelar_comandos_diferidos()
        roteamento.desvincular_guild_worker(guild_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 422])
@pytest.mark.parametrize("serialized", [False, True])
async def test_http_rejection_with_broken_pipe_detail_is_not_replayed(monkeypatch, status, serialized):
    phone = DestinoWorker("phone", "Termux", "http://100.64.0.10:8766", "token")
    guild_id = 88723
    monkeypatch.setattr(comandos, "destino_vinculado", lambda guild: phone)
    calls = []
    async def post(**kwargs):
        calls.append(kwargs["payload"])
        message = f"Music Agent HTTP {status}: BrokenPipeError: [Errno 32] Broken pipe"
        if serialized:
            return {"ok": False, "error": message}
        raise RuntimeError(message)
    monkeypatch.setattr(comandos, "post_json_worker", post)
    with pytest.raises(MusicWorkerEngineUnavailable):
        await comandos.music_agent_command("enqueue_many", guild_id=guild_id, query="playlist")
    assert len(calls) == 1
    assert guild_id not in comandos._DEFERRED_TASKS


@pytest.mark.asyncio
async def test_serialized_broken_pipe_on_temporal_control_is_not_queued_or_exposed_raw(monkeypatch):
    phone = DestinoWorker("phone", "Termux", "http://100.64.0.10:8766", "token")
    monkeypatch.setattr(comandos, "destino_vinculado", lambda guild: phone)
    async def post(**kwargs):
        return {"ok": False, "error": "BrokenPipeError: [Errno 32] Broken pipe"}
    monkeypatch.setattr(comandos, "post_json_worker", post)
    with pytest.raises(MusicWorkerEngineUnavailable, match="reconectando") as error:
        await comandos.music_agent_command("pause", guild_id=88722)
    assert "BrokenPipe" not in str(error.value)
    assert 88722 not in comandos._DEFERRED_TASKS


@pytest.mark.asyncio
async def test_erro_closing_transport_deixa_play_pendente_para_autorrecuperacao(monkeypatch) -> None:
    destino = DestinoWorker("phone", "Phone", "http://100.64.0.10:8766", "token")
    monkeypatch.setattr(comandos, "destino_vinculado", lambda guild_id: destino)

    async def falhar(**kwargs):
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(comandos, "post_json_worker", falhar)

    result = await comandos.music_agent_command("play", guild_id=123, query="505")
    assert result["ok"] is True
    assert result["deferred"] is True
    assert result["state"]["deferred_delivery"] is True
    assert result["state"]["status"] == "starting"
    await comandos.cancelar_comandos_diferidos()


@pytest.mark.asyncio
async def test_play_diferido_reenvia_mesmo_command_id_e_conclui(monkeypatch) -> None:
    destino = DestinoWorker("phone", "Phone", "http://100.64.0.10:8766", "token")
    monkeypatch.setattr(comandos, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(comandos.config, "MUSIC_AGENT_DEFERRED_PLAY_SECONDS", 10.0, raising=False)
    monkeypatch.setattr(comandos.config, "MUSIC_AGENT_DEFERRED_PLAY_RETRY_MAX_SECONDS", 0.01, raising=False)

    async def selecionar():
        class Selection:
            worker_id = "phone"
            name = "Phone"
            worker = {"worker_id": "phone", "name": "Phone", "endpoint": destino.base}
        return Selection()

    monkeypatch.setattr(comandos, "require_music_worker_available_async", selecionar)
    monkeypatch.setattr(comandos, "resolver_destino_worker", lambda *args, **kwargs: destino)

    command_ids: list[str] = []
    calls = 0

    async def post_fake(**kwargs):
        nonlocal calls
        calls += 1
        command_ids.append(str(kwargs["payload"].get("command_id") or ""))
        if calls == 1:
            raise asyncio.TimeoutError()
        return {"ok": True, "state": {"status": "playing"}}

    monkeypatch.setattr(comandos, "post_json_worker", post_fake)

    result = await comandos.music_agent_command("play", guild_id=456, query="505")
    assert result["deferred"] is True
    task = comandos._DEFERRED_TASKS[456]
    await asyncio.wait_for(task, timeout=2.0)
    snapshot = comandos.estado_comando_diferido(456)
    assert snapshot["status"] == "delivered"
    assert snapshot["attempts"] >= 1
    assert len(set(command_ids)) == 1
    await comandos.cancelar_comandos_diferidos()


@pytest.mark.asyncio
async def test_timeout_de_transporte_e_recuperavel_e_tenta_de_novo(monkeypatch) -> None:
    monkeypatch.setattr(transporte_http, "_RECOVERY_DELAYS", (0.0, 0.0))
    calls = 0

    async def obter():
        return _SessaoRecuperada()

    async def post_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError()
        return {"ok": True, "recovered": True}

    monkeypatch.setattr(transporte_http, "obter_sessao_http", obter)
    monkeypatch.setattr(transporte_http, "_post_json_once", post_once)
    monkeypatch.setattr(transporte_http, "invalidar_sessao_http", lambda session=None: asyncio.sleep(0))

    result = await transporte_http.post_json_worker(
        url="http://100.64.0.10:8766/task",
        token="token",
        payload={"task": "music_agent_status"},
        timeout_seconds=2.0,
    )
    assert result["recovered"] is True
    assert calls == 2

@pytest.mark.asyncio
async def test_play_fica_diferido_mesmo_se_healthcheck_nao_encontra_worker(monkeypatch) -> None:
    """Uma queda total de Tailscale não pode perder o `_p` antes do POST."""
    await comandos.cancelar_comandos_diferidos()
    comandos._DEFERRED_STATE.clear()
    monkeypatch.setattr(comandos, "destino_vinculado", lambda guild_id: None)
    monkeypatch.setattr(comandos.config, "PHONE_WORKER_ENABLED", True, raising=False)
    monkeypatch.setattr(comandos.config, "PHONE_WORKER_HOST", "100.64.0.10", raising=False)
    monkeypatch.setattr(comandos.config, "PHONE_WORKER_PORT", 8766, raising=False)
    monkeypatch.setattr(comandos.config, "PHONE_WORKER_SCHEME", "http", raising=False)
    monkeypatch.setattr(comandos.config, "PHONE_WORKER_TOKEN", "token", raising=False)
    monkeypatch.setattr(comandos.config, "MUSIC_AGENT_DEFERRED_PLAY_SECONDS", 10.0, raising=False)

    async def indisponivel():
        raise comandos.MusicWorkerUnavailable("nenhum worker alcançável")

    monkeypatch.setattr(comandos, "require_music_worker_available_async", indisponivel)

    result = await comandos.music_agent_command(
        "play",
        guild_id=789,
        voice_channel_id=987,
        query="Arctic Monkeys - Snap Out Of It",
    )

    assert result["ok"] is True
    assert result["deferred"] is True
    assert result["state"]["status"] == "starting"
    snapshot = comandos.estado_comando_diferido(789)
    assert snapshot["status"] == "pending"
    assert snapshot["base"] == "http://100.64.0.10:8766"
    assert snapshot["command_id"] == result["state"]["deferred_command_id"]
    await comandos.cancelar_comandos_diferidos()
