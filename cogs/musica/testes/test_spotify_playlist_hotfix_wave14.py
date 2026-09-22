from __future__ import annotations

import pytest

from cogs.musica.busca import (
    limpar_memoria_busca,
    obter_escolha_busca,
    registrar_lote_link_busca,
)
from cogs.musica.legado.extrator_local import MusicExtractor
from cogs.musica.metadados.fontes.spotify_publico_parser import parse_embed_document
from cogs.musica.metadados.modelos import ApiTrackCandidate
from cogs.musica.nucleo.modelos import MusicTrack


SPOTIFY_HOME = "https://open.spotify.com/track/0123456789ABCDEFGHIJKL"
SPOTIFY_BOYS = "https://open.spotify.com/track/ABCDEFGHIJKL0123456789"


def _spotify_track(title: str, url: str) -> MusicTrack:
    track = MusicTrack(
        title=f"Cavetown - {title}",
        webpage_url=url,
        original_url=url,
        requester_id=1,
        uploader="Cavetown",
        source="Spotify público",
        extractor="metadata",
        duration=240,
    )
    track.display_title = track.title
    track.display_uploader = "Cavetown"
    track.display_source = "Spotify público"
    return track


def test_embed_preserva_link_individual_da_faixa_spotify() -> None:
    html = """
    <main>
      <a href="/track/0123456789ABCDEFGHIJKL">
        <h3>Home</h3><h4>Cavetown</h4><span>04:29</span>
      </a>
      <div data-uri="spotify:track:ABCDEFGHIJKL0123456789">
        <h3>Boys Will Be Bugs</h3><h4><span>E</span> Cavetown</h4><span>05:29</span>
      </div>
    </main>
    """
    document = parse_embed_document(html, limit=5)
    assert [row.webpage_url for row in document.rows] == [SPOTIFY_HOME, SPOTIFY_BOYS]
    assert document.rows[1].artist == "Cavetown"


@pytest.mark.asyncio
async def test_html_sem_href_completa_links_pelo_json_sem_reordenar() -> None:
    from cogs.musica.metadados.fontes.spotify_publico import SpotifyPublicoMixin

    html = f"""
    <h1>Playlist</h1>
    <h3>Home</h3><h4>Cavetown</h4><span>04:29</span>
    <h3>Boys Will Be Bugs</h3><h4>Cavetown</h4><span>05:29</span>
    <script type="application/json">
    {{"items":[
      {{"uri":"spotify:track:0123456789ABCDEFGHIJKL","type":"track","name":"Home","artists":[{{"name":"Cavetown"}}],"duration_ms":269000}},
      {{"uri":"spotify:track:ABCDEFGHIJKL0123456789","type":"track","name":"Boys Will Be Bugs","artists":[{{"name":"Cavetown"}}],"duration_ms":329000}}
    ]}}
    </script>
    """

    class Dummy(SpotifyPublicoMixin):
        spotify_public_fallback_enabled = True
        spotify_public_fallback_max_tracks = 100

        async def _to_thread_text(self, url: str, max_bytes: int = 0) -> str:
            return html

    provider = Dummy()
    batch = await provider._spotify_public_page_batch(
        "playlist",
        "0123456789ABCDEFGHIJKL",
        limit=2,
        original_url="https://open.spotify.com/playlist/0123456789ABCDEFGHIJKL",
    )
    assert batch is not None
    assert [track.title for track in batch.tracks] == ["Home", "Boys Will Be Bugs"]
    assert [track.webpage_url for track in batch.tracks] == [SPOTIFY_HOME, SPOTIFY_BOYS]


