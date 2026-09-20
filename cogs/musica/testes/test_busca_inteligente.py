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


def test_intencao_separa_apresentacao_de_variacao_da_gravacao() -> None:
    consulta = analisar_consulta("Daft Punk - Get Lucky official audio clean version")

    assert "clean" in consulta.atributos
    assert {"official", "audio"}.issubset(consulta.apresentacao)
    assert "audio" not in consulta.atributos


def test_clean_bandit_nao_vira_falso_pedido_de_clean_version() -> None:
    consulta = analisar_consulta("Clean Bandit - Rather Be")

    assert "clean" not in consulta.atributos


def test_official_audio_prefere_audio_oficial_a_video_e_lyrics() -> None:
    audio = _track("Bad Romance (Official Audio)", "Lady Gaga")
    video = _track("Lady Gaga - Bad Romance (Official Music Video)", "Lady Gaga")
    lyrics = _track("Bad Romance (Lyrics)", "Lady Gaga")

    ordenadas, ranking = ranquear_faixas(
        "Lady Gaga - Bad Romance official audio",
        [lyrics, video, audio],
    )

    assert ordenadas[0] is audio
    assert ranking[0].sinais.apresentacao > 0
    assert ranking[0].score > ranking[1].score


def test_pedido_lyrics_prefere_lyrics_mesmo_com_audio_oficial_disponivel() -> None:
    audio = _track("Numb (Official Audio)", "Linkin Park")
    lyrics = _track("Numb Lyrics", "Linkin Park")

    ordenadas, ranking = ranquear_faixas("Linkin Park - Numb lyrics", [audio, lyrics])

    assert ordenadas[0] is lyrics
    assert ranking[0].score > ranking[1].score


def test_clean_e_explicit_sao_intencoes_mutuamente_exclusivas() -> None:
    clean = _track("Starboy (Clean)", "The Weeknd")
    explicit = _track("Starboy (Explicit)", "The Weeknd")

    ordenadas_clean, _ = ranquear_faixas("The Weeknd - Starboy clean", [explicit, clean])
    ordenadas_explicit, _ = ranquear_faixas("The Weeknd - Starboy explicit", [clean, explicit])

    assert ordenadas_clean[0] is clean
    assert ordenadas_explicit[0] is explicit


def test_fusao_nao_colapsa_clean_e_explicit_com_mesma_duracao() -> None:
    clean = _track("Starboy (Clean)", "The Weeknd", url="https://youtube.test/clean")
    explicit = _track("Starboy (Explicit)", "The Weeknd", url="https://youtube.test/explicit")
    clean.duration = explicit.duration = 230

    tracks, resumo = fundir_resultados("The Weeknd - Starboy", [clean, explicit], [], limit=5)

    assert len(tracks) == 2
    assert resumo.duplicatas == 0


def test_fusao_preserva_audio_video_e_lyrics_como_opcoes_do_worker() -> None:
    audio = _track("Blinding Lights (Official Audio)", "The Weeknd", url="https://youtube.test/audio")
    video = _track("Blinding Lights (Official Music Video)", "The Weeknd", url="https://youtube.test/video")
    lyrics = _track("Blinding Lights (Lyrics)", "The Weeknd", url="https://youtube.test/lyrics")
    for track in (audio, video, lyrics):
        track.duration = 200

    tracks, resumo = fundir_resultados(
        "The Weeknd - Blinding Lights official audio",
        [video, lyrics, audio],
        [],
        limit=5,
    )

    assert len(tracks) == 3
    assert resumo.duplicatas == 0
    assert tracks[0].webpage_url == audio.webpage_url


def test_deep_pass_preserva_apresentacao_pedida_na_reformulacao() -> None:
    from cogs.musica.busca import avaliar_busca_profunda

    tracks = [_track("Get Lucky (Official Audio)", "Daft Punk")]
    _, ranking = ranquear_faixas("Daft Punk - Get Lucky official audio", tracks)
    decisao = avaliar_busca_profunda(
        "Daft Punk - Get Lucky official audio",
        tracks,
        ranking,
        requested_limit=5,
    )

    assert decisao.executar is True
    assert decisao.query == "daft punk get lucky official audio"


def test_qualidade_penaliza_resultado_excessivamente_longo_em_busca_de_faixa() -> None:
    normal = _track("Numb (Official Audio)", "Linkin Park")
    longo = _track("Numb (Official Audio) 10 Hours", "Reupload Channel")
    normal.duration = 185
    longo.duration = 36000

    ordenadas, ranking = ranquear_faixas("Linkin Park Numb", [longo, normal])

    assert ordenadas[0] is normal
    assert ranking[0].score > ranking[1].score


