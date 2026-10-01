from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.musica.agente_telefone import comandos, resolucao, roteamento, selecao
from cogs.musica.agente_telefone.modelos import MusicWorkerSelection


@pytest.fixture
def direct(monkeypatch):
    roteamento.limpar_vinculos_worker()
    monkeypatch.setattr(roteamento, "_DIRECT_HEALTH_CACHE", None)
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_ENABLED", True)
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_BASE_URL", "http://127.0.0.1:8767")
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_TOKEN", "voice-secret")
    yield roteamento.configured_direct_destination()
    roteamento.limpar_vinculos_worker()


@pytest.mark.parametrize("base", ["http://8.8.8.8:8767", "http://public.example", "https://user:pass@host", "https://host/path", "https://host?token=x", "https://host:bad"])
def test_direct_endpoint_rejects_public_plaintext_or_embedded_credentials(direct, monkeypatch, base):
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_BASE_URL", base)
    with pytest.raises(ValueError):
        roteamento.configured_direct_destination()


@pytest.mark.parametrize("base", ["http://100.100.10.2:8767", "http://music.example.ts.net:8767", "https://music.example", "http://[fd7a:115c:a1e0::1]:8767"])
def test_direct_endpoint_accepts_private_tailnet_or_tls(direct, monkeypatch, base):
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_BASE_URL", base)
    assert roteamento.configured_direct_destination().base == base


@pytest.mark.asyncio
async def test_direct_commands_skip_phone_proxy_and_preserve_binding(direct, monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("phone selection on direct voice path"))
    sent = []
    async def post(**kwargs):
        sent.append(kwargs)
        return {"ok": True, "discord_ready": True}
    monkeypatch.setattr(comandos, "require_music_worker_available_async", forbidden)
    monkeypatch.setattr(comandos, "post_json_worker", post)
    await comandos.music_agent_command("play", guild_id=11, voice_channel_id=20, query="music", command_id="same")
    await comandos.music_agent_status(guild_id=11)
    assert all(item["url"] == direct.base + "/command" for item in sent)
    assert all("task" not in item["payload"] for item in sent)
    assert sent[0]["payload"]["command_id"] == "same"
    assert roteamento.destino_vinculado(11) == direct
    assert await comandos._resolver_destino_retry(direct, 11) == direct
    await comandos.music_agent_command("disconnect", guild_id=11)
    assert roteamento.destino_vinculado(11) is None


@pytest.mark.asyncio
async def test_existing_phone_owner_keeps_voice_but_archiving_uses_metadata_worker(direct, monkeypatch):
    phone = roteamento.DestinoWorker("phone", "Phone", "http://phone:8766", "phone-secret")
    roteamento.vincular_guild_worker(11, phone)
    sent = []
    async def post(**kwargs):
        sent.append(kwargs)
        return {"ok": True}
    monkeypatch.setattr(comandos, "post_json_worker", post)
    await comandos.music_agent_command("pause", guild_id=11)
    assert sent[-1]["url"] == phone.base + "/task"
    roteamento.vincular_guild_worker(11, direct)
    monkeypatch.setattr(comandos.config, "PHONE_WORKER_TOKEN", "phone-secret")
    monkeypatch.setattr(comandos, "require_music_worker_available_async", AsyncMock(return_value=MusicWorkerSelection(True, worker_id="phone", worker={"endpoint": phone.base})))
    await comandos.music_agent_command("archive_enqueue", guild_id=11)
    assert sent[-1]["url"] == phone.base + "/task"
    assert roteamento.destino_vinculado(11) == direct


@pytest.mark.asyncio
async def test_direct_voice_binding_is_not_used_for_metadata(direct, monkeypatch):
    roteamento.vincular_guild_worker(11, direct)
    monkeypatch.setattr(resolucao.config, "PHONE_WORKER_TOKEN", "phone-secret")
    monkeypatch.setattr(resolucao, "require_music_worker_available_async", AsyncMock(return_value=MusicWorkerSelection(True, worker_id="phone", worker={"endpoint": "http://phone:8766"})))
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS", 0, raising=False)
    called = []
    async def execute(**kwargs):
        called.append(kwargs["base"])
        return {"ok": True, "tracks": []}
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", execute)
    await resolucao.resolve_music_tracks_on_worker("https://www.youtube.com/watch?v=abcdefghijk", guild_id=11, metadata_only=True)
    assert called == ["http://phone:8766"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,role,version,deps,expected", [(True,"voice","0.3.83",True,True), (False,"voice","0.3.83",True,False), (True,"archive","0.3.83",True,False), (True,"voice","0.3.82",True,False), (True,"voice","0.3.83",False,False)])
async def test_direct_health_is_authoritative_and_cached(direct, monkeypatch, ready, role, version, deps, expected):
    requests = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({"ok": True, "discord_ready": ready, "version": version,
                               "executor_mode": role, "voice_dependencies": {"ok": deps}}).encode()
    def open_(request, **kwargs):
        requests.append(request)
        return Reply()
    monkeypatch.setattr(roteamento, "urlopen", open_)
    monkeypatch.setattr(selecao, "select_music_worker_async", AsyncMock(side_effect=AssertionError("no automatic phone fallback")))
    assert (await selecao.ensure_music_worker_available()).available is expected
    assert (await selecao.ensure_music_worker_available()).available is expected
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == "Bearer voice-secret"


@pytest.mark.asyncio
async def test_controller_direct_route_reaches_real_authenticated_agent_http(direct, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_TOKEN", "voice-secret")
    monkeypatch.setenv("MUSIC_AGENT_EXECUTOR_MODE", "voice")
    from aiohttp import web, ClientSession
    from cogs.musica.runtime_telefone.agente.servidor import MusicAgent
    from cogs.musica.agente_telefone.transporte_http import fechar_sessao_http
    agent = MusicAgent()
    monkeypatch.setattr(agent.client, "is_ready", lambda: True)
    monkeypatch.setattr(agent, "voice_dependencies_payload", lambda: {"ok": True})
    prepare = AsyncMock(return_value={"ok": True, "prepared": True})
    monkeypatch.setattr(agent, "cmd_prepare_voice", prepare)
    runner = web.AppRunner(agent._app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_BASE_URL", base)
    monkeypatch.setattr(comandos, "require_music_worker_available_async", AsyncMock(side_effect=AssertionError("phone proxy reached")))
    try:
        assert (await selecao.ensure_music_worker_available()).available
        result = await comandos.music_agent_command("prepare_voice", guild_id=91, voice_channel_id=19,
                                                    prepare_id="lease", command_id="dedup")
        assert result["prepared"]
        await comandos.music_agent_command("prepare_voice", guild_id=91, voice_channel_id=19,
                                          prepare_id="lease", command_id="dedup")
        assert prepare.await_count == 1
        assert "task" not in prepare.call_args.args[0]
        assert (await comandos.music_agent_status(guild_id=91))["discord_ready"]
        async with ClientSession() as client:
            async with client.get(base + "/health") as response:
                assert response.status == 401
                assert response.headers["X-Music-Agent-Version"] == "0.3.83"
            async with client.post(base + "/command", headers={"Authorization": "Bearer voice-secret"},
                                   json={"action": "archive_enqueue"}) as response:
                assert (await response.json())["ok"] is False
            agent.executor_mode = "archive"
            async with client.post(base + "/command", headers={"Authorization": "Bearer voice-secret"},
                                   json={"action": "prepare_voice"}) as response:
                assert (await response.json())["ok"] is False
            assert prepare.await_count == 1
    finally:
        await fechar_sessao_http()
        await runner.cleanup()
        await agent.client.close()
