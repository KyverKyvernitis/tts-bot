from __future__ import annotations

import asyncio
from pathlib import Path

from cogs.musica.agente_telefone.cache_resolucao import (
    chave_cache_resolucao,
    copiar_lote_para_requisicao,
)
from cogs.musica.agente_telefone.conversao_resolucao import converter_resposta_resolucao
from cogs.musica.agente_telefone.resolucao import _limite_resolucao, _montar_tarefa_resolucao
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
    spotify = (RAIZ_MUSICA / "metadados" / "fontes" / "spotify.py").read_text(encoding="utf-8")

    assert "def spotify_search" not in fachada
    assert "def spotify_batch_from_url" not in fachada
    assert "class ProvedorSpotifyMixin" in spotify
    assert "def spotify_search" in spotify
    assert "def spotify_batch_from_url" in spotify
    for proibido in ("FFmpegPCMAudio", "LavalinkBackend", "yt_dlp.YoutubeDL", "voice_client.play"):
        assert proibido not in spotify


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
