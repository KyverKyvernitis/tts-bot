from __future__ import annotations

import asyncio
from urllib.error import HTTPError

import pytest

from cogs.musica.busca import fontes
from cogs.musica.metadados.modelos import ApiTrackCandidate


@pytest.fixture(autouse=True)
def _resetar_breaker() -> None:
    fontes.resetar_circuito_youtube_fast()
    yield
    fontes.resetar_circuito_youtube_fast()


@pytest.mark.asyncio
async def test_timeout_abre_cooldown_e_proxima_busca_nao_consulta_api(monkeypatch) -> None:
    chamadas = 0

    class FakeProviders:
        enabled = True
        youtube_api_key = "key"

        async def search_youtube_fast(self, query: str, *, limit: int, timeout_seconds: float):
            nonlocal chamadas
            chamadas += 1
            raise asyncio.TimeoutError()

    monkeypatch.setattr(fontes, "_provedores", lambda: FakeProviders())
    monkeypatch.setattr(fontes, "youtube_api_fast_disponivel", lambda: not fontes.snapshot_circuito_youtube_fast().aberto)

    with pytest.raises(asyncio.TimeoutError):
        await fontes.buscar_candidatos_youtube_fast("505", limit=3)

    snap = fontes.snapshot_circuito_youtube_fast()
    assert snap.aberto is True
    assert 40.0 <= snap.restante_seconds <= 45.0

    assert await fontes.buscar_candidatos_youtube_fast("505", limit=3) == []
    assert chamadas == 1


@pytest.mark.asyncio
async def test_429_abre_cooldown_longo(monkeypatch) -> None:
    class FakeProviders:
        enabled = True
        youtube_api_key = "key"

        async def search_youtube_fast(self, query: str, *, limit: int, timeout_seconds: float):
            raise HTTPError("https://youtube.test", 429, "Too Many Requests", None, None)

    monkeypatch.setattr(fontes, "_provedores", lambda: FakeProviders())
    monkeypatch.setattr(fontes, "youtube_api_fast_disponivel", lambda: not fontes.snapshot_circuito_youtube_fast().aberto)

    with pytest.raises(HTTPError):
        await fontes.buscar_candidatos_youtube_fast("505", limit=3)

    snap = fontes.snapshot_circuito_youtube_fast()
    assert snap.aberto is True
    assert 590.0 <= snap.restante_seconds <= 600.0
    assert "429" in snap.motivo


@pytest.mark.asyncio
async def test_resposta_vazia_nao_abre_breaker(monkeypatch) -> None:
    chamadas = 0

    class FakeProviders:
        enabled = True
        youtube_api_key = "key"

        async def search_youtube_fast(self, query: str, *, limit: int, timeout_seconds: float):
            nonlocal chamadas
            chamadas += 1
            return []

    monkeypatch.setattr(fontes, "_provedores", lambda: FakeProviders())
    monkeypatch.setattr(fontes, "youtube_api_fast_disponivel", lambda: True)

    assert await fontes.buscar_candidatos_youtube_fast("consulta rara", limit=3) == []
    assert fontes.snapshot_circuito_youtube_fast().aberto is False
    assert await fontes.buscar_candidatos_youtube_fast("consulta rara", limit=3) == []
    assert chamadas == 2


@pytest.mark.asyncio
async def test_singleflight_compartilha_so_operacao_em_voo_sem_cache(monkeypatch) -> None:
    chamadas = 0
    entrou = asyncio.Event()
    liberar = asyncio.Event()

    class FakeProviders:
        enabled = True
        youtube_api_key = "key"

        async def search_youtube_fast(self, query: str, *, limit: int, timeout_seconds: float):
            nonlocal chamadas
            chamadas += 1
            entrou.set()
            await liberar.wait()
            return [
                ApiTrackCandidate(
                    title="Numb",
                    artist="Linkin Park",
                    webpage_url="https://youtube.test/numb",
                    provider="youtube",
                )
            ]

    monkeypatch.setattr(fontes, "_provedores", lambda: FakeProviders())
    monkeypatch.setattr(fontes, "youtube_api_fast_disponivel", lambda: True)

    primeira = asyncio.create_task(fontes.buscar_candidatos_youtube_fast("Linkin Park - Numb", limit=3))
    await entrou.wait()
    segunda = asyncio.create_task(fontes.buscar_candidatos_youtube_fast("Numb by Linkin Park", limit=3))
    await asyncio.sleep(0)
    assert chamadas == 1

    liberar.set()
    a, b = await asyncio.gather(primeira, segunda)
    assert a and b
    assert chamadas == 1
    assert fontes.snapshot_circuito_youtube_fast().em_voo == 0

    liberar = asyncio.Event()
    liberar.set()
    await fontes.buscar_candidatos_youtube_fast("Linkin Park - Numb", limit=3)
    assert chamadas == 2

@pytest.mark.asyncio
async def test_resolver_usa_worker_durante_cooldown_sem_tentar_api_de_novo(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao
    from cogs.musica.agente_telefone.roteamento import DestinoWorker

    fontes.resetar_circuito_youtube_fast()
    chamadas_api = 0
    chamadas_worker = 0

    async def api(query: str, *, limit: int = 3):
        nonlocal chamadas_api
        chamadas_api += 1
        if chamadas_api == 1:
            raise asyncio.TimeoutError()
        raise AssertionError("breaker deveria impedir nova tentativa de API")

    async def worker(executor, *, base, token, payload, timeout_seconds):
        nonlocal chamadas_worker
        chamadas_worker += 1
        return {
            "ok": True,
            "metadata_only": True,
            "default_search": "ytsearch3",
            "tracks": [
                {
                    "title": "Resultado worker",
                    "webpage_url": "https://youtube.test/worker",
                    "uploader": "Canal",
                    "source": "worker-ytdlp",
                    "metadata_only": True,
                }
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", api)
    monkeypatch.setattr(
        resolucao,
        "destino_vinculado",
        lambda guild_id: DestinoWorker("phone", "Phone", "http://worker.test", "token"),
    )
    monkeypatch.setattr(resolucao, "executar_resolucao_compartilhada", worker)

    # O resolver captura a primeira falha e usa o worker.
    primeiro = await resolucao.resolve_music_tracks_on_worker(
        "consulta breaker",
        metadata_only=True,
        guild_id=1,
    )
    assert primeiro.tracks[0].title == "Resultado worker"

    # Simula o comportamento real de fontes.py durante cooldown: sem nova API.
    async def api_em_cooldown(*args, **kwargs):
        return []

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", api_em_cooldown)
    segundo = await resolucao.resolve_music_tracks_on_worker(
        "consulta breaker",
        metadata_only=True,
        guild_id=1,
    )
    assert segundo.tracks[0].title == "Resultado worker"
    assert chamadas_api == 1
    assert chamadas_worker == 2
