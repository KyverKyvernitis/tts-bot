from __future__ import annotations

import asyncio

import pytest

from cogs.musica.agente_telefone.busca_profunda import executar_passagem_profunda
from cogs.musica.agente_telefone.coalescencia_resolucao import (
    executar_resolucao_compartilhada,
    limpar_coalescencia_resolucao,
)
from cogs.musica.metadados.modelos import ApiTrackCandidate


def _payload() -> dict:
    return {
        "task": "resolve_music",
        "query": "Linkin Park Numb",
        "limit": 5,
        "metadata_only": True,
        "allow_playlist": False,
        "fast_search": False,
        "default_search": "ytsearch5",
    }


def _worker_tracks() -> dict:
    return {
        "ok": True,
        "metadata_only": True,
        "tracks": [
            {
                "title": "Numb",
                "uploader": "Linkin Park",
                "webpage_url": f"https://youtube.test/{i}",
                "metadata_only": True,
                "source": "youtube",
            }
            for i in range(5)
        ],
    }


@pytest.mark.asyncio
async def test_deep_early_exit_worker_cancela_metadata_lenta() -> None:
    limpar_coalescencia_resolucao()
    cancelada = asyncio.Event()

    async def worker(**kwargs):
        await asyncio.sleep(0.01)
        return _worker_tracks()

    async def metadata(query: str, *, limit: int):
        try:
            await asyncio.sleep(5)
        finally:
            cancelada.set()
        return []

    resultado = await executar_passagem_profunda(
        base="http://worker",
        token="token",
        query="Linkin Park Numb",
        limit=5,
        timeout_seconds=5,
        requester_id=1,
        requester_name="tester",
        executar_worker=worker,
        buscar_metadata=metadata,
        avaliar_parcial=lambda tracks, api: len(tracks) >= 3,
    )

    assert resultado.early_exit == "worker"
    assert len(resultado.tracks) == 5
    assert cancelada.is_set()
    assert resultado.elapsed_ms < 500
    limpar_coalescencia_resolucao()


@pytest.mark.asyncio
async def test_deep_early_exit_metadata_cancela_worker_orfao() -> None:
    limpar_coalescencia_resolucao()
    worker_cancelado = asyncio.Event()

    async def worker(**kwargs):
        try:
            await asyncio.sleep(5)
        finally:
            worker_cancelado.set()
        return _worker_tracks()

    async def metadata(query: str, *, limit: int):
        await asyncio.sleep(0.01)
        return [
            ApiTrackCandidate(
                title=f"Numb {i}",
                artist="Linkin Park",
                webpage_url=f"https://youtube.test/meta{i}",
                source="youtube",
                provider="youtube",
            )
            for i in range(3)
        ]

    resultado = await executar_passagem_profunda(
        base="http://worker",
        token="token",
        query="Linkin Park Numb",
        limit=5,
        timeout_seconds=5,
        requester_id=1,
        requester_name="tester",
        executar_worker=worker,
        buscar_metadata=metadata,
        avaliar_parcial=lambda tracks, api: len(api) >= 3,
    )

    await asyncio.wait_for(worker_cancelado.wait(), timeout=0.5)
    assert resultado.early_exit == "metadata"
    assert len(resultado.api_candidates) == 3
    assert resultado.elapsed_ms < 500
    limpar_coalescencia_resolucao()


@pytest.mark.asyncio
async def test_deep_sem_confianca_espera_os_dois_lados() -> None:
    limpar_coalescencia_resolucao()

    async def worker(**kwargs):
        await asyncio.sleep(0.01)
        return _worker_tracks()

    async def metadata(query: str, *, limit: int):
        await asyncio.sleep(0.02)
        return [ApiTrackCandidate(title="Numb", artist="Linkin Park", provider="spotify")]

    resultado = await executar_passagem_profunda(
        base="http://worker",
        token="token",
        query="Linkin Park Numb",
        limit=5,
        timeout_seconds=5,
        requester_id=1,
        requester_name="tester",
        executar_worker=worker,
        buscar_metadata=metadata,
        avaliar_parcial=lambda tracks, api: False,
    )

    assert resultado.early_exit == ""
    assert len(resultado.tracks) == 5
    assert len(resultado.api_candidates) == 1
    limpar_coalescencia_resolucao()


@pytest.mark.asyncio
async def test_singleflight_worker_cancela_executor_quando_ultimo_consumidor_sai() -> None:
    limpar_coalescencia_resolucao()
    iniciou = asyncio.Event()
    cancelou = asyncio.Event()

    async def executor(**kwargs):
        iniciou.set()
        try:
            await asyncio.sleep(5)
        finally:
            cancelou.set()
        return {"ok": True, "tracks": []}

    task = asyncio.create_task(
        executar_resolucao_compartilhada(
            executor,
            base="http://worker",
            token="token",
            payload=_payload(),
            timeout_seconds=5,
        )
    )
    await iniciou.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelou.wait(), timeout=0.5)
    limpar_coalescencia_resolucao()


@pytest.mark.asyncio
async def test_singleflight_worker_preserva_executor_enquanto_outro_consumidor_espera() -> None:
    limpar_coalescencia_resolucao()
    iniciou = asyncio.Event()
    liberar = asyncio.Event()
    chamadas = 0

    async def executor(**kwargs):
        nonlocal chamadas
        chamadas += 1
        iniciou.set()
        await liberar.wait()
        return {"ok": True, "tracks": [{"title": "Numb"}]}

    a = asyncio.create_task(
        executar_resolucao_compartilhada(
            executor, base="http://worker", token="token", payload=_payload(), timeout_seconds=5
        )
    )
    await iniciou.wait()
    b = asyncio.create_task(
        executar_resolucao_compartilhada(
            executor, base="http://worker", token="token", payload=_payload(), timeout_seconds=5
        )
    )
    await asyncio.sleep(0)
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    assert not b.done()
    liberar.set()
    resultado = await b
    assert chamadas == 1
    assert resultado["tracks"][0]["title"] == "Numb"
    limpar_coalescencia_resolucao()
