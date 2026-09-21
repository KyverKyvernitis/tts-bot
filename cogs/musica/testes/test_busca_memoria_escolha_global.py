from __future__ import annotations

import pytest

from cogs.musica.busca import limpar_memoria_busca, registrar_selecao_busca
from cogs.musica.nucleo.modelos import MusicTrack


@pytest.mark.asyncio
async def test_direct_hit_global_pula_worker_provider_ranking_e_seletor(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    escolhido = MusicTrack(
        title="Mili - Compass [Limbus Company]",
        webpage_url="https://youtube.test/compass",
        original_url="https://youtube.test/compass",
        requester_id=111,
        requester_name="Primeiro",
        uploader="Mili",
        duration=169,
        source="youtube",
        extractor="worker-ytdlp",
    )
    registrar_selecao_busca(
        "mili compass",
        escolhido,
        guild_id=1,
        requester_id=111,
    )

    async def nao_pode_consultar_worker(*args, **kwargs):
        raise AssertionError("direct-hit não deve consultar disponibilidade do worker")

    async def nao_pode_consultar_provider(*args, **kwargs):
        raise AssertionError("direct-hit não deve consultar YouTube API")

    monkeypatch.setattr(resolucao, "require_music_worker_available_async", nao_pode_consultar_worker)
    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", nao_pode_consultar_provider)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "  MILI   COMPASS  ",
        requester_id=222,
        requester_name="Segundo",
        limit=3,
        metadata_only=True,
        guild_id=999,
    )

    assert len(lote.tracks) == 1
    assert lote.query == "MILI   COMPASS"
    assert lote.tracks[0].webpage_url == "https://youtube.test/compass"
    assert lote.tracks[0].requester_id == 222
    assert lote.tracks[0].requester_name == "Segundo"
    limpar_memoria_busca()


@pytest.mark.asyncio
async def test_direct_hit_nao_reutiliza_qualificador_diferente(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    escolhido = MusicTrack(
        title="Numb",
        webpage_url="https://youtube.test/numb",
        requester_id=1,
        uploader="Linkin Park",
        source="youtube",
    )
    registrar_selecao_busca("Linkin Park - Numb", escolhido, guild_id=1, requester_id=1)

    chamado = {"worker": 0}

    async def worker_disponivel(*args, **kwargs):
        chamado["worker"] += 1
        raise RuntimeError("parar aqui: prova de miss")

    monkeypatch.setattr(resolucao, "require_music_worker_available_async", worker_disponivel)
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: None)

    with pytest.raises(RuntimeError, match="prova de miss"):
        await resolucao.resolve_music_tracks_on_worker(
            "Linkin Park - Numb live",
            requester_id=2,
            requester_name="Outro",
            metadata_only=True,
            guild_id=2,
        )

    assert chamado["worker"] == 1
    limpar_memoria_busca()


def test_memoria_persistente_sobrevive_recarregamento() -> None:
    from cogs.musica.busca import (
        limpar_memoria_busca,
        obter_escolha_busca,
        recarregar_memoria_busca,
        registrar_selecao_busca,
    )

    limpar_memoria_busca()
    track = MusicTrack(
        title="Mili - Compass",
        webpage_url="https://youtube.test/compass",
        original_url="mili compass",
        requester_id=1,
        uploader="Mili",
        source="youtube",
        extractor="worker-ytdlp",
    )
    assert registrar_selecao_busca("mili compass", track) is True

    recarregar_memoria_busca()
    hit = obter_escolha_busca("MILI COMPASS", requester_id=77, requester_name="Depois do restart")

    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/compass"
    assert hit.requester_id == 77
    assert hit.requester_name == "Depois do restart"
    limpar_memoria_busca()


def test_link_cria_alias_do_titulo_e_primeira_palavra_e_sobrepoe_seletor() -> None:
    from cogs.musica.busca import (
        limpar_memoria_busca,
        obter_escolha_busca,
        registrar_link_busca,
        registrar_selecao_busca,
    )

    limpar_memoria_busca()
    escolha_antiga = MusicTrack(
        title="505 - Arctic Monkeys",
        webpage_url="https://youtube.test/resultado-antigo",
        original_url="505",
        requester_id=1,
        uploader="Outro canal",
        source="youtube",
    )
    registrar_selecao_busca("505", escolha_antiga)

    link = MusicTrack(
        title="Arctic Monkeys - 505 (Official Video)",
        webpage_url="https://youtube.test/505-oficial",
        original_url="https://youtube.test/505-oficial",
        requester_id=2,
        uploader="Arctic Monkeys",
        source="YouTube",
        extractor="worker-ytdlp",
    )
    aliases = registrar_link_busca(link)

    assert "505" in aliases
    hit = obter_escolha_busca("505", requester_id=9)
    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/505-oficial"

    # Uma escolha posterior do menu de três resultados não pode desfazer a
    # autoridade aprendida por link direto.
    posterior = MusicTrack(
        title="Arctic Monkeys - 505 (Lyrics)",
        webpage_url="https://youtube.test/lyrics",
        original_url="505",
        requester_id=3,
        uploader="Lyrics Channel",
        source="youtube",
    )
    assert registrar_selecao_busca("505", posterior) is False
    hit2 = obter_escolha_busca("505", requester_id=10)
    assert hit2 is not None
    assert hit2.webpage_url == "https://youtube.test/505-oficial"
    limpar_memoria_busca()


