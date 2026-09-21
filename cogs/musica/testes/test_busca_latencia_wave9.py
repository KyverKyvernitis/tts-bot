from __future__ import annotations

import asyncio
import time

import pytest


def test_grace_metadata_adaptativo_preserva_base_sem_historico() -> None:
    from cogs.musica.busca.latencia import grace_metadata_adaptativo, limpar_latencia_busca

    limpar_latencia_busca()
    assert grace_metadata_adaptativo(0.08, min_seconds=0.015, min_samples=4) == pytest.approx(0.08)


def test_grace_metadata_adaptativo_encurta_metadata_lenta_e_inutil() -> None:
    from cogs.musica.busca.latencia import (
        grace_metadata_adaptativo,
        limpar_latencia_busca,
        registrar_latencia_metadata,
    )

    limpar_latencia_busca()
    for _ in range(4):
        registrar_latencia_metadata(elapsed_ms=140.0, util=False)
    assert grace_metadata_adaptativo(0.08, min_seconds=0.015, min_samples=4) == pytest.approx(0.015)
    limpar_latencia_busca()


def test_grace_metadata_adaptativo_mantem_folga_para_metadata_rapida_e_util() -> None:
    from cogs.musica.busca.latencia import (
        grace_metadata_adaptativo,
        limpar_latencia_busca,
        registrar_latencia_metadata,
    )

    limpar_latencia_busca()
    for elapsed in (20.0, 24.0, 18.0, 22.0):
        registrar_latencia_metadata(elapsed_ms=elapsed, util=True)
    grace = grace_metadata_adaptativo(0.08, min_seconds=0.015, min_samples=4)
    assert 0.025 <= grace <= 0.04
    limpar_latencia_busca()


def test_timeout_deep_adaptativo_reduz_cauda_sem_ficar_abaixo_do_piso() -> None:
    from cogs.musica.busca.latencia import (
        limpar_latencia_busca,
        registrar_latencia_deep,
        timeout_deep_adaptativo,
    )

    limpar_latencia_busca()
    assert timeout_deep_adaptativo(5.0, min_seconds=3.0, min_samples=4) == pytest.approx(5.0)
    for elapsed in (420.0, 510.0, 460.0, 490.0):
        registrar_latencia_deep(elapsed_ms=elapsed, sucesso=True)
    assert timeout_deep_adaptativo(5.0, min_seconds=3.0, min_samples=4) == pytest.approx(3.0)
    limpar_latencia_busca()


def test_snapshot_latencia_expoe_metadata_e_deep() -> None:
    from cogs.musica.busca.latencia import (
        limpar_latencia_busca,
        registrar_latencia_deep,
        registrar_latencia_metadata,
        snapshot_latencia_busca,
    )

    limpar_latencia_busca()
    registrar_latencia_metadata(elapsed_ms=40.0, util=True)
    registrar_latencia_deep(elapsed_ms=700.0, sucesso=False)
    snap = snapshot_latencia_busca()
    assert snap.metadata_amostras == 1
    assert snap.metadata_uteis == 1
    assert snap.metadata_ewma_ms == pytest.approx(40.0)
    assert snap.deep_amostras == 1
    assert snap.deep_sucessos == 0
    assert snap.deep_ewma_ms == pytest.approx(700.0)
    limpar_latencia_busca()