def test_qualidade_nao_penaliza_conteudo_longo_quando_consulta_pede_full_album() -> None:
    from cogs.musica.busca import pontuar_faixa

    longo = _track("Discovery Full Album", "Daft Punk")
    longo.duration = 3660
    resultado = pontuar_faixa("Daft Punk Discovery full album", longo)

    assert resultado.sinais.penalidade < 0.10


def test_diversidade_evitaria_lista_dominada_por_reuploads_equivalentes() -> None:
    primeiro = _track("Numb (Official Audio)", "Linkin Park", url="https://youtube.test/1")
    reupload_a = _track("Numb (Official Audio)", "Mirror A", url="https://youtube.test/2")
    reupload_b = _track("Numb - Official Audio", "Mirror B", url="https://youtube.test/3")
    video = _track("Numb (Official Music Video)", "Linkin Park", url="https://youtube.test/4")
    for track in (primeiro, reupload_a, reupload_b, video):
        track.duration = 185

    ordenadas, _ = ranquear_faixas(
        "Linkin Park Numb",
        [primeiro, reupload_a, reupload_b, video],
    )

    assert ordenadas[0] is primeiro
    assert ordenadas.index(video) < max(ordenadas.index(reupload_a), ordenadas.index(reupload_b))


def test_diversidade_nao_promove_live_sobre_lyrics_quando_lyrics_foi_pedido() -> None:
    lyrics_a = _track("Numb Lyrics", "Linkin Park", url="https://youtube.test/l1")
    lyrics_b = _track("Numb Lyric Video", "Linkin Park", url="https://youtube.test/l2")
    live = _track("Numb Live", "Linkin Park", url="https://youtube.test/live")
    for track in (lyrics_a, lyrics_b, live):
        track.duration = 185

    ordenadas, _ = ranquear_faixas("Linkin Park Numb lyrics", [lyrics_a, live, lyrics_b])

    assert live is not ordenadas[0]
    assert lyrics_a in ordenadas[:2]
    assert lyrics_b in ordenadas[:2]


def test_estrutura_entende_titulo_by_artista() -> None:
    consulta = analisar_consulta("Numb by Linkin Park")

    assert consulta.artista == "linkin park"
    assert consulta.titulo == "numb"
    assert consulta.estrutura == "by"


def test_estrutura_entende_titulo_entre_aspas_em_ambas_as_ordens() -> None:
    antes = analisar_consulta('Linkin Park "Numb"')
    depois = analisar_consulta('"Numb" Linkin Park')

    for consulta in (antes, depois):
        assert consulta.artista == "linkin park"
        assert consulta.titulo == "numb"
        assert consulta.estrutura == "aspas"


def test_estrutura_extrai_feat_sem_poluir_titulo() -> None:
    consulta = analisar_consulta("The Weeknd - Save Your Tears ft. Ariana Grande")

    assert consulta.artista == "the weeknd"
    assert consulta.titulo == "save your tears"
    assert consulta.colaboradores == ("ariana grande",)
    assert "ft" not in consulta.tokens


def test_estrutura_extrai_feat_no_lado_do_artista() -> None:
    consulta = analisar_consulta("Calvin Harris feat. Rihanna - This Is What You Came For")

    assert consulta.artista == "calvin harris"
    assert consulta.titulo == "this is what you came for"
    assert consulta.colaboradores == ("rihanna",)


def test_banda_live_nao_vira_falso_pedido_de_versao_ao_vivo() -> None:
    consulta = analisar_consulta("Live - Lightning Crashes")

    assert consulta.artista == "live"
    assert consulta.titulo == "lightning crashes"
    assert "live" not in consulta.atributos


def test_by_artist_melhora_desambiguacao_de_titulo_igual() -> None:
    errado = _track("Numb", "Marina")
    certo = _track("Numb", "Linkin Park - Topic")

    ordenadas, ranking = ranquear_faixas("Numb by Linkin Park", [errado, certo])

    assert ordenadas[0] is certo
    assert ranking[0].sinais.artista > ranking[1].sinais.artista


def test_feat_ajuda_a_distinguir_colaboracao_da_versao_sem_convidado() -> None:
    sem_feat = _track("Save Your Tears", "The Weeknd")
    com_feat = _track("The Weeknd - Save Your Tears ft. Ariana Grande", "The Weeknd")

    ordenadas, ranking = ranquear_faixas(
        "The Weeknd - Save Your Tears ft. Ariana Grande",
        [sem_feat, com_feat],
    )

    assert ordenadas[0] is com_feat
    assert ranking[0].score > ranking[1].score


