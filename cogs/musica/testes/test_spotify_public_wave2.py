from __future__ import annotations

import pytest

from cogs.musica.metadados.fontes.spotify_publico_parser import (
    html_meta,
    json_script_blobs,
    parse_embed_document,
)
from cogs.musica.metadados.modelos import ApiTrackCandidate
from cogs.musica.metadados.provedores_api import MusicApiProviders


def test_parser_embed_playlist_sem_depender_de_classes_css() -> None:
    content = """
    <html><head>
      <title>Minha Playlist | Spotify</title>
      <meta property="og:image" content="https://img.example/cover.jpg">
    </head><body>
      <h1>Minha Playlist</h1>
      <div class="qualquer-coisa"><h3>Faixa &amp; Um</h3><h4>Artista A, Artista B</h4><span>03:21</span></div>
      <div><h3>Faixa Dois</h3><h4>Artista C</h4><span>1:02:03</span></div>
    </body></html>
    """

    doc = parse_embed_document(content, limit=10)

    assert doc.title == "Minha Playlist"
    assert doc.thumbnail == "https://img.example/cover.jpg"
    assert [(r.title, r.artist, r.duration) for r in doc.rows] == [
        ("Faixa & Um", "Artista A, Artista B", 201.0),
        ("Faixa Dois", "Artista C", 3723.0),
    ]


def test_parser_json_aceita_ld_next_e_application_json() -> None:
    content = """
    <script type="application/ld+json">{"name":"ld"}</script>
    <script id="__NEXT_DATA__" type="application/json">{"name":"next"}</script>
    <script type="application/json">{"name":"generic"}</script>
    <script>window.foo = {"name":"nao executar"}</script>
    """
    assert [blob["name"] for blob in json_script_blobs(content)] == ["ld", "next", "generic"]


def test_meta_parser_independe_da_ordem_dos_atributos() -> None:
    content = '<meta content="https://img.example/a.jpg" data-x="1" property="og:image">'
    assert html_meta(content, "og:image") == "https://img.example/a.jpg"


@pytest.mark.asyncio
async def test_playlist_publica_prioriza_embed_e_preserva_ordem(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True
    providers.spotify_public_fallback_max_tracks = 100
    seen_urls: list[str] = []

    async def forbidden_oembed(url: str):
        raise AssertionError("playlist não deve pagar uma requisição oEmbed separada")

    async def text(url: str, *, headers=None, max_bytes: int = 5_000_000):
        seen_urls.append(url)
        return """
        <html><head><meta property="og:image" content="https://img.example/cover.jpg"></head><body>
          <h1>Minha Playlist</h1>
          <h3>Primeira</h3><h4>Artista 1</h4><span>03:00</span>
          <h3>Segunda</h3><h4>Artista 2</h4><span>02:30</span>
        </body></html>
        """

    monkeypatch.setattr(providers, "_spotify_public_oembed", forbidden_oembed)
    monkeypatch.setattr(providers, "_to_thread_text", text)

    batch = await providers.spotify_public_batch_from_url(
        "https://open.spotify.com/playlist/5swQ0HSpbndKvuoYXE9yjO?si=x",
        limit=25,
    )

    assert batch is not None
    assert batch.title == "Minha Playlist"
    assert [track.title for track in batch.tracks] == ["Primeira", "Segunda"]
    assert [track.duration for track in batch.tracks] == [180.0, 150.0]
    assert all(track.thumbnail == "https://img.example/cover.jpg" for track in batch.tracks)
    assert seen_urls == ["https://open.spotify.com/embed/playlist/5swQ0HSpbndKvuoYXE9yjO"]


@pytest.mark.asyncio
async def test_enriquecimento_publico_nao_reordena_playlist() -> None:
    providers = MusicApiProviders()
    items = [
        ApiTrackCandidate(title="Primeira", artist="A", score=1),
        ApiTrackCandidate(title="Segunda", artist="B", score=999),
    ]
    result = await providers._spotify_enrich_candidates(items, limit=10)
    assert [item.title for item in result] == ["Primeira", "Segunda"]


@pytest.mark.asyncio
async def test_track_publico_pode_cair_no_heading_do_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True
    providers.spotify_public_fallback_max_tracks = 100

    async def oembed(url: str):
        return {"title": "Castle Vein", "thumbnail": "https://img.example/castle.jpg"}

    async def text(url: str, *, headers=None, max_bytes: int = 5_000_000):
        return "<html><body><h1>Castle Vein</h1><h2>Heaven Pierce Her</h2></body></html>"

    monkeypatch.setattr(providers, "_spotify_public_oembed", oembed)
    monkeypatch.setattr(providers, "_to_thread_text", text)

    batch = await providers.spotify_public_batch_from_url(
        "https://open.spotify.com/track/3BxXcWY0ZYkNBhiOvy6vWr",
        limit=1,
    )

    assert batch is not None
    assert not batch.is_playlist
    assert len(batch.tracks) == 1
    assert batch.tracks[0].title == "Castle Vein"
    assert batch.tracks[0].artist == "Heaven Pierce Her"
    assert batch.tracks[0].thumbnail == "https://img.example/castle.jpg"


@pytest.mark.asyncio
async def test_id_spotify_invalido_e_rejeitado_antes_de_io(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = MusicApiProviders()
    providers.spotify_public_fallback_enabled = True

    async def forbidden(*args, **kwargs):
        raise AssertionError("I/O não deveria ocorrer")

    monkeypatch.setattr(providers, "_spotify_public_page_batch", forbidden)
    result = await providers.spotify_public_batch_from_url("https://open.spotify.com/playlist/curto", limit=25)
    assert result is None
