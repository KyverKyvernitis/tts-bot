from __future__ import annotations

import asyncio

import pytest
from pathlib import Path

from cogs.musica.agente_telefone.cache_resolucao import (
    chave_cache_resolucao,
    copiar_lote_para_requisicao,
)
from cogs.musica.agente_telefone.conversao_resolucao import converter_resposta_resolucao
from cogs.musica.agente_telefone.resolucao import _limite_resolucao, _montar_tarefa_resolucao
from cogs.musica.metadados.modelos import ApiTrackCandidate
from cogs.musica.metadados.provedores_api import MusicApiProviders
from cogs.musica.nucleo.modelos import ExtractedBatch, MusicTrack


RAIZ_MUSICA = Path(__file__).resolve().parents[1]


def test_cache_de_resolucao_copia_solicitante_sem_mutar_original() -> None:
    faixa = MusicTrack(
        title="Faixa",
        webpage_url="https://example.test/faixa",
        requester_id=1,
        requester_name="Original",
    )
    lote = ExtractedBatch(tracks=[faixa], query="faixa", is_playlist=False)
    copia = copiar_lote_para_requisicao(lote, requester_id=42, requester_name="Novo")

    assert copia is not lote
    assert copia.tracks[0] is not faixa
    assert copia.tracks[0].requester_id == 42
    assert copia.tracks[0].requester_name == "Novo"
    assert faixa.requester_id == 1
    assert faixa.requester_name == "Original"
    assert chave_cache_resolucao(" Faixa ", 5, True, False) == ("faixa", 5, True, False)


def test_conversao_resolucao_aceita_metadata_sem_stream_e_normaliza_origem() -> None:
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
        limit=5,
        requester_id=9,
        requester_name="Pessoa",
    )

    assert len(lote.tracks) == 1
    faixa = lote.tracks[0]
    assert faixa.stream_url == ""
    assert faixa.source == "YouTube"
    assert faixa.extractor == "worker-ytdlp"
    assert faixa.display_source == "YouTube"
    assert faixa.requester_id == 9


def test_resolucao_worker_monta_busca_e_limita_link_direto() -> None:
    limite_busca, busca_textual = _limite_resolucao("artista musica", 50, permitir_playlist=False)
    assert (limite_busca, busca_textual) == (10, True)

    limite_link, busca_textual = _limite_resolucao("https://example.test/faixa", 10, permitir_playlist=False)
    assert (limite_link, busca_textual) == (1, False)

    tarefa = _montar_tarefa_resolucao(
        query="artista musica",
        limit=5,
        timeout_seconds=12.0,
        somente_metadados=True,
        permitir_playlist=False,
        busca_textual=True,
    )
    assert tarefa["task"] == "music_ytdlp_resolve"
    assert tarefa["default_search"] == "ytsearch5"
    assert tarefa["metadata_only"] is True


def test_provedores_api_delegam_youtube_deezer_e_soundcloud_para_fontes() -> None:
    texto_fachada = (RAIZ_MUSICA / "metadados" / "provedores_api.py").read_text(encoding="utf-8")
    for nome in (
        "async def youtube_search",
        "async def deezer_search",
        "async def soundcloud_search",
    ):
        assert nome not in texto_fachada

    for arquivo in ("youtube.py", "deezer.py", "soundcloud.py"):
        caminho = RAIZ_MUSICA / "metadados" / "fontes" / arquivo
        assert caminho.is_file()
        texto = caminho.read_text(encoding="utf-8")
        for proibido in ("FFmpegPCMAudio", "LavalinkBackend", "yt_dlp.YoutubeDL"):
            assert proibido not in texto


def test_mixin_youtube_preserva_contrato_da_fachada(monkeypatch) -> None:
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    api = MusicApiProviders(timeout=2.0)
    api.youtube_api_key = "chave"

    respostas = [
        {
            "items": [
                {
                    "id": {"videoId": "abc123DEF"},
                    "snippet": {
                        "title": "Faixa",
                        "channelTitle": "Canal",
                        "thumbnails": {"medium": {"url": "https://img.test/capa.jpg"}},
                    },
                }
            ]
        },
        {
            "items": [
                {
                    "id": "abc123DEF",
                    "contentDetails": {"duration": "PT3M"},
                    "status": {"embeddable": True},
                }
            ]
        },
    ]

    async def fake_json(url: str, **kwargs):
        return respostas.pop(0)

    api._to_thread_json = fake_json  # type: ignore[method-assign]
    resultados = asyncio.run(api.youtube_search("Faixa", limit=1))

    assert len(resultados) == 1
    assert resultados[0].title == "Faixa"
    assert resultados[0].duration == 180.0
    assert resultados[0].provider == "youtube"


