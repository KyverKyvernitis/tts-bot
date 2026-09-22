from __future__ import annotations

import pytest
from pathlib import Path

from cogs.musica.metadados.fontes.spotify_publico import SpotifyPublicoMixin
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.nucleo.playlist_virtual import logical_virtual_queue_count


PLAYLIST = "https://open.spotify.com/playlist/0123456789ABCDEFGHIJKL"
HOME = "https://open.spotify.com/track/0123456789ABCDEFGHIJKL"
BOYS = "https://open.spotify.com/track/ABCDEFGHIJKL0123456789"


def _track(title: str, url: str, duration: int = 180) -> MusicTrack:
    return MusicTrack(
        title=title,
        webpage_url=url,
        original_url=PLAYLIST,
        requester_id=1,
        requester_name="Core",
        duration=duration,
        uploader=title.split(" - ", 1)[0],
        source="Spotify público",
        extractor="metadata",
    )


def test_contagem_total_publica_e_extraida_sem_materializar_playlist() -> None:
    provider = object.__new__(SpotifyPublicoMixin)
    html = '''
    <meta name="description" content="Playlist pública · 137 songs">
    <script type="application/json">
      {"playlistV2":{"content":{"totalCount":137,"items":[]}}}
    </script>
    '''
    assert provider._spotify_public_total_tracks_from_content(html, minimum=25) == 137


@pytest.mark.asyncio
async def test_primeira_janela_propaga_total_e_links_individuais_por_posicao() -> None:
    html = f'''
    <meta name="description" content="Playlist pública · 137 songs">
    <h1>Playlist Pública</h1>
    <h3>Home</h3><h4>Cavetown</h4><span>04:29</span>
    <h3>Boys Will Be Bugs</h3><h4>Cavetown</h4><span>05:29</span>
    <script type="application/json">
    {{"playlistV2":{{"content":{{"totalCount":137,"items":[
      {{"uri":"spotify:track:0123456789ABCDEFGHIJKL","type":"track","name":"Home - Remastered","artists":[{{"name":"Cavetown"}}],"duration_ms":269000}},
      {{"uri":"spotify:track:ABCDEFGHIJKL0123456789","type":"track","name":"Boys Will Be Bugs","artists":[{{"name":"Cavetown"}}],"duration_ms":329000}}
    ]}}}}}}
    </script>
    '''

    class Dummy(SpotifyPublicoMixin):
        spotify_public_fallback_enabled = True
        spotify_public_fallback_max_tracks = 100

        async def _to_thread_text(self, url: str, max_bytes: int = 0) -> str:
            return html

    batch = await Dummy()._spotify_public_page_batch(
        "playlist",
        "0123456789ABCDEFGHIJKL",
        limit=2,
        original_url=PLAYLIST,
    )
    assert batch is not None
    assert batch.playlist_cursor is not None
    assert batch.playlist_cursor.total_tracks == 137
    assert [item.webpage_url for item in batch.tracks] == [HOME, BOYS]


def test_contagem_logica_da_fila_virtual_usa_total_real() -> None:
    # 137 faixas totais; cursor já leu 25 e há 23 antes do marker => duas
    # faixas já saíram da fila (tocada + atual). Restam exatamente 135.
    assert logical_virtual_queue_count(
        total_tracks=137,
        next_offset=25,
        materialized_before=23,
        remote_queue_size=23,
    ) == 135


def test_components_v2_usa_total_exato_e_link_publico_spotify() -> None:
    source = (Path(__file__).resolve().parents[1] / "interface" / "componentes.py").read_text(encoding="utf-8")
    assert "logical_virtual_queue_count(" in source
    assert 'total_text = str(total) if (not virtual or virtual_total_known) else f"{total}+"' in source
    assert 'if not virtual and duration and duration != "desconhecida"' in source
    assert 'profile.platform == "spotify" and profile.resource_type == "track"' in source
    assert 'return f"[{label}]({url})"' in source

