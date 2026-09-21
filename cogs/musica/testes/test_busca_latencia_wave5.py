from __future__ import annotations

import asyncio

from cogs.musica.busca.roteamento_fontes import planejar_fontes
from cogs.musica.metadados.provedores_api import MusicApiProviders


def test_roteamento_consulta_estruturada_usa_um_catalogo_quando_youtube_ja_foi_tentado():
    plano = planejar_fontes("Linkin Park - Numb", profundo=False, incluir_youtube=False)
    assert plano.motivo == "estruturada"
    assert plano.prioridades == ("spotify", "deezer")
    assert plano.max_fontes == 1


def test_roteamento_apresentacao_fast_nao_chama_catalogos_quando_youtube_ja_foi_tentado():
    plano = planejar_fontes("Linkin Park - Numb official video", profundo=False, incluir_youtube=False)
    assert plano.motivo == "apresentacao"
    assert plano.max_fontes == 0
    assert plano.prioridades == ("spotify", "deezer")


def test_roteamento_deep_amplia_fontes_sem_perder_prioridade():
    fast = planejar_fontes("Linkin Park - Numb", profundo=False, incluir_youtube=True)
    deep = planejar_fontes("Linkin Park - Numb", profundo=True, incluir_youtube=True)
    assert fast.prioridades[:3] == ("youtube", "spotify", "deezer")
    assert fast.max_fontes == 2
    assert deep.max_fontes == 3


def test_roteamento_remix_prioriza_soundcloud_como_segunda_fonte():
    plano = planejar_fontes("Notion remix", profundo=False, incluir_youtube=True)
    assert plano.motivo == "remix"
    assert plano.prioridades[:2] == ("youtube", "soundcloud")
    assert plano.max_fontes == 2


def test_roteamento_hint_explicito_prioriza_provider():
    plano = planejar_fontes("sweater weather deezer", profundo=False, incluir_youtube=True)
    assert plano.motivo == "hint_deezer"
    assert plano.prioridades[0] == "deezer"
    assert plano.max_fontes == 1


def test_search_sources_aplica_limite_depois_de_remover_provider_indisponivel(monkeypatch):
    api = MusicApiProviders(timeout=2.0)
    api.enabled = True
    api.youtube_api_key = ""
    api.spotify_client_id = ""
    api.spotify_client_secret = ""
    api.deezer_enabled = True
    api.soundcloud_enabled = False

    chamados = []

    async def deezer(query: str, *, limit: int = 3):
        chamados.append(("deezer", query, limit))
        return []

    monkeypatch.setattr(api, "deezer_search", deezer)

    async def scenario():
        return await api.search_sources(
            "Artista - Musica",
            limit=3,
            provider_order=("spotify", "deezer"),
            max_providers=1,
            total_budget_seconds=0.2,
        )

    assert asyncio.run(scenario()) == []
    assert chamados == [("deezer", "Artista - Musica", 3)]


def test_search_sources_nao_agenda_fontes_fora_do_plano(monkeypatch):
    api = MusicApiProviders(timeout=2.0)
    api.enabled = True
    api.youtube_api_key = "yt"
    api.spotify_client_id = "sp"
    api.spotify_client_secret = "secret"
    api.deezer_enabled = True
    api.soundcloud_enabled = False

    chamados = []

    async def youtube(query: str, *, limit: int = 3, include_details: bool = False):
        chamados.append("youtube")
        return []

    async def spotify(query: str, *, limit: int = 3):
        chamados.append("spotify")
        return []

    async def deezer(query: str, *, limit: int = 3):
        chamados.append("deezer")
        return []

    monkeypatch.setattr(api, "_youtube_search_com_quota", youtube)
    monkeypatch.setattr(api, "spotify_search", spotify)
    monkeypatch.setattr(api, "deezer_search", deezer)

    async def scenario():
        return await api.search_sources(
            "q",
            limit=3,
            provider_order=("youtube", "spotify", "deezer"),
            max_providers=1,
            total_budget_seconds=0.2,
        )

    assert asyncio.run(scenario()) == []
    assert chamados == ["youtube"]
