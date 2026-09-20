from __future__ import annotations

from cogs.musica.busca import analisar_consulta, ranquear_faixas
from cogs.musica.nucleo.modelos import MusicTrack


def _track(title: str, uploader: str = "", *, url: str = "https://youtube.test/watch?v=x") -> MusicTrack:
    return MusicTrack(
        title=title,
        webpage_url=url,
        requester_id=1,
        requester_name="tester",
        uploader=uploader,
        source="YouTube",
        extractor="worker-ytdlp",
    )


def test_intencao_preserva_versao_e_identidade() -> None:
    consulta = analisar_consulta("ytsearch: Daft Punk - Get Lucky (Live)")
    assert consulta.prefixo == "ytsearch"
    assert consulta.artista == "daft punk"
    assert consulta.titulo.startswith("get lucky")
    assert "live" in consulta.atributos
    assert "daft" in consulta.tokens
    assert "lucky" in consulta.tokens


def test_busca_neutra_penaliza_cover_karaoke_e_remix_nao_pedidos() -> None:
    tracks = [
        _track("Blinding Lights (Karaoke Version)", "KaraFun"),
        _track("Blinding Lights (Remix)", "Random DJ"),
        _track("The Weeknd - Blinding Lights (Official Audio)", "The Weeknd"),
        _track("Blinding Lights Cover", "Canal de Covers"),
    ]
    ordenadas, ranking = ranquear_faixas("the weeknd blinding lights", tracks)

    assert ordenadas[0] is tracks[2]
    assert ranking[0].score > ranking[-1].score
    assert ranking[0].confianca > 0.5


def test_versao_explicitamente_pedida_muda_o_ranking() -> None:
    original = _track("Numb (Official Audio)", "Linkin Park")
    live = _track("Numb Live in Texas", "Linkin Park")
    cover = _track("Numb Cover", "Outro canal")

    ordenadas, _ = ranquear_faixas("Linkin Park - Numb live", [original, cover, live])
    assert ordenadas[0] is live


def test_artista_titulo_separados_reduzem_falso_positivo() -> None:
    errado = _track("Castle Vein", "Piano Cover Project")
    certo = _track("Castle Vein", "Heaven Pierce Her - Topic")
    outro = _track("Castle on the Hill", "Ed Sheeran")

    ordenadas, ranking = ranquear_faixas("Heaven Pierce Her - Castle Vein", [errado, outro, certo])
    assert ordenadas[0] is certo
    assert ranking[0].sinais.artista > ranking[1].sinais.artista


def test_tolerancia_a_pequeno_typo_sem_dependencia_pesada() -> None:
    certo = _track("Bohemian Rhapsody", "Queen Official")
    errado = _track("Bohemian Like You", "The Dandy Warhols")

    ordenadas, ranking = ranquear_faixas("quen bohemain rapsody", [errado, certo])
    assert ordenadas[0] is certo
    assert ranking[0].score > ranking[1].score


def test_empate_preserva_ordem_original_para_estabilidade() -> None:
    a = _track("Same Song", "Same Artist", url="https://youtube.test/a")
    b = _track("Same Song", "Same Artist", url="https://youtube.test/b")

    ordenadas, ranking = ranquear_faixas("same artist same song", [a, b])
    assert ordenadas == [a, b]
    assert [item.indice_original for item in ranking] == [0, 1]

from cogs.musica.busca import fundir_resultados
from cogs.musica.metadados.modelos import ApiTrackCandidate


def test_fusao_colapsa_mesma_gravacao_de_worker_spotify_e_deezer() -> None:
    worker = _track(
        "The Weeknd - Blinding Lights (Official Audio)",
        "The Weeknd - Topic",
        url="https://youtube.test/watch?v=official",
    )
    outro = _track(
        "Blinding Lights (Live)",
        "The Weeknd",
        url="https://youtube.test/watch?v=live",
    )
    worker.duration = 200
    outro.duration = 215
    spotify = ApiTrackCandidate(
        title="Blinding Lights",
        artist="The Weeknd",
        duration=200,
        provider="spotify",
        source="Spotify",
        webpage_url="https://open.spotify.com/track/abc",
        isrc="USUG11904206",
    )
    deezer = ApiTrackCandidate(
        title="Blinding Lights",
        artist="The Weeknd",
        duration=201,
        provider="deezer",
        source="Deezer",
        webpage_url="https://deezer.test/track/abc",
        isrc="USUG11904206",
    )

    tracks, resumo = fundir_resultados(
        "the weeknd blinding lights",
        [outro, worker],
        [spotify, deezer],
        requester_id=1,
        requester_name="tester",
        limit=5,
    )

    assert len(tracks) == 2
    assert tracks[0].webpage_url == worker.webpage_url
    assert resumo.entradas == 4
    assert resumo.grupos == 2
    assert resumo.duplicatas == 2
    assert {"worker-youtube", "spotify", "deezer"}.issubset(set(resumo.fontes))


