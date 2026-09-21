from __future__ import annotations

import pytest

from cogs.musica.agente_telefone.roteamento import DestinoWorker
from cogs.musica.busca import limpar_memoria_busca
from cogs.musica.metadados.modelos import ApiTrackCandidate


@pytest.mark.asyncio
async def test_api_first_simples_preserva_ordem_e_nao_chama_worker_rank_deep_ou_multifonte(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_SIMPLE_MODE_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_API_FIRST_ENABLED", True, raising=False)

    async def api(query: str, *, limit: int = 3):
        assert query == "505"
        assert limit == 3
        return [
            ApiTrackCandidate(
                title="Arctic Monkeys - 505",
                artist="Pizza Music",
                webpage_url="https://youtube.test/primeiro",
                provider="youtube",
            ),
            ApiTrackCandidate(
                title="505 - Arctic Monkeys",
                artist="Outro canal",
                webpage_url="https://youtube.test/segundo",
                provider="youtube",
            ),
        ]

    async def proibido_async(*args, **kwargs):
        raise AssertionError("API-first suficiente nao deve chamar outro caminho")

    def proibido_sync(*args, **kwargs):
        raise AssertionError("busca simplificada nao deve rankear/deep")

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", api)
    monkeypatch.setattr(resolucao, "require_music_worker_available_async", proibido_async)
    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", proibido_async)
    monkeypatch.setattr(resolucao, "ranquear_faixas", proibido_sync)
    monkeypatch.setattr(resolucao, "avaliar_busca_profunda", proibido_sync)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "505",
        requester_id=7,
        requester_name="Pessoa",
        limit=3,
        metadata_only=True,
        guild_id=99,
    )

    assert [track.title for track in lote.tracks] == [
        "Arctic Monkeys - 505",
        "505 - Arctic Monkeys",
    ]
    assert all(track.extractor == "worker-ytdlp" for track in lote.tracks)
    assert all(track.requester_id == 7 for track in lote.tracks)
    limpar_memoria_busca()


@pytest.mark.asyncio
async def test_api_first_com_um_resultado_nao_busca_para_completar_tres(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_SIMPLE_MODE_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_API_FIRST_ENABLED", True, raising=False)

    async def api(*args, **kwargs):
        return [
            ApiTrackCandidate(
                title="Resultado unico",
                artist="Canal",
                webpage_url="https://youtube.test/unico",
                provider="youtube",
            )
        ]

    async def worker_proibido(*args, **kwargs):
        raise AssertionError("nao deve buscar mais apenas para completar a lista")

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", api)
    monkeypatch.setattr(resolucao, "require_music_worker_available_async", worker_proibido)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "consulta rara",
        metadata_only=True,
        guild_id=123,
    )
    assert [track.title for track in lote.tracks] == ["Resultado unico"]
    limpar_memoria_busca()


@pytest.mark.asyncio
async def test_api_vazia_faz_um_ytsearch3_sem_ranking_multifonte_deep_ou_cache(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao

    limpar_memoria_busca()
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_SIMPLE_MODE_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_API_FIRST_ENABLED", True, raising=False)

    chamadas = {"api": 0, "worker": 0}

    async def api(*args, **kwargs):
        chamadas["api"] += 1
        return []

    async def worker(executor, *, base, token, payload, timeout_seconds):
        chamadas["worker"] += 1
        assert payload["default_search"] == "ytsearch3"
        assert payload["limit"] == 3
        assert payload["metadata_only"] is True
        assert payload["fast_search"] is True
        return {
            "ok": True,
            "metadata_only": True,
            "default_search": "ytsearch3",
            "tracks": [
                {
                    "title": "Primeiro bruto",
                    "webpage_url": "https://youtube.test/1",
                    "uploader": "Canal A",
                    "source": "worker-ytdlp",
                    "metadata_only": True,
                },
                {
                    "title": "Segundo bruto",
                    "webpage_url": "https://youtube.test/2",
                    "uploader": "Canal B",
                    "source": "worker-ytdlp",
                    "metadata_only": True,
                },
                {
                    "title": "Terceiro bruto",
                    "webpage_url": "https://youtube.test/3",
                    "uploader": "Canal C",
                    "source": "worker-ytdlp",
                    "metadata_only": True,
                },
            ],
        }

    async def proibido_async(*args, **kwargs):
        raise AssertionError("multifonte nao deve rodar")

    def proibido_sync(*args, **kwargs):
        raise AssertionError("ranking/deep nao deve rodar")

    monkeypatch.setattr(resolucao, "buscar_candidatos_youtube_fast", api)
    monkeypatch.setattr(
        resolucao,
        "destino_vinculado",
        lambda guild_id: DestinoWorker("phone", "Phone", "http://worker.test", "token"),
    )
    monkeypatch.setattr(resolucao, "executar_resolucao_compartilhada", worker)
    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", proibido_async)
    monkeypatch.setattr(resolucao, "ranquear_faixas", proibido_sync)
    monkeypatch.setattr(resolucao, "avaliar_busca_profunda", proibido_sync)

    primeiro = await resolucao.resolve_music_tracks_on_worker(
        "busca sem memoria",
        metadata_only=True,
        guild_id=1,
    )
    segundo = await resolucao.resolve_music_tracks_on_worker(
        "busca sem memoria",
        metadata_only=True,
        guild_id=1,
    )

    assert [track.title for track in primeiro.tracks] == [
        "Primeiro bruto",
        "Segundo bruto",
        "Terceiro bruto",
    ]
    assert [track.title for track in segundo.tracks] == [
        "Primeiro bruto",
        "Segundo bruto",
        "Terceiro bruto",
    ]
    # Sem escolha registrada, duas pesquisas sequenciais sao duas consultas:
    # nao existe cache transitório de resultados no caminho simplificado.
    assert chamadas == {"api": 2, "worker": 2}
    limpar_memoria_busca()