@pytest.mark.asyncio
async def test_resolucao_aplica_grace_adaptativo_pos_worker(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento
    from cogs.musica.agente_telefone.coalescencia_resolucao import limpar_coalescencia_resolucao
    from cogs.musica.busca.latencia import limpar_latencia_busca, registrar_latencia_metadata

    limpar_coalescencia_resolucao()
    limpar_latencia_busca()
    for _ in range(2):
        registrar_latencia_metadata(elapsed_ms=200.0, util=False)

    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_API_FIRST_ENABLED", False, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_SIMPLE_MODE_ENABLED", False, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", False, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_ADAPTIVE_TAIL_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_ADAPTIVE_TAIL_MIN_SAMPLES", 2, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_METADATA_AFTER_WORKER_GRACE_SECONDS", 0.08, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_METADATA_GRACE_MIN_SECONDS", 0.005, raising=False)

    async def metadata_lenta(query: str, *, limit: int = 3, **kwargs):
        await asyncio.sleep(0.5)
        return []

    async def worker_rapido(*, base, token, payload, timeout_seconds):
        return {
            "ok": True,
            "metadata_only": True,
            "default_search": "ytsearch3",
            "tracks": [
                {
                    "title": "Resultado incerto",
                    "uploader": "Canal",
                    "webpage_url": "https://youtube.test/1",
                    "metadata_only": True,
                    "source": "youtube",
                }
            ],
        }

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_lenta)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_rapido)

    started = time.monotonic()
    lote = await resolucao.resolve_music_tracks_on_worker(
        "consulta obscura",
        requester_id=7,
        requester_name="tester",
        limit=3,
        metadata_only=True,
        guild_id=999,
    )
    elapsed = time.monotonic() - started

    assert len(lote.tracks) == 1
    assert elapsed < 0.06
    limpar_coalescencia_resolucao()
    limpar_latencia_busca()


@pytest.mark.asyncio
async def test_resolucao_aplica_timeout_deep_adaptativo(monkeypatch) -> None:
    from cogs.musica.agente_telefone import resolucao, roteamento
    from cogs.musica.agente_telefone.busca_profunda import ResultadoPassagemProfunda
    from cogs.musica.agente_telefone.coalescencia_resolucao import limpar_coalescencia_resolucao
    from cogs.musica.busca.latencia import limpar_latencia_busca, registrar_latencia_deep
    from cogs.musica.busca.resiliencia import limpar_resiliencia_busca

    limpar_coalescencia_resolucao()
    limpar_resiliencia_busca()
    limpar_latencia_busca()
    for _ in range(2):
        registrar_latencia_deep(elapsed_ms=450.0, sucesso=True)

    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    monkeypatch.setattr(resolucao, "destino_vinculado", lambda guild_id: destino)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_API_FIRST_ENABLED", False, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_SIMPLE_MODE_ENABLED", False, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_MAX_CONCURRENT", 1, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_MAX_WORKER_INFLIGHT", 10, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_ADAPTIVE_TAIL_ENABLED", True, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_ADAPTIVE_TAIL_MIN_SAMPLES", 2, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_TIMEOUT_SECONDS", 5.0, raising=False)
    monkeypatch.setattr(resolucao.config, "MUSIC_SEARCH_DEEP_TIMEOUT_MIN_SECONDS", 3.0, raising=False)

    async def metadata_vazia(query: str, *, limit: int = 3, **kwargs):
        return []

    async def worker_rapido(*, base, token, payload, timeout_seconds):
        return {
            "ok": True,
            "metadata_only": True,
            "default_search": "ytsearch3",
            "tracks": [
                {
                    "title": "Resultado incerto",
                    "uploader": "Canal",
                    "webpage_url": "https://youtube.test/1",
                    "metadata_only": True,
                    "source": "youtube",
                }
            ],
        }

    timeout_recebido: list[float] = []

    async def deep_fake(**kwargs):
        timeout_recebido.append(float(kwargs["timeout_seconds"]))
        return ResultadoPassagemProfunda(elapsed_ms=20.0)

    monkeypatch.setattr(resolucao, "buscar_candidatos_multifonte", metadata_vazia)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", worker_rapido)
    monkeypatch.setattr(resolucao, "executar_passagem_profunda", deep_fake)

    lote = await resolucao.resolve_music_tracks_on_worker(
        "consulta obscura",
        requester_id=7,
        requester_name="tester",
        limit=3,
        metadata_only=True,
        guild_id=999,
    )

    assert len(lote.tracks) == 1
    assert timeout_recebido == [pytest.approx(3.0)]
    limpar_coalescencia_resolucao()
    limpar_resiliencia_busca()
    limpar_latencia_busca()
