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