def test_limite_memoria_configuravel_evicta_mais_antiga(monkeypatch) -> None:
    from cogs.musica import configuracao as config
    from cogs.musica.busca import limpar_memoria_busca, obter_escolha_busca, registrar_selecao_busca

    monkeypatch.setattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_MAX_ENTRIES", 3)
    limpar_memoria_busca()
    for idx in range(4):
        registrar_selecao_busca(
            f"faixa {idx}",
            MusicTrack(
                title=f"Faixa {idx}",
                webpage_url=f"https://youtube.test/{idx}",
                original_url=f"faixa {idx}",
                requester_id=1,
                source="youtube",
            ),
            now=float(idx + 1),
        )

    assert obter_escolha_busca("faixa 0") is None
    assert obter_escolha_busca("faixa 1") is not None
    assert obter_escolha_busca("faixa 3") is not None
    limpar_memoria_busca()


@pytest.mark.asyncio
async def test_playing_de_link_aprende_alias_global_para_busca() -> None:
    from cogs.musica.busca import limpar_memoria_busca, obter_escolha_busca
    from cogs.musica.nucleo.estado import MusicGuildState
    from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente

    limpar_memoria_busca()
    state = MusicGuildState()

    class RouterFake:
        def get_state(self, guild_id: int):
            return state

        def _panel_key_for_track(self, track):
            return (track.webpage_url if track else "") or (track.title if track else "")

        def _set_current_status(self, st, status: str):
            st.current_status = status

        def _reactivate_panel_controls_now(self, guild_id: int):
            return None

        def _schedule_agent_playback_started_effects(self, guild_id: int, key: str):
            return None

        def start_music_agent_monitor(self, guild_id: int, **kwargs):
            return None

    await sincronizar_estado_agente(
        RouterFake(),
        123,
        agent_state={
            "status": "playing",
            "confirmed_playing": True,
            "playback_token": 7,
            "current": {
                "title": "Arctic Monkeys - 505 (Official Video)",
                "uploader": "Arctic Monkeys",
                "webpage_url": "https://youtube.test/watch?v=505",
                "original_url": "https://youtube.test/watch?v=505",
                "duration": 252,
            },
            "queue": [],
            "queue_size": 0,
        },
        create_panel=False,
    )

    hit = obter_escolha_busca("505", requester_id=999)
    assert hit is not None
    assert hit.title == "Arctic Monkeys - 505 (Official Video)"
    assert hit.webpage_url == "https://youtube.test/watch?v=505"
    assert hit.stream_url == ""
    limpar_memoria_busca()

@pytest.mark.asyncio
async def test_direct_hit_aproximado_typo_pula_api_worker_e_ranking(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    registrar_selecao_busca(
        "mili compass",
        MusicTrack(
            title="Mili - Compass",
            webpage_url="https://youtube.test/compass",
            original_url="mili compass",
            requester_id=1,
            uploader="Mili",
            source="youtube",
        ),
    )

    async def proibido(*args, **kwargs):
        raise AssertionError("fuzzy direct-hit nao deve consultar rede")

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", proibido)
    monkeypatch.setattr(resolucao, "require_music_worker_available_async", proibido)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "mili compas",
        requester_id=999,
        requester_name="Outro",
        metadata_only=True,
        guild_id=555,
    )

    assert len(lote.tracks) == 1
    assert lote.tracks[0].webpage_url == "https://youtube.test/compass"
    assert lote.tracks[0].requester_id == 999
    limpar_memoria_busca()


def test_direct_hit_aproximado_aceita_ordem_de_tokens_equivalente() -> None:
    from cogs.musica.busca import obter_escolha_busca

    limpar_memoria_busca()
    registrar_selecao_busca(
        "Arctic Monkeys - 505",
        MusicTrack(
            title="Arctic Monkeys - 505",
            webpage_url="https://youtube.test/505",
            original_url="arctic monkeys 505",
            requester_id=1,
            uploader="Arctic Monkeys",
            source="youtube",
        ),
    )

    hit = obter_escolha_busca("505 arctic monkeys", requester_id=2)
    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/505"
    limpar_memoria_busca()


def test_direct_hit_aproximado_nao_mistura_qualificador() -> None:
    from cogs.musica.busca import obter_escolha_busca

    limpar_memoria_busca()
    registrar_selecao_busca(
        "mili compass",
        MusicTrack(
            title="Mili - Compass",
            webpage_url="https://youtube.test/compass",
            requester_id=1,
            source="youtube",
        ),
    )

    assert obter_escolha_busca("mili compas live") is None
    assert obter_escolha_busca("mili compas lyrics") is None
    limpar_memoria_busca()


def test_direct_hit_aproximado_nao_aproxima_consulta_so_numerica() -> None:
    from cogs.musica.busca import obter_escolha_busca

    limpar_memoria_busca()
    registrar_selecao_busca(
        "505",
        MusicTrack(
            title="Arctic Monkeys - 505",
            webpage_url="https://youtube.test/505",
            requester_id=1,
            source="youtube",
        ),
    )

    assert obter_escolha_busca("506") is None
    limpar_memoria_busca()