def test_metadata_track_preserva_spotify_track_url_para_ui_e_origem() -> None:
    extractor = object.__new__(MusicExtractor)
    candidate = ApiTrackCandidate(
        title="Home",
        artist="Cavetown",
        webpage_url=SPOTIFY_HOME,
        source="Spotify público",
        provider="spotify",
        query="Cavetown Home official audio",
    )
    track = extractor._metadata_track_from_candidate(
        candidate,
        requester_id=1,
        requester_name="Core",
        original_url="https://open.spotify.com/playlist/playlist1234567890",
    )
    assert track.webpage_url == SPOTIFY_HOME
    assert track.original_url == "https://open.spotify.com/playlist/playlist1234567890"

    # Depois de resolver o áudio, webpage_url pode ser YouTube. O renderer V2
    # deve preferir explicitamente o track Spotify preservado em original_url.
    from pathlib import Path

    track.webpage_url = "https://www.youtube.com/watch?v=home123"
    root = Path(__file__).resolve().parents[1]
    source = (root / "interface" / "componentes.py").read_text(encoding="utf-8")
    assert 'profile.platform == "spotify" and profile.resource_type == "track"' in source
    assert 'getattr(track, "original_url", "")' in source


def test_worker_promove_link_da_faixa_spotify_sem_perder_deteccao_de_playlist() -> None:
    from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin

    resolver = object.__new__(ResolucaoMixin)
    meta = {
        "title": "Cavetown - Home",
        "uploader": "Cavetown",
        "source": "Spotify público",
        "extractor": "metadata",
        "webpage_url": SPOTIFY_HOME,
        "original_url": "https://open.spotify.com/playlist/playlist1234567890",
        "query": "ytsearch1:Cavetown - Home official audio",
    }
    assert resolver._is_metadata_collection_item(meta) is True
    track = resolver._agent_track_from_resolved(
        {
            "title": "Cavetown - Home",
            "uploader": "Cavetown",
            "webpage_url": "https://www.youtube.com/watch?v=home123",
            "stream_url": "https://rr.example/home",
            "audio_format_id": "251",
        },
        query=meta["query"],
        track_meta=meta,
        body={},
    )
    assert track.webpage_url == "https://www.youtube.com/watch?v=home123"
    assert track.original_url == SPOTIFY_HOME


def test_playlist_alimenta_aliases_de_link_e_nome_vira_direct_hit() -> None:
    limpar_memoria_busca()
    try:
        tracks = [
            _spotify_track("Home", SPOTIFY_HOME),
            _spotify_track("Boys Will Be Bugs", SPOTIFY_BOYS),
        ]
        registrados = registrar_lote_link_busca(tracks)
        assert registrados >= 4

        home = obter_escolha_busca("Home", requester_id=9)
        assert home is not None
        assert home.original_url == SPOTIFY_HOME

        # Reusa exatamente o alias agressivo já existente para links: primeira
        # palavra útil do título também passa a ser direct play.
        boys = obter_escolha_busca("Boys", requester_id=9)
        assert boys is not None
        assert boys.original_url == SPOTIFY_BOYS
    finally:
        limpar_memoria_busca()


@pytest.mark.asyncio
async def test_nome_aprendido_da_playlist_nao_consulta_api_de_busca(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    try:
        registrar_lote_link_busca([_spotify_track("Home", SPOTIFY_HOME)])

        async def proibido(*args, **kwargs):
            raise AssertionError("direct-hit aprendido da playlist não deve consultar rede")

        monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", proibido)
        monkeypatch.setattr(resolucao, "require_music_worker_available_async", proibido)

        batch = await resolucao.resolve_music_tracks_on_worker(
            "Home",
            requester_id=55,
            requester_name="Teste",
            metadata_only=True,
            limit=3,
            guild_id=123,
        )
        assert len(batch.tracks) == 1
        assert batch.tracks[0].original_url == SPOTIFY_HOME
        assert batch.tracks[0].requester_id == 55
    finally:
        limpar_memoria_busca()


def test_refills_spotify_continuam_alimentando_memoria_direct() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "reproducao" / "playlist_virtual.py").read_text(encoding="utf-8")
    assert 'cursor.provider.startswith("spotify")' in source
    assert "registrar_lote_link_busca(batch.tracks)" in source
