from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.musica.agente_telefone import cache_resolucao, comandos, monitor, resolucao, roteamento
from cogs.musica.agente_telefone.modelos import MusicWorkerSelection


def _selection(worker_id: str, endpoint: str) -> MusicWorkerSelection:
    return MusicWorkerSelection(
        True,
        worker_id=worker_id,
        name=worker_id,
        worker={"worker_id": worker_id, "name": worker_id, "endpoint": endpoint},
        reason="ok",
    )


def test_destino_prefere_endpoint_do_worker_selecionado(monkeypatch) -> None:
    monkeypatch.setattr(roteamento.config, "PHONE_WORKER_TOKEN", "token-global", raising=False)
    monkeypatch.setattr(roteamento, "_phone_worker_base_url", lambda: "http://configurado:8766")

    destino = roteamento.destino_da_selecao(_selection("worker-b", "http://worker-b:8766/"))

    assert destino is not None
    assert destino.worker_id == "worker-b"
    assert destino.base == "http://worker-b:8766"
    assert destino.token == "token-global"


@pytest.mark.asyncio
async def test_comandos_da_guild_ficam_no_worker_que_iniciou_sessao(monkeypatch) -> None:
    roteamento.limpar_vinculos_worker()
    monkeypatch.setattr(roteamento.config, "PHONE_WORKER_TOKEN", "token", raising=False)

    selecoes = [_selection("worker-a", "http://worker-a:8766"), _selection("worker-b", "http://worker-b:8766")]
    chamadas_selecao = 0

    async def selecionar():
        nonlocal chamadas_selecao
        item = selecoes[min(chamadas_selecao, len(selecoes) - 1)]
        chamadas_selecao += 1
        return item

    urls: list[str] = []

    async def post_fake(**kwargs):
        urls.append(kwargs["url"])
        return {"ok": True, "state": {}}

    monkeypatch.setattr(comandos, "require_music_worker_available_async", selecionar)
    monkeypatch.setattr(comandos, "post_json_worker", post_fake)

    await comandos.music_agent_command("play", guild_id=77, query="x")
    await comandos.music_agent_command("pause", guild_id=77)
    await comandos.music_agent_status(guild_id=77)

    assert chamadas_selecao == 1
    assert urls == [
        "http://worker-a:8766/task",
        "http://worker-a:8766/task",
        "http://worker-a:8766/task",
    ]
    assert roteamento.destino_vinculado(77).worker_id == "worker-a"

    roteamento.limpar_vinculos_worker()


def test_cache_direto_e_particionado_por_worker_mas_metadata_e_compartilhada() -> None:
    meta_a = cache_resolucao.chave_cache_resolucao("teste", 5, True, worker_scope="worker-a")
    meta_b = cache_resolucao.chave_cache_resolucao("teste", 5, True, worker_scope="worker-b")
    direto_a = cache_resolucao.chave_cache_resolucao("https://x.test/a", 1, False, worker_scope="worker-a")
    direto_b = cache_resolucao.chave_cache_resolucao("https://x.test/a", 1, False, worker_scope="worker-b")

    assert meta_a == meta_b
    assert direto_a != direto_b


@pytest.mark.asyncio
async def test_resolucao_de_guild_ativa_usa_worker_vinculado(monkeypatch) -> None:
    roteamento.limpar_vinculos_worker()
    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    roteamento.vincular_guild_worker(123, destino)

    async def selecionar_proibido():
        raise AssertionError("não deve selecionar outro worker para guild vinculada")

    bases: list[str] = []

    async def executar_fake(*, base, token, payload, timeout_seconds):
        bases.append(base)
        return {"ok": True, "tracks": []}

    monkeypatch.setattr(resolucao, "require_music_worker_available_async", selecionar_proibido)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", executar_fake)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS", 0, raising=False)

    await resolucao.resolve_music_tracks_on_worker(
        "https://example.test/faixa",
        metadata_only=False,
        guild_id=123,
    )

    assert bases == ["http://worker-a:8766"]
    roteamento.limpar_vinculos_worker()


@pytest.mark.asyncio
async def test_monitor_para_e_libera_vinculo_de_worker_morto(monkeypatch) -> None:
    state = SimpleNamespace(agent_monitor_task=None)
    router = SimpleNamespace(get_state=lambda guild_id: state)
    roteamento.limpar_vinculos_worker()
    roteamento.vincular_guild_worker(9, roteamento.DestinoWorker("dead", "Dead", "http://dead:8766", "token"))

    async def status_fake(**kwargs):
        return {"ok": False, "available": False, "error": "connection refused"}

    async def sleep_fake(delay):
        await asyncio.sleep(0)

    real_sleep = asyncio.sleep

    async def yield_once(delay):
        await real_sleep(0)

    monkeypatch.setattr(monitor, "music_agent_status", status_fake)
    monkeypatch.setattr(monitor.asyncio, "sleep", yield_once)

    monitor.iniciar_monitor_music_agent(router, 9)
    task = state.agent_monitor_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert roteamento.destino_vinculado(9) is None
