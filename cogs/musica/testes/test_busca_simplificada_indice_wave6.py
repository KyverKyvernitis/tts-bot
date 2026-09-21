from __future__ import annotations

import pytest

from cogs.musica.busca import (
    esquecer_escolha_busca,
    limpar_memoria_busca,
    obter_escolha_busca,
    registrar_link_busca,
    registrar_selecao_busca,
)
from cogs.musica.nucleo.modelos import MusicTrack


def _track(title: str, url: str, *, uploader: str = "", original_url: str = "") -> MusicTrack:
    return MusicTrack(
        title=title,
        webpage_url=url,
        original_url=original_url or url,
        requester_id=1,
        uploader=uploader,
        source="YouTube",
        extractor="worker-ytdlp",
    )


@pytest.mark.asyncio
async def test_subconjunto_de_tokens_vira_direct_hit_sem_rede(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    registrar_selecao_busca(
        "arctic monkeys 505 official video",
        _track("Arctic Monkeys - 505", "https://youtube.test/505", uploader="Arctic Monkeys"),
    )

    async def proibido(*args, **kwargs):
        raise AssertionError("direct-hit lexical nao deve consultar rede")

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", proibido)
    monkeypatch.setattr(resolucao, "require_music_worker_available_async", proibido)

    batch = await resolucao.resolve_music_tracks_on_worker(
        "arctic 505 monkeys",
        requester_id=50,
        requester_name="Lexical",
        metadata_only=True,
        guild_id=900,
    )
    assert batch.tracks[0].webpage_url == "https://youtube.test/505"
    limpar_memoria_busca()


def test_superconjunto_de_tokens_reaproveita_escolha() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "mili compass",
        _track("Mili - Compass", "https://youtube.test/compass", uploader="Mili"),
    )
    hit = obter_escolha_busca("mili compass limbus")
    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/compass"
    limpar_memoria_busca()


def test_numeros_diferentes_nao_aproximam() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "arctic monkeys 505",
        _track("Arctic Monkeys - 505", "https://youtube.test/505", uploader="Arctic Monkeys"),
    )
    assert obter_escolha_busca("arctic monkeys 506") is None
    limpar_memoria_busca()


def test_intencao_live_nao_se_mistura_com_studio() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "artist song",
        _track("Artist - Song", "https://youtube.test/song", uploader="Artist"),
    )
    assert obter_escolha_busca("artist song live") is None
    limpar_memoria_busca()


def test_link_vence_quando_ha_multiplos_hits_lexicais() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "mili compass game",
        _track("Mili - Compass", "https://youtube.test/menu", uploader="Mili"),
        now=10.0,
    )
    registrar_link_busca(
        _track(
            "Mili - Compass (Official Audio)",
            "https://youtube.test/link",
            uploader="Mili",
            original_url="https://youtube.test/link",
        ),
        now=5.0,
    )
    hit = obter_escolha_busca("mili compass extra")
    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/link"
    limpar_memoria_busca()


def test_esquecer_remove_alias_do_indice_lexical() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "alpha beta gamma",
        _track("Different Song", "https://youtube.test/rare", uploader="Different Artist"),
    )
    assert obter_escolha_busca("alpha beta gamma extra") is not None
    assert esquecer_escolha_busca("alpha beta gamma") is True
    # Outros aliases ensinados pela Wave 5 podem continuar válidos; esta forma
    # exclusiva do alias removido não pode sobreviver pelo índice invertido.
    assert obter_escolha_busca("alpha beta gamma extra") is None
    limpar_memoria_busca()