def test_fusao_preserva_remix_como_resultado_distinto() -> None:
    original = _track("Get Lucky (Official Audio)", "Daft Punk")
    remix = ApiTrackCandidate(
        title="Get Lucky Remix",
        artist="Daft Punk",
        duration=260,
        provider="spotify",
        webpage_url="https://open.spotify.com/track/remix",
        isrc="FRREMIX00001",
    )
    original.duration = 248

    tracks, resumo = fundir_resultados(
        "Daft Punk - Get Lucky",
        [original],
        [remix],
        limit=5,
    )

    assert len(tracks) == 2
    assert resumo.duplicatas == 0
    assert any("remix" in track.title.lower() for track in tracks)


def test_fusao_por_isrc_vence_pequena_variacao_de_metadata() -> None:
    spotify = ApiTrackCandidate(
        title="Numb",
        artist="Linkin Park",
        duration=185,
        provider="spotify",
        webpage_url="https://open.spotify.com/track/numb",
        isrc="USWB10300474",
    )
    deezer = ApiTrackCandidate(
        title="Numb - 2003 Remaster",
        artist="Linkin Park",
        duration=186,
        provider="deezer",
        webpage_url="https://deezer.test/numb",
        isrc="USWB10300474",
    )

    tracks, resumo = fundir_resultados("Linkin Park Numb", [], [spotify, deezer], limit=5)

    assert len(tracks) == 1
    assert resumo.grupos == 1
    assert resumo.duplicatas == 1


def test_fusao_candidato_metadata_permanece_lazy_sem_stream() -> None:
    spotify = ApiTrackCandidate(
        title="Genesis",
        artist="Grimes",
        duration=255,
        provider="spotify",
        source="Spotify",
        webpage_url="https://open.spotify.com/track/genesis",
        isrc="CAAAA0000001",
    )

    tracks, _ = fundir_resultados("Grimes Genesis", [], [spotify], limit=5)

    assert len(tracks) == 1
    assert tracks[0].extractor == "metadata"
    assert tracks[0].stream_url == ""
    assert tracks[0].display_title == "Genesis"
    assert tracks[0].display_uploader == "Grimes"


def test_deep_pass_nao_roda_quando_primeira_passagem_e_clara() -> None:
    from cogs.musica.busca import avaliar_busca_profunda

    tracks = [
        _track("The Weeknd - Blinding Lights (Official Audio)", "The Weeknd"),
        _track("Blinding Lights Remix", "Random DJ"),
        _track("Blinding Lights Cover", "Cover Channel"),
        _track("Save Your Tears", "The Weeknd"),
        _track("Starboy", "The Weeknd"),
    ]
    ordenadas, ranking = ranquear_faixas("the weeknd blinding lights", tracks)
    decisao = avaliar_busca_profunda(
        "the weeknd blinding lights",
        ordenadas,
        ranking,
        requested_limit=5,
    )

    assert decisao.executar is False
    assert decisao.motivo == "primeira_passagem_suficiente"
    assert decisao.top_score >= 0.9


def test_deep_pass_reformula_typo_usando_melhor_candidato_sem_trocar_ranking_final() -> None:
    from cogs.musica.busca import avaliar_busca_profunda

    tracks = [
        _track("Bohemian Like You", "The Dandy Warhols"),
        _track("Bohemian Rhapsody", "Queen Official"),
        _track("Bohemian Rhapsody Cover", "Cover Band"),
    ]
    ordenadas, ranking = ranquear_faixas("quen bohemain rapsody", tracks)
    decisao = avaliar_busca_profunda(
        "quen bohemain rapsody",
        ordenadas,
        ranking,
        requested_limit=5,
    )

    assert decisao.executar is True
    assert decisao.motivo in {"score_baixo", "poucos_resultados"}
    assert "Bohemian Rhapsody" in decisao.query
    assert decisao.limit == 10


def test_deep_pass_preserva_artista_titulo_e_versao_na_reformulacao() -> None:
    from cogs.musica.busca import avaliar_busca_profunda

    tracks = [_track("Get Lucky Live", "Daft Punk")]
    _, ranking = ranquear_faixas("Daft Punk - Get Lucky live", tracks)
    decisao = avaliar_busca_profunda(
        "Daft Punk - Get Lucky live",
        tracks,
        ranking,
        requested_limit=5,
    )

    assert decisao.executar is True
    assert decisao.query == "daft punk get lucky live"