def test_spotify_esta_isolado_como_fonte_de_metadados() -> None:
    fachada = (RAIZ_MUSICA / "metadados" / "provedores_api.py").read_text(encoding="utf-8")
    pasta = RAIZ_MUSICA / "metadados" / "fontes"
    spotify = (pasta / "spotify.py").read_text(encoding="utf-8")
    autenticacao = (pasta / "spotify_autenticacao.py").read_text(encoding="utf-8")
    publico = (pasta / "spotify_publico.py").read_text(encoding="utf-8")
    api = (pasta / "spotify_api.py").read_text(encoding="utf-8")

    assert "def spotify_search" not in fachada
    assert "def spotify_batch_from_url" not in fachada
    assert "class ProvedorSpotifyMixin" in spotify
    assert "def spotify_token" in autenticacao
    assert "def spotify_public_batch_from_url" in publico
    assert "def spotify_search" in api
    assert "def spotify_batch_from_url" in api
    conjunto = spotify + autenticacao + publico + api
    for proibido in ("FFmpegPCMAudio", "LavalinkBackend", "yt_dlp.YoutubeDL", "voice_client.play"):
        assert proibido not in conjunto


def test_spotify_preserva_conversao_de_metadados_sem_player(monkeypatch) -> None:
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    api = MusicApiProviders(timeout=2.0)
    faixa = api._spotify_candidate(
        {
            "name": "Faixa",
            "artists": [{"name": "Artista"}],
            "duration_ms": 183000,
            "album": {
                "name": "Album",
                "images": [{"url": "https://img.test/capa.jpg"}],
            },
            "external_ids": {"isrc": "BRABC1234567"},
            "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
        }
    )

    assert faixa is not None
    assert faixa.provider == "spotify"
    assert faixa.title == "Faixa"
    assert faixa.artist == "Artista"
    assert faixa.duration == 183.0
    assert faixa.thumbnail == "https://img.test/capa.jpg"
    assert not hasattr(faixa, "stream_url")


def test_preparo_da_resolucao_worker_esta_em_modulo_proprio() -> None:
    from cogs.musica.agente_telefone import resolucao
    from cogs.musica.agente_telefone.solicitacao_resolucao import (
        limite_resolucao,
        montar_tarefa_resolucao,
        timeout_resolucao,
    )

    assert resolucao._limite_resolucao is limite_resolucao
    assert resolucao._montar_tarefa_resolucao is montar_tarefa_resolucao
    assert resolucao._timeout_resolucao is timeout_resolucao

    limite, busca = limite_resolucao("https://example.test/faixa", 10, permitir_playlist=False)
    assert (limite, busca) == (1, False)
    tarefa = montar_tarefa_resolucao(
        query="https://example.test/faixa",
        limit=limite,
        timeout_seconds=28.0,
        somente_metadados=False,
        permitir_playlist=False,
        busca_textual=busca,
    )
    assert tarefa["default_search"] == "auto"
    assert tarefa["metadata_only"] is False
    assert timeout_resolucao(somente_metadados=True, timeout_seconds=1.0) == 5.0


def test_search_sources_preserva_candidatos_de_multiplos_provedores(monkeypatch) -> None:
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    api = MusicApiProviders(timeout=2.0)
    api.enabled = True
    api.youtube_api_key = "yt-key"
    api.spotify_client_id = "sp-id"
    api.spotify_client_secret = "sp-secret"
    api.deezer_enabled = True
    api.soundcloud_enabled = False

    async def youtube(query: str, *, limit: int = 5):
        return [ApiTrackCandidate(title="Faixa", artist="Artista", provider="youtube")]

    async def spotify(query: str, *, limit: int = 5):
        return [ApiTrackCandidate(title="Faixa", artist="Artista", provider="spotify", isrc="BRABC1234567")]

    async def deezer(query: str, *, limit: int = 5):
        return [ApiTrackCandidate(title="Faixa", artist="Artista", provider="deezer", isrc="BRABC1234567")]

    api.youtube_search = youtube  # type: ignore[method-assign]
    api.spotify_search = spotify  # type: ignore[method-assign]
    api.deezer_search = deezer  # type: ignore[method-assign]

    resultados = asyncio.run(api.search_sources("Artista Faixa", limit=5, prefer_youtube=True))

    assert [item.provider for item in resultados] == ["youtube", "spotify", "deezer"]


