from __future__ import annotations

import pytest

from cogs.musica.interface import componentes
from cogs.musica.metadados.fontes.spotify_publico import SpotifyPublicoMixin
from cogs.musica.nucleo.estado import MusicGuildState
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


def test_components_v2_usa_total_logico_remoto_e_link_publico_spotify() -> None:
    tracks = [_track("Cavetown - Home", HOME), _track("Cavetown - Boys Will Be Bugs", BOYS)]
    state = MusicGuildState()
    state.forward_queue.extend(tracks)
    state.agent_remote_queue_size = 205
    state.agent_virtual_playlists = [
        {"active": True, "total_tracks": 137, "next_offset": 25, "source_url": PLAYLIST},
        {"active": True, "total_tracks": 88, "next_offset": 44, "source_url": PLAYLIST + "?second=1"},
    ]
    # Renderiza a fila sem criar controles Discord: o total autoritativo deve
    # vencer a janela de duas faixas e a reconstrução por um único cursor.
    view = object.__new__(componentes.QueueView)
    view.page = 0
    view.selected_position = None
    rendered = view._queue_text(state, tracks)
    assert "## 📜 Fila · 205 músicas" in rendered
    assert "205+ músicas" not in rendered
    assert "2 playlists na fila" in rendered
    assert f"[Cavetown - Home]({HOME})" in rendered
    assert f"]({PLAYLIST})" not in rendered

    # A origem Spotify individual também vence a URL do áudio YouTube.
    tracks[0].original_url = HOME
    tracks[0].webpage_url = "https://www.youtube.com/watch?v=abc123"
    assert componentes._track_link_v2(tracks[0]) == f"[Cavetown - Home]({HOME})"
    assert componentes._track_link_v2(_track("Coleção", PLAYLIST)) == "Coleção"

    state.agent_virtual_playlists[1]["total_tracks"] = None
    assert "## 📜 Fila · 205+ músicas" in view._queue_text(state, tracks)



def test_contagem_total_fallback_conta_linhas_sem_materializar_objetos() -> None:
    provider = object.__new__(SpotifyPublicoMixin)
    rows = "".join(f"<h3>Faixa {i}</h3><h4>Artista</h4><span>03:00</span>" for i in range(61))
    html = f"<h1>Playlist</h1>{rows}"
    assert provider._spotify_public_total_tracks_from_content(html, minimum=25) == 61
