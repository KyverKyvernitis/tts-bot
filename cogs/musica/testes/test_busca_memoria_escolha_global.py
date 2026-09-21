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
        raise AssertionError("direct-hit não deve consultar provider")

    def nao_pode_rankear(*args, **kwargs):
        raise AssertionError("direct-hit não deve executar ranking")

    monkeypatch.setattr(resolucao, "require_music_worker_available_async", nao_pode_consultar_worker)
    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", nao_pode_consultar_provider)
    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", nao_pode_consultar_provider)
    monkeypatch.setattr(resolucao, "ranquear_faixas", nao_pode_rankear)

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
