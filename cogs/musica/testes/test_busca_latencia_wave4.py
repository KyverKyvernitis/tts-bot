from __future__ import annotations

import pytest

from cogs.musica.busca import fontes
from cogs.musica.busca.chaves import chave_semantica_busca
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


@pytest.mark.asyncio
async def test_youtube_fast_nao_cacheia_resultado_concluido(monkeypatch) -> None:
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
    await fontes.buscar_candidatos_youtube_fast("Linkin Park - Numb", limit=3)
    await fontes.buscar_candidatos_youtube_fast("Numb by Linkin Park", limit=3)
    assert chamadas == 2
    limpar_resiliencia_metadata()


@pytest.mark.asyncio
async def test_guard_youtube_interrompe_api_apos_soft_limit(monkeypatch) -> None:
    limpar_quota_youtube()
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    monkeypatch.setattr(
        "cogs.musica.metadados.provedores_api.config.MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "cogs.musica.metadados.provedores_api.config.MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS",
        2,
        raising=False,
    )
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
    await api.close()
    limpar_quota_youtube()


def test_guard_youtube_pode_ser_desabilitado() -> None:
    limpar_quota_youtube()
    for _ in range(5):
        assert busca_youtube_disponivel(limite_diario=0, habilitado=False)
    assert snapshot_quota_youtube(limite_diario=0).chamadas == 0
    limpar_quota_youtube()