def test_deep_pass_preserva_colaborador_na_query_canonica() -> None:
    from cogs.musica.busca import avaliar_busca_profunda

    track = _track("Save Your Tears ft. Ariana Grande", "The Weeknd")
    ordenadas, ranking = ranquear_faixas(
        "The Weeknd - Save Your Tears ft. Ariana Grande",
        [track],
    )
    decisao = avaliar_busca_profunda(
        "The Weeknd - Save Your Tears ft. Ariana Grande",
        ordenadas,
        ranking,
        requested_limit=5,
    )

    assert decisao.executar is True
    assert "ariana grande" in decisao.query


def test_by_artist_usa_ultimo_by_quando_titulo_contem_by() -> None:
    consulta = analisar_consulta("Stand by Me by Ben E. King")

    assert consulta.titulo == "stand by me"
    assert consulta.artista == "ben e king"


def test_memoria_curta_estabiliza_empate_para_resultado_escolhido() -> None:
    from cogs.musica.busca import limpar_memoria_busca, registrar_selecao_busca

    limpar_memoria_busca()
    a = _track("Same Song", "Same Artist", url="https://youtube.test/a")
    b = _track("Same Song", "Same Artist", url="https://youtube.test/b")

    registrar_selecao_busca(
        "Same Artist - Same Song",
        b,
        guild_id=10,
        requester_id=20,
    )
    ordenadas, ranking = ranquear_faixas(
        "Same Artist - Same Song",
        [a, b],
        guild_id=10,
        requester_id=20,
    )

    assert ordenadas[0] is b
    assert ranking[0].indice_original == 1
    limpar_memoria_busca()


def test_memoria_curta_nao_vaza_entre_usuarios_ou_guilds() -> None:
    from cogs.musica.busca import limpar_memoria_busca, registrar_selecao_busca

    limpar_memoria_busca()
    a = _track("Same Song", "Same Artist", url="https://youtube.test/a")
    b = _track("Same Song", "Same Artist", url="https://youtube.test/b")
    registrar_selecao_busca(
        "Same Artist - Same Song",
        b,
        guild_id=10,
        requester_id=20,
    )

    outra_pessoa, _ = ranquear_faixas(
        "Same Artist - Same Song", [a, b], guild_id=10, requester_id=21
    )
    outra_guild, _ = ranquear_faixas(
        "Same Artist - Same Song", [a, b], guild_id=11, requester_id=20
    )

    assert outra_pessoa[0] is a
    assert outra_guild[0] is a
    limpar_memoria_busca()


def test_memoria_curta_expira_sem_persistencia() -> None:
    from cogs.musica.busca import (
        limpar_memoria_busca,
        obter_preferencia_busca,
        registrar_selecao_busca,
    )

    limpar_memoria_busca()
    track = _track("Same Song", "Same Artist", url="https://youtube.test/b")
    registrar_selecao_busca(
        "Same Artist - Same Song",
        track,
        guild_id=10,
        requester_id=20,
        now=100.0,
    )

    assert obter_preferencia_busca(
        "Same Artist - Same Song", guild_id=10, requester_id=20, now=1299.0
    ) is not None
    assert obter_preferencia_busca(
        "Same Artist - Same Song", guild_id=10, requester_id=20, now=1301.0
    ) is None
    limpar_memoria_busca()


def test_memoria_nao_supera_mudanca_explicita_de_versao() -> None:
    from cogs.musica.busca import limpar_memoria_busca, registrar_selecao_busca

    limpar_memoria_busca()
    live = _track("Numb Live", "Linkin Park", url="https://youtube.test/live")
    lyrics = _track("Numb Lyrics", "Linkin Park", url="https://youtube.test/lyrics")
    registrar_selecao_busca(
        "Linkin Park - Numb live",
        live,
        guild_id=10,
        requester_id=20,
    )

    ordenadas, _ = ranquear_faixas(
        "Linkin Park - Numb lyrics",
        [live, lyrics],
        guild_id=10,
        requester_id=20,
    )

    assert ordenadas[0] is lyrics
    limpar_memoria_busca()


def test_memoria_nao_promove_resultado_claramente_pior() -> None:
    from cogs.musica.busca import limpar_memoria_busca, registrar_selecao_busca

    limpar_memoria_busca()
    errado = _track("Numb Cover", "Cover Channel", url="https://youtube.test/cover")
    certo = _track("Numb Official Audio", "Linkin Park", url="https://youtube.test/official")
    registrar_selecao_busca(
        "Linkin Park - Numb",
        errado,
        guild_id=10,
        requester_id=20,
    )

    ordenadas, ranking = ranquear_faixas(
        "Linkin Park - Numb",
        [errado, certo],
        guild_id=10,
        requester_id=20,
    )

    assert ordenadas[0] is certo
    assert ranking[0].score > ranking[1].score
    limpar_memoria_busca()


