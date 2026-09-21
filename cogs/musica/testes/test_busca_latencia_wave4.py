from __future__ import annotations

import asyncio

import pytest

from cogs.musica.busca.chaves import chave_semantica_busca
from cogs.musica.busca import fontes
from cogs.musica.metadados.modelos import ApiTrackCandidate
from cogs.musica.metadados.provedores_api import MusicApiProviders
from cogs.musica.metadados.quota_youtube import (
    busca_youtube_disponivel,
    limpar_quota_youtube,
    snapshot_quota_youtube,
)
from cogs.musica.metadados.resiliencia import limpar_resiliencia_metadata


def test_chave_semantica_unifica_formas_estruturadas_equivalentes() -> None:
    assert chave_semantica_busca("Linkin Park - Numb") == chave_semantica_busca(
        "Numb by Linkin Park"
    )
    assert chave_semantica_busca("  LINKIN   PARK - Numb ") == chave_semantica_busca(
        "Linkin Park - Numb"
    )


def test_chave_semantica_nao_mistura_qualificadores() -> None:
    assert chave_semantica_busca("Linkin Park - Numb live") != chave_semantica_busca(
        "Linkin Park - Numb lyrics"
    )
    assert chave_semantica_busca("Live - Lightning Crashes") != chave_semantica_busca(
        "Lightning Crashes live"
    )


@pytest.mark.asyncio
async def test_youtube_fast_nao_cacheia_consulta_semanticamente_equivalente(monkeypatch) -> None:
    limpar_resiliencia_metadata()
    chamadas = 0

    class FakeProviders:
        enabled = True
        youtube_api_key = "key"

        async def search_youtube_fast(self, query: str, *, limit: int, timeout_seconds: float):
            nonlocal chamadas
            chamadas += 1
            return [
                ApiTrackCandidate(
                    title="Numb",
                    artist="Linkin Park",
                    provider="youtube",
                    webpage_url="https://www.youtube.com/watch?v=abc123",
                )
            ]

    monkeypatch.setattr(fontes, "_provedores", lambda: FakeProviders())
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_SEMANTIC_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_METADATA_CACHE_TTL_SECONDS", 300.0, raising=False)

    primeiro = await fontes.buscar_candidatos_youtube_fast("Linkin Park - Numb", limit=3)
    segundo = await fontes.buscar_candidatos_youtube_fast("Numb by Linkin Park", limit=3)

    assert chamadas == 2
    assert primeiro[0].webpage_url == segundo[0].webpage_url
    assert primeiro is not segundo
    limpar_resiliencia_metadata()


@pytest.mark.asyncio
async def test_cache_semantico_preserva_intencoes_distintas(monkeypatch) -> None:
    limpar_resiliencia_metadata()
    chamadas = 0

    class FakeProviders:
        enabled = True
        youtube_api_key = "key"

        async def search_youtube_fast(self, query: str, *, limit: int, timeout_seconds: float):
            nonlocal chamadas
            chamadas += 1
            return [ApiTrackCandidate(title=query, artist="A", provider="youtube")]

    monkeypatch.setattr(fontes, "_provedores", lambda: FakeProviders())
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_SEMANTIC_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_METADATA_CACHE_TTL_SECONDS", 300.0, raising=False)

    await fontes.buscar_candidatos_youtube_fast("Song live", limit=3)
    await fontes.buscar_candidatos_youtube_fast("Song lyrics", limit=3)

    assert chamadas == 2
    limpar_resiliencia_metadata()


@pytest.mark.asyncio
async def test_guard_youtube_interrompe_api_apos_soft_limit(monkeypatch) -> None:
    limpar_quota_youtube()
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    monkeypatch.setattr("cogs.musica.metadados.provedores_api.config.MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED", True, raising=False)
    monkeypatch.setattr("cogs.musica.metadados.provedores_api.config.MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", 2, raising=False)
    api = MusicApiProviders(timeout=2.0)
    api.enabled = True
    api.youtube_api_key = "key"
    chamadas = 0

    async def youtube(query: str, *, limit: int = 3, include_details: bool = True):
        nonlocal chamadas
        chamadas += 1
        return [ApiTrackCandidate(title=query, artist="A", provider="youtube")]

    api.youtube_search = youtube  # type: ignore[method-assign]

    assert await api.search_youtube_fast("um", limit=3)
    assert await api.search_youtube_fast("dois", limit=3)
    assert await api.search_youtube_fast("tres", limit=3) == []
    assert chamadas == 2
    snap = snapshot_quota_youtube(limite_diario=2)
    assert snap.chamadas == 2
    assert snap.bloqueadas == 1
    assert snap.restantes == 0
    assert not busca_youtube_disponivel(limite_diario=2, habilitado=True)
    await api.close()
    limpar_quota_youtube()


