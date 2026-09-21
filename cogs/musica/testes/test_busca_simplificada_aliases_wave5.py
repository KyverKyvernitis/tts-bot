from __future__ import annotations

import pytest

from cogs.musica.busca import (
    limpar_memoria_busca,
    obter_escolha_busca,
    recarregar_memoria_busca,
    registrar_link_busca,
    registrar_selecao_busca,
)
from cogs.musica.nucleo.modelos import MusicTrack


def _track(
    title: str,
    url: str,
    *,
    uploader: str = "",
    original_url: str = "",
) -> MusicTrack:
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
async def test_selecao_aprende_titulo_limpo_e_artista_titulo_sem_rede(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    escolhido = _track(
        "Mili - Compass [Limbus Company]",
        "https://youtube.test/compass",
        uploader="Mili",
    )
    assert registrar_selecao_busca("musica da mili", escolhido) is True

    async def proibido(*args, **kwargs):
        raise AssertionError("alias persistente nao deve consultar rede")

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", proibido)
    monkeypatch.setattr(resolucao, "require_music_worker_available_async", proibido)

    por_titulo = await resolucao.resolve_music_tracks_on_worker(
        "compass",
        requester_id=7,
        requester_name="Alias",
        metadata_only=True,
        guild_id=700,
    )
    por_artista = await resolucao.resolve_music_tracks_on_worker(
        "Mili - Compass",
        requester_id=8,
        requester_name="Alias 2",
        metadata_only=True,
        guild_id=701,
    )

    assert por_titulo.tracks[0].webpage_url == "https://youtube.test/compass"
    assert por_artista.tracks[0].webpage_url == "https://youtube.test/compass"
    limpar_memoria_busca()


def test_aliases_de_selecao_sobrevivem_restart() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "quero essa musica",
        _track(
            "Linkin Park - Numb (Official Video)",
            "https://youtube.test/numb",
            uploader="Linkin Park",
        ),
    )

    recarregar_memoria_busca()
    hit = obter_escolha_busca("numb", requester_id=99)

    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/numb"
    limpar_memoria_busca()


def test_link_continua_soberano_sobre_alias_automatico_da_selecao() -> None:
    limpar_memoria_busca()
    link = _track(
        "Arctic Monkeys - 505 (Official Video)",
        "https://youtube.test/505-oficial",
        uploader="Arctic Monkeys",
        original_url="https://youtube.test/505-oficial",
    )
    registrar_link_busca(link)

    assert registrar_selecao_busca(
        "resultado alternativo",
        _track(
            "Arctic Monkeys - 505 (Lyrics)",
            "https://youtube.test/505-lyrics",
            uploader="Arctic Monkeys",
        ),
    ) is True

    hit = obter_escolha_busca("505")
    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/505-oficial"
    limpar_memoria_busca()


def test_alias_barato_corrige_resultado_titulo_artista_invertido_pelo_uploader() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "musica 505",
        _track(
            "505 - Arctic Monkeys",
            "https://youtube.test/505-invertido",
            uploader="Arctic Monkeys",
        ),
    )

    hit = obter_escolha_busca("505")
    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/505-invertido"
    limpar_memoria_busca()


def test_alias_automatico_preserva_versao_live() -> None:
    limpar_memoria_busca()
    registrar_selecao_busca(
        "quero a versao ao vivo",
        _track(
            "Artist - Song (Live)",
            "https://youtube.test/song-live",
            uploader="Artist",
        ),
    )

    assert obter_escolha_busca("song") is None
    hit = obter_escolha_busca("song live")
    assert hit is not None
    assert hit.webpage_url == "https://youtube.test/song-live"
    limpar_memoria_busca()