def test_gain_gate_rejeita_deep_sem_melhoria() -> None:
    from cogs.musica.busca import avaliar_ganho_busca_profunda
    from cogs.musica.busca.modelos import ResultadoRanking, SinaisCandidato

    a = _track("Numb", "Linkin Park", url="https://youtube.test/a")
    b = _track("Numb Lyrics", "Linkin Park", url="https://youtube.test/b")
    sinais = SinaisCandidato()
    fast = [
        ResultadoRanking(0, 0.88, 0.82, sinais),
        ResultadoRanking(1, 0.76, 0.40, sinais),
    ]
    deep = [
        ResultadoRanking(0, 0.88, 0.82, sinais),
        ResultadoRanking(1, 0.76, 0.40, sinais),
    ]

    decisao = avaliar_ganho_busca_profunda([a, b], fast, [a, b], deep, requested_limit=5)

    assert decisao.aplicar is False
    assert decisao.motivo == "sem_ganho_relevante"


def test_gain_gate_aceita_deep_que_preenche_resultados() -> None:
    from cogs.musica.busca import avaliar_ganho_busca_profunda
    from cogs.musica.busca.modelos import ResultadoRanking, SinaisCandidato

    a = _track("Numb", "Linkin Park", url="https://youtube.test/a")
    b = _track("Numb Lyrics", "Linkin Park", url="https://youtube.test/b")
    sinais = SinaisCandidato()
    fast = [ResultadoRanking(0, 0.88, 0.82, sinais)]
    deep = [
        ResultadoRanking(0, 0.88, 0.82, sinais),
        ResultadoRanking(1, 0.74, 0.35, sinais),
    ]

    decisao = avaliar_ganho_busca_profunda([a], fast, [a, b], deep, requested_limit=5)

    assert decisao.aplicar is True
    assert decisao.motivo == "preencheu_resultados"
    assert decisao.delta_resultados == 1


def test_gain_gate_aceita_melhoria_clara_do_top_score() -> None:
    from cogs.musica.busca import avaliar_ganho_busca_profunda
    from cogs.musica.busca.modelos import ResultadoRanking, SinaisCandidato

    errado = _track("Bohemian Like You", "The Dandy Warhols", url="https://youtube.test/wrong")
    certo = _track("Bohemian Rhapsody", "Queen", url="https://youtube.test/correct")
    sinais = SinaisCandidato()
    fast = [ResultadoRanking(0, 0.61, 0.42, sinais)]
    deep = [ResultadoRanking(0, 0.91, 0.88, sinais)]

    decisao = avaliar_ganho_busca_profunda([errado], fast, [certo], deep, requested_limit=5)

    assert decisao.aplicar is True
    assert decisao.motivo == "top_score_melhor"
    assert decisao.delta_score > 0.2


def test_telemetria_agregada_nao_guarda_query_e_mede_selecao() -> None:
    from cogs.musica.busca import (
        limpar_memoria_busca,
        limpar_telemetria_busca,
        registrar_busca_telemetria,
        registrar_selecao_busca,
        snapshot_telemetria_busca,
    )

    limpar_memoria_busca()
    limpar_telemetria_busca()
    registrar_busca_telemetria(
        cache_hit=False,
        deep_estado="aplicado",
        deep_motivo="score_baixo",
        top_score=0.91,
        top_confianca=0.84,
        elapsed_ms=120.0,
        fontes=("YouTube", "Spotify"),
        resumo_cada=25,
    )
    track = _track("Numb", "Linkin Park")
    registrar_selecao_busca(
        "Linkin Park - Numb",
        track,
        guild_id=10,
        requester_id=20,
        posicao=2,
        total=5,
    )

    snapshot = snapshot_telemetria_busca()
    assert snapshot.buscas == 1
    assert snapshot.deep_solicitadas == 1
    assert snapshot.deep_aplicadas == 1
    assert snapshot.selecoes == 1
    assert snapshot.selecoes_primeiro == 0
    assert snapshot.selecoes_top3 == 1
    assert snapshot.score_medio == 0.91
    assert dict(snapshot.fontes)["youtube"] >= 1
    assert dict(snapshot.motivos_deep)["score_baixo"] == 1
    assert not hasattr(snapshot, "query")
    limpar_memoria_busca()
    limpar_telemetria_busca()