def test_guard_youtube_pode_ser_desabilitado() -> None:
    limpar_quota_youtube()
    for _ in range(5):
        assert busca_youtube_disponivel(limite_diario=0, habilitado=False)
    snap = snapshot_quota_youtube(limite_diario=0)
    assert snap.chamadas == 0
    limpar_quota_youtube()


@pytest.mark.asyncio
async def test_modo_simples_nao_reusa_cache_de_resolucao_entre_buscas(monkeypatch) -> None:
    from cogs.musica.agente_telefone import cache_resolucao, resolucao, roteamento

    cache_resolucao.limpar_cache_resolucao()
    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 420.0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_SEMANTIC_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_API_FIRST_ENABLED", False, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", False, raising=False)
    chamadas_worker = 0
    chamadas_metadata = 0

    async def metadata_fake(query: str, *, limit: int = 3, **kwargs):
        nonlocal chamadas_metadata
        chamadas_metadata += 1
        return []

    async def worker_fake(*, base, token, payload, timeout_seconds):
        nonlocal chamadas_worker
        chamadas_worker += 1
        return {
            "ok": True,
            "metadata_only": True,
            "default_search": "ytsearch3",
            "tracks": [
                {
                    "title": "Linkin Park - Numb",
                    "uploader": "Linkin Park",
                    "duration": 185,
                    "webpage_url": "https://www.youtube.com/watch?v=kXYiU_JCYtU",
                    "metadata_only": True,
                    "source": "youtube",
                }
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    primeiro = await resolucao.resolve_music_tracks_on_worker(
        "Linkin Park - Numb",
        requester_id=1,
        requester_name="primeiro",
        limit=3,
        metadata_only=True,
        guild_id=999,
    )
    segundo = await resolucao.resolve_music_tracks_on_worker(
        "Numb by Linkin Park",
        requester_id=2,
        requester_name="segundo",
        limit=3,
        metadata_only=True,
        guild_id=999,
    )

    assert chamadas_worker == 2
    assert chamadas_metadata == 0
    assert primeiro.query == "Linkin Park - Numb"
    assert segundo.query == "Numb by Linkin Park"
    assert segundo.tracks[0].requester_id == 2
    assert segundo.tracks[0].requester_name == "segundo"
    cache_resolucao.limpar_cache_resolucao()


@pytest.mark.asyncio
async def test_sem_cache_youtube_soft_limit_forca_fallback_na_busca_seguinte(monkeypatch) -> None:
    limpar_resiliencia_metadata()
    limpar_quota_youtube()
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_SEMANTIC_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_METADATA_CACHE_TTL_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED", True, raising=False)
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", 1, raising=False)
    api = MusicApiProviders(timeout=2.0)
    api.enabled = True
    api.youtube_api_key = "key"
    chamadas = 0

    async def youtube(query: str, *, limit: int = 3, include_details: bool = True):
        nonlocal chamadas
        chamadas += 1
        return [
            ApiTrackCandidate(
                title="Numb",
                artist="Linkin Park",
                provider="youtube",
                webpage_url="https://www.youtube.com/watch?v=abc123",
            )
        ]

    api.youtube_search = youtube  # type: ignore[method-assign]
    monkeypatch.setattr(fontes, "_provedores", lambda: api)

    primeiro = await fontes.buscar_candidatos_youtube_fast("Linkin Park - Numb", limit=3)
    assert primeiro
    assert chamadas == 1
    assert not busca_youtube_disponivel(limite_diario=1, habilitado=True)

    segundo = await fontes.buscar_candidatos_youtube_fast("Numb by Linkin Park", limit=3)
    assert segundo == []
    assert chamadas == 1
    assert snapshot_quota_youtube(limite_diario=1).bloqueadas == 1

    await api.close()
    limpar_resiliencia_metadata()
    limpar_quota_youtube()



def test_quota_youtube_persiste_entre_reinicializacoes_do_estado_em_memoria() -> None:
    import cogs.musica.metadados.quota_youtube as quota

    quota.limpar_quota_youtube()
    assert quota.consumir_busca_youtube(limite_diario=3, habilitado=True)
    assert quota.consumir_busca_youtube(limite_diario=3, habilitado=True)

    # Simula restart do processo sem apagar o arquivo persistido.
    quota._DIA_QUOTA = ""
    quota._CHAMADAS = 0
    quota._BLOQUEADAS = 0
    quota._CARREGADO = False

    snap = quota.snapshot_quota_youtube(limite_diario=3)
    assert snap.chamadas == 2
    assert snap.restantes == 1
    quota.limpar_quota_youtube()
