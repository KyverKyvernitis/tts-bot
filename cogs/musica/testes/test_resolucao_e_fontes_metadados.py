from __future__ import annotations

import asyncio
import sys
import types

import pytest

from cogs.musica.agente_telefone.cache_resolucao import (
    chave_cache_resolucao,
    copiar_lote_para_requisicao,
    ttl_cache_resolucao,
)
from cogs.musica.agente_telefone.conversao_resolucao import converter_resposta_resolucao
from cogs.musica.agente_telefone.resolucao import _limite_resolucao, _montar_tarefa_resolucao
from cogs.musica.metadados.modelos import ApiTrackCandidate
from cogs.musica.metadados.provedores_api import MusicApiProviders
from cogs.musica.nucleo.modelos import ExtractedBatch, MusicTrack


def test_cache_de_resolucao_copia_solicitante_sem_mutar_original() -> None:
    faixa = MusicTrack(
        title="Faixa",
        webpage_url="https://example.test/faixa",
        requester_id=1,
        requester_name="Original",
    )
    lote = ExtractedBatch(tracks=[faixa], query="faixa", is_playlist=False)
    copia = copiar_lote_para_requisicao(lote, requester_id=42, requester_name="Novo")
    assert copia.tracks[0] is not faixa
    assert copia.tracks[0].requester_id == 42
    assert faixa.requester_id == 1
    assert chave_cache_resolucao(" Faixa ", 3, True, False) == ("faixa", 3, True, False)


def test_cache_de_pesquisa_foi_removido_mas_cache_direto_permanece() -> None:
    assert ttl_cache_resolucao(somente_metadados=True) == 0.0
    assert ttl_cache_resolucao(somente_metadados=False) >= 0.0


def test_conversao_resolucao_aceita_metadata_sem_stream() -> None:
    lote = converter_resposta_resolucao(
        {
            "tracks": [
                {
                    "title": "Musica",
                    "webpage_url": "https://youtube.test/watch?v=abc",
                    "metadata_only": True,
                    "source": "worker-ytdlp",
                    "uploader": "Canal",
                    "duration": 123,
                }
            ]
        },
        base="http://worker.test",
        query="musica",
        limit=3,
        requester_id=9,
        requester_name="Pessoa",
    )
    faixa = lote.tracks[0]
    assert faixa.stream_url == ""
    assert faixa.source == "YouTube"
    assert faixa.extractor == "worker-ytdlp"
    assert faixa.requester_id == 9


def test_resolucao_worker_monta_ytsearch3_e_limita_link_direto() -> None:
    limite_busca, busca_textual = _limite_resolucao("artista musica", 50, permitir_playlist=False)
    assert busca_textual is True
    assert limite_busca >= 3
    limite_link, busca_textual = _limite_resolucao("https://example.test/faixa", 10, permitir_playlist=False)
    assert (limite_link, busca_textual) == (1, False)
    tarefa = _montar_tarefa_resolucao(
        query="artista musica",
        limit=3,
        timeout_seconds=12.0,
        somente_metadados=True,
        permitir_playlist=False,
        busca_textual=True,
    )
    assert tarefa["default_search"] == "ytsearch3"
    assert tarefa["metadata_only"] is True
    assert tarefa["fast_search"] is True


@pytest.mark.asyncio
async def test_provider_reutiliza_client_session_e_fecha_pool(monkeypatch) -> None:
    from cogs.musica.metadados import provedores_api

    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    monkeypatch.setattr(provedores_api.config, "MUSIC_SEARCH_HTTP_POOL_LIMIT", 6, raising=False)
    monkeypatch.setattr(provedores_api.config, "MUSIC_SEARCH_HTTP_POOL_LIMIT_PER_HOST", 3, raising=False)
    api = MusicApiProviders(timeout=2.0)
    primeira = await api._http_session_persistente()
    segunda = await api._http_session_persistente()
    assert primeira is segunda
    assert primeira.connector is not None
    assert primeira.connector.limit == 6
    assert primeira.connector.limit_per_host == 3
    await api.close()
    assert primeira.closed is True


def test_phone_worker_fast_search_usa_ytsearch3_sem_retries_nem_cli(monkeypatch) -> None:
    from cogs.musica.runtime_telefone.ponte_worker import resolucao as worker_resolucao

    capturado: dict[str, object] = {}

    class FakeYDL:
        def __init__(self, opts):
            capturado["opts"] = dict(opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, target, download=False):
            capturado["target"] = target
            return {
                "entries": [
                    {"id": "a", "title": "A", "uploader": "Canal", "extractor": "youtube"},
                    {"id": "b", "title": "B", "uploader": "Canal", "extractor": "youtube"},
                    {"id": "c", "title": "C", "uploader": "Canal", "extractor": "youtube"},
                    {"id": "d", "title": "D", "uploader": "Canal", "extractor": "youtube"},
                ]
            }

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    resultado = worker_resolucao.resolve_ytdlp(
        {
            "query": "artista faixa",
            "limit": 3,
            "metadata_only": True,
            "fast_search": True,
            "default_search": "ytsearch3",
        },
        job_timeout=10,
    )
    opts = capturado["opts"]
    assert capturado["target"] == "ytsearch3:artista faixa"
    assert opts["extract_flat"] == "in_playlist"
    assert opts["playlistend"] == 3
    assert opts["retries"] == 0
    assert opts["extractor_retries"] == 0
    assert len(resultado["tracks"]) == 3


def test_phone_worker_fast_search_vazio_nao_dispara_fallback_cli(monkeypatch) -> None:
    from cogs.musica.runtime_telefone.ponte_worker import resolucao as worker_resolucao

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, target, download=False):
            return None

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    monkeypatch.setattr(
        worker_resolucao.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback CLI nao deve rodar")),
    )
    resultado = worker_resolucao.resolve_ytdlp(
        {
            "query": "consulta sem resultado",
            "limit": 3,
            "metadata_only": True,
            "fast_search": True,
            "default_search": "ytsearch3",
        },
        job_timeout=10,
    )
    assert resultado["tracks"] == []


@pytest.mark.asyncio
async def test_metadata_singleflight_cancela_produtor_quando_ultimo_consumidor_desiste() -> None:
    from cogs.musica.metadados.resiliencia import buscar_metadata_compartilhada, limpar_resiliencia_metadata

    limpar_resiliencia_metadata()
    iniciou = asyncio.Event()
    cancelou = asyncio.Event()

    async def produtor():
        iniciou.set()
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            cancelou.set()
            raise
        return [ApiTrackCandidate(title="x", provider="youtube")]

    caller = asyncio.create_task(
        buscar_metadata_compartilhada(
            "consulta",
            limit=3,
            produtor=produtor,
            ttl_seconds=0,
            namespace="cancelavel",
            cancelar_quando_sem_consumidores=True,
        )
    )
    await iniciou.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0)
    assert cancelou.is_set()
    limpar_resiliencia_metadata()