@pytest.mark.asyncio
async def test_resolucao_textual_funde_worker_e_metadata_em_paralelo(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento
    from cogs.musica.metadados.modelos import ApiTrackCandidate

    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)

    provider_iniciou = asyncio.Event()
    worker_iniciou = asyncio.Event()

    async def metadata_fake(query: str, *, limit: int = 5):
        provider_iniciou.set()
        await worker_iniciou.wait()
        return [
            ApiTrackCandidate(
                title="Castle Vein",
                artist="Heaven Pierce Her",
                duration=275,
                provider="spotify",
                webpage_url="https://open.spotify.com/track/castle",
                isrc="TESTCASTLE001",
            )
        ]

    async def worker_fake(*, base, token, payload, timeout_seconds):
        worker_iniciou.set()
        await provider_iniciou.wait()
        return {
            "ok": True,
            "metadata_only": True,
            "tracks": [
                {
                    "title": "Heaven Pierce Her - Castle Vein (Official Audio)",
                    "uploader": "Heaven Pierce Her - Topic",
                    "duration": 275,
                    "webpage_url": "https://www.youtube.com/watch?v=castle",
                    "metadata_only": True,
                    "source": "youtube",
                },
                {
                    "title": "Castle Vein Piano Cover",
                    "uploader": "Piano Covers",
                    "duration": 280,
                    "webpage_url": "https://www.youtube.com/watch?v=cover",
                    "metadata_only": True,
                    "source": "youtube",
                },
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "Heaven Pierce Her - Castle Vein",
        requester_id=1,
        requester_name="tester",
        limit=5,
        metadata_only=True,
        guild_id=999,
    )

    assert provider_iniciou.is_set() and worker_iniciou.is_set()
    assert len(lote.tracks) == 2
    assert lote.tracks[0].webpage_url == "https://www.youtube.com/watch?v=castle"
    assert "cover" in lote.tracks[1].title.lower()


@pytest.mark.asyncio
async def test_busca_multifonte_remove_prefixo_de_engine_antes_das_apis(monkeypatch) -> None:
    from cogs.musica.busca import fontes

    consultas: list[str] = []

    class FakeProviders:
        has_any_provider = True

        async def search_sources(self, query: str, *, limit: int = 5, prefer_youtube: bool = True):
            consultas.append(query)
            return []

    monkeypatch.setattr(fontes, "_provedores_api", FakeProviders())

    await fontes.buscar_candidatos_multifonte("ytsearch: Daft Punk Get Lucky", limit=5)

    assert consultas == ["Daft Punk Get Lucky"]


@pytest.mark.asyncio
async def test_resolucao_busca_profunda_so_roda_quando_fast_pass_precisa(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento

    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", True, raising=False)

    limites: list[int] = []

    async def metadata_fake(query: str, *, limit: int = 5):
        return []

    async def worker_fake(*, base, token, payload, timeout_seconds):
        limite = int(payload["limit"])
        limites.append(limite)
        if limite <= 5:
            tracks = [
                {
                    "title": "Bohemian Like You",
                    "uploader": "The Dandy Warhols",
                    "duration": 210,
                    "webpage_url": "https://www.youtube.com/watch?v=wrong",
                    "metadata_only": True,
                    "source": "youtube",
                }
            ]
        else:
            tracks = [
                {
                    "title": "Bohemian Like You",
                    "uploader": "The Dandy Warhols",
                    "duration": 210,
                    "webpage_url": "https://www.youtube.com/watch?v=wrong",
                    "metadata_only": True,
                    "source": "youtube",
                },
                {
                    "title": "Bohemian Rhapsody (Official Video)",
                    "uploader": "Queen Official",
                    "duration": 355,
                    "webpage_url": "https://www.youtube.com/watch?v=correct",
                    "metadata_only": True,
                    "source": "youtube",
                },
            ]
        return {"ok": True, "metadata_only": True, "tracks": tracks}

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "quen bohemain rapsody",
        requester_id=1,
        requester_name="tester",
        limit=5,
        metadata_only=True,
        guild_id=999,
    )

    assert limites == [5, 10]
    assert lote.tracks[0].webpage_url == "https://www.youtube.com/watch?v=correct"


@pytest.mark.asyncio
async def test_resolucao_fast_pass_claro_nao_paga_segunda_busca(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento

    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", True, raising=False)

    chamadas = 0

    async def metadata_fake(query: str, *, limit: int = 5):
        return []

    async def worker_fake(*, base, token, payload, timeout_seconds):
        nonlocal chamadas
        chamadas += 1
        return {
            "ok": True,
            "metadata_only": True,
            "tracks": [
                {"title": "The Weeknd - Blinding Lights (Official Audio)", "uploader": "The Weeknd", "webpage_url": "https://www.youtube.com/watch?v=1", "metadata_only": True, "source": "youtube"},
                {"title": "Blinding Lights Remix", "uploader": "Random DJ", "webpage_url": "https://www.youtube.com/watch?v=2", "metadata_only": True, "source": "youtube"},
                {"title": "Blinding Lights Cover", "uploader": "Cover Channel", "webpage_url": "https://www.youtube.com/watch?v=3", "metadata_only": True, "source": "youtube"},
                {"title": "Save Your Tears", "uploader": "The Weeknd", "webpage_url": "https://www.youtube.com/watch?v=4", "metadata_only": True, "source": "youtube"},
                {"title": "Starboy", "uploader": "The Weeknd", "webpage_url": "https://www.youtube.com/watch?v=5", "metadata_only": True, "source": "youtube"},
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "the weeknd blinding lights",
        requester_id=1,
        requester_name="tester",
        limit=5,
        metadata_only=True,
        guild_id=999,
    )

    assert chamadas == 1
    assert lote.tracks[0].webpage_url == "https://www.youtube.com/watch?v=1"


@pytest.mark.asyncio
async def test_falha_da_busca_profunda_preserva_fast_pass(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento

    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", True, raising=False)

    chamadas = 0

    async def metadata_fake(query: str, *, limit: int = 5):
        if limit > 5:
            raise RuntimeError("provider deep indisponivel")
        return []

    async def worker_fake(*, base, token, payload, timeout_seconds):
        nonlocal chamadas
        chamadas += 1
        if chamadas > 1:
            raise TimeoutError("deep timeout")
        return {
            "ok": True,
            "metadata_only": True,
            "tracks": [
                {"title": "Misteriosa Musica", "uploader": "Canal", "webpage_url": "https://www.youtube.com/watch?v=first", "metadata_only": True, "source": "youtube"},
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "misteriosa musica",
        requester_id=1,
        requester_name="tester",
        limit=5,
        metadata_only=True,
        guild_id=999,
    )

    assert chamadas == 2
    assert len(lote.tracks) == 1
    assert lote.tracks[0].webpage_url == "https://www.youtube.com/watch?v=first"


@pytest.mark.asyncio
async def test_busca_multifonte_cache_curto_e_singleflight_nao_duplicam_provider(monkeypatch) -> None:
    from cogs.musica.busca import fontes
    from cogs.musica.metadados.resiliencia import limpar_resiliencia_metadata

    limpar_resiliencia_metadata()
    chamadas = 0
    iniciou = asyncio.Event()
    liberar = asyncio.Event()

    class FakeProviders:
        has_any_provider = True

        async def search_sources(self, query: str, *, limit: int = 5, prefer_youtube: bool = True):
            nonlocal chamadas
            chamadas += 1
            iniciou.set()
            await liberar.wait()
            return [ApiTrackCandidate(title="Numb", artist="Linkin Park", provider="spotify")]

    monkeypatch.setattr(fontes, "_provedores_api", FakeProviders())
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_METADATA_CACHE_TTL_SECONDS", 30.0, raising=False)
    monkeypatch.setattr(fontes.config, "MUSIC_SEARCH_METADATA_CACHE_MAX_ITEMS", 16, raising=False)

    primeira = asyncio.create_task(fontes.buscar_candidatos_multifonte("Linkin Park Numb", limit=5))
    await iniciou.wait()
    segunda = asyncio.create_task(fontes.buscar_candidatos_multifonte("Linkin Park Numb", limit=5))
    await asyncio.sleep(0)
    liberar.set()
    a, b = await asyncio.gather(primeira, segunda)

    assert chamadas == 1
    assert a[0].title == b[0].title == "Numb"
    a[0].title = "mutado"

    terceira = await fontes.buscar_candidatos_multifonte("Linkin Park Numb", limit=5)
    assert chamadas == 1
    assert terceira[0].title == "Numb"
    limpar_resiliencia_metadata()


@pytest.mark.asyncio
async def test_provider_circuit_breaker_para_de_repetir_falha(monkeypatch) -> None:
    from cogs.musica.metadados import provedores_api
    from cogs.musica.metadados.resiliencia import estado_circuito_provider, limpar_resiliencia_metadata

    limpar_resiliencia_metadata()
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    monkeypatch.setattr(provedores_api.config, "MUSIC_SEARCH_PROVIDER_TIMEOUT_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(provedores_api.config, "MUSIC_SEARCH_PROVIDER_CIRCUIT_FAILURES", 2, raising=False)
    monkeypatch.setattr(provedores_api.config, "MUSIC_SEARCH_PROVIDER_CIRCUIT_COOLDOWN_SECONDS", 60.0, raising=False)

    api = MusicApiProviders(timeout=2.0)
    api.enabled = True
    api.youtube_api_key = "yt-key"
    api.spotify_client_id = ""
    api.spotify_client_secret = ""
    api.deezer_enabled = False
    api.soundcloud_enabled = False
    chamadas = 0

    async def youtube(query: str, *, limit: int = 5):
        nonlocal chamadas
        chamadas += 1
        raise RuntimeError("youtube temporariamente indisponivel")

    api.youtube_search = youtube  # type: ignore[method-assign]

    assert await api.search_sources("q1", limit=5) == []
    assert await api.search_sources("q2", limit=5) == []
    assert await api.search_sources("q3", limit=5) == []
    assert chamadas == 2
    estado = estado_circuito_provider("youtube")
    assert estado.falhas_consecutivas == 2
    assert estado.aberto_ate > 0
    limpar_resiliencia_metadata()


@pytest.mark.asyncio
async def test_provider_timeout_e_isolado_sem_atrasar_demais_a_busca(monkeypatch) -> None:
    import time as _time

    from cogs.musica.metadados import provedores_api
    from cogs.musica.metadados.resiliencia import limpar_resiliencia_metadata

    limpar_resiliencia_metadata()
    monkeypatch.setattr("cogs.musica.metadados.provedores_api._env", lambda name, default="": "")
    monkeypatch.setattr(provedores_api.config, "MUSIC_SEARCH_PROVIDER_TIMEOUT_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(provedores_api.config, "MUSIC_SEARCH_PROVIDER_CIRCUIT_FAILURES", 3, raising=False)

    api = MusicApiProviders(timeout=2.0)
    api.enabled = True
    api.youtube_api_key = "yt-key"
    api.spotify_client_id = ""
    api.spotify_client_secret = ""
    api.deezer_enabled = False
    api.soundcloud_enabled = False

    async def youtube(query: str, *, limit: int = 5):
        await asyncio.sleep(1.0)
        return [ApiTrackCandidate(title="tarde", provider="youtube")]

    api.youtube_search = youtube  # type: ignore[method-assign]
    inicio = _time.monotonic()
    resultado = await api.search_sources("consulta", limit=5)
    elapsed = _time.monotonic() - inicio

    assert resultado == []
    assert elapsed < 0.6
    limpar_resiliencia_metadata()


@pytest.mark.asyncio
async def test_resolucao_textual_singleflight_compartilha_json_cru_sem_misturar_requester(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento
    from cogs.musica.agente_telefone.coalescencia_resolucao import limpar_coalescencia_resolucao

    limpar_coalescencia_resolucao()
    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", False, raising=False)

    iniciou = asyncio.Event()
    liberar = asyncio.Event()
    chamadas = 0

    async def metadata_fake(query: str, *, limit: int = 5):
        return []

    async def worker_fake(*, base, token, payload, timeout_seconds):
        nonlocal chamadas
        chamadas += 1
        iniciou.set()
        await liberar.wait()
        return {
            "ok": True,
            "metadata_only": True,
            "tracks": [
                {
                    "title": "Numb (Official Audio)",
                    "uploader": "Linkin Park",
                    "webpage_url": "https://youtube.test/numb",
                    "metadata_only": True,
                    "source": "youtube",
                }
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    t1 = asyncio.create_task(
        resolucao.resolve_music_tracks_on_worker(
            "Linkin Park Numb", requester_id=11, requester_name="A", metadata_only=True, guild_id=99
        )
    )
    await iniciou.wait()
    t2 = asyncio.create_task(
        resolucao.resolve_music_tracks_on_worker(
            "Linkin Park Numb", requester_id=22, requester_name="B", metadata_only=True, guild_id=99
        )
    )
    await asyncio.sleep(0)
    liberar.set()
    lote_a, lote_b = await asyncio.gather(t1, t2)

    assert chamadas == 1
    assert lote_a.tracks[0].requester_id == 11
    assert lote_b.tracks[0].requester_id == 22
    assert lote_a.tracks[0] is not lote_b.tracks[0]
    limpar_coalescencia_resolucao()


@pytest.mark.asyncio
async def test_deep_pass_e_suprimido_quando_limite_de_concorrencia_esta_ocupado(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento
    from cogs.musica.busca.resiliencia import (
        liberar_busca_profunda,
        limpar_resiliencia_busca,
        tentar_reservar_busca_profunda,
    )

    limpar_resiliencia_busca()
    assert tentar_reservar_busca_profunda(limite=1) is True
    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_MAX_CONCURRENT", 1, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_MAX_WORKER_INFLIGHT", 10, raising=False)
    chamadas = 0

    async def metadata_fake(query: str, *, limit: int = 5):
        return []

    async def worker_fake(*, base, token, payload, timeout_seconds):
        nonlocal chamadas
        chamadas += 1
        return {
            "ok": True,
            "metadata_only": True,
            "tracks": [
                {
                    "title": "Musica Misteriosa",
                    "uploader": "Canal",
                    "webpage_url": "https://youtube.test/primeira",
                    "metadata_only": True,
                    "source": "youtube",
                }
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    try:
        lote = await resolucao.resolve_music_tracks_on_worker(
            "misteriosa muzika", requester_id=1, requester_name="tester", metadata_only=True, guild_id=99
        )
        assert chamadas == 1
        assert lote.tracks[0].webpage_url == "https://youtube.test/primeira"
    finally:
        liberar_busca_profunda()
        limpar_resiliencia_busca()


@pytest.mark.asyncio
async def test_gain_gate_preserva_fast_pass_quando_deep_nao_melhora(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento
    from cogs.musica.busca import limpar_telemetria_busca, snapshot_telemetria_busca

    limpar_telemetria_busca()
    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_TELEMETRY_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_TELEMETRY_SUMMARY_EVERY", 100, raising=False)
    chamadas = 0

    async def metadata_fake(query: str, *, limit: int = 5):
        return []

    async def worker_fake(*, base, token, payload, timeout_seconds):
        nonlocal chamadas
        chamadas += 1
        return {
            "ok": True,
            "metadata_only": True,
            "tracks": [
                {
                    "title": "Misteriosa Musica",
                    "uploader": "Canal",
                    "webpage_url": "https://youtube.test/primeira",
                    "metadata_only": True,
                    "source": "youtube",
                }
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_fake)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_fake)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "misteriosa muzika",
        requester_id=1,
        requester_name="tester",
        metadata_only=True,
        guild_id=99,
    )

    assert chamadas == 2
    assert len(lote.tracks) == 1
    assert lote.tracks[0].webpage_url == "https://youtube.test/primeira"
    snapshot = snapshot_telemetria_busca()
    assert snapshot.deep_rejeitadas == 1
    assert snapshot.deep_aplicadas == 0
    limpar_telemetria_busca()
