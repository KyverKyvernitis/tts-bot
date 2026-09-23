from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.musica.agente_telefone import cache_resolucao, comandos, monitor, resolucao, roteamento
from cogs.musica.agente_telefone.modelos import MusicWorkerSelection


def _selection(worker_id: str, endpoint: str) -> MusicWorkerSelection:
    return MusicWorkerSelection(
        True,
        worker_id=worker_id,
        name=worker_id,
        worker={"worker_id": worker_id, "name": worker_id, "endpoint": endpoint},
        reason="ok",
    )


def test_destino_prefere_endpoint_do_worker_selecionado(monkeypatch) -> None:
    monkeypatch.setattr(roteamento.config, "PHONE_WORKER_TOKEN", "token-global", raising=False)
    monkeypatch.setattr(roteamento, "_phone_worker_base_url", lambda: "http://configurado:8766")

    destino = roteamento.destino_da_selecao(_selection("worker-b", "http://worker-b:8766/"))

    assert destino is not None
    assert destino.worker_id == "worker-b"
    assert destino.base == "http://worker-b:8766"
    assert destino.token == "token-global"


@pytest.mark.asyncio
async def test_comandos_da_guild_ficam_no_worker_que_iniciou_sessao(monkeypatch) -> None:
    roteamento.limpar_vinculos_worker()
    monkeypatch.setattr(roteamento.config, "PHONE_WORKER_TOKEN", "token", raising=False)

    selecoes = [_selection("worker-a", "http://worker-a:8766"), _selection("worker-b", "http://worker-b:8766")]
    chamadas_selecao = 0

    async def selecionar():
        nonlocal chamadas_selecao
        item = selecoes[min(chamadas_selecao, len(selecoes) - 1)]
        chamadas_selecao += 1
        return item

    urls: list[str] = []

    async def post_fake(**kwargs):
        urls.append(kwargs["url"])
        return {"ok": True, "state": {}}

    monkeypatch.setattr(comandos, "require_music_worker_available_async", selecionar)
    monkeypatch.setattr(comandos, "post_json_worker", post_fake)

    await comandos.music_agent_command("play", guild_id=77, query="x")
    await comandos.music_agent_command("pause", guild_id=77)
    await comandos.music_agent_status(guild_id=77)

    assert chamadas_selecao == 1
    assert urls == [
        "http://worker-a:8766/task",
        "http://worker-a:8766/task",
        "http://worker-a:8766/task",
    ]
    assert roteamento.destino_vinculado(77).worker_id == "worker-a"

    roteamento.limpar_vinculos_worker()


def test_cache_direto_e_particionado_por_worker_mas_metadata_e_compartilhada() -> None:
    meta_a = cache_resolucao.chave_cache_resolucao("teste", 5, True, worker_scope="worker-a")
    meta_b = cache_resolucao.chave_cache_resolucao("teste", 5, True, worker_scope="worker-b")
    direto_a = cache_resolucao.chave_cache_resolucao("https://x.test/a", 1, False, worker_scope="worker-a")
    direto_b = cache_resolucao.chave_cache_resolucao("https://x.test/a", 1, False, worker_scope="worker-b")

    assert meta_a == meta_b
    assert direto_a != direto_b


@pytest.mark.asyncio
async def test_resolucao_de_guild_ativa_usa_worker_vinculado(monkeypatch) -> None:
    roteamento.limpar_vinculos_worker()
    destino = roteamento.DestinoWorker("worker-a", "A", "http://worker-a:8766", "token")
    roteamento.vincular_guild_worker(123, destino)

    async def selecionar_proibido():
        raise AssertionError("não deve selecionar outro worker para guild vinculada")

    bases: list[str] = []

    async def executar_fake(*, base, token, payload, timeout_seconds):
        bases.append(base)
        return {"ok": True, "tracks": []}

    monkeypatch.setattr(resolucao, "require_music_worker_available_async", selecionar_proibido)
    monkeypatch.setattr(resolucao, "executar_tarefa_resolucao", executar_fake)
    monkeypatch.setattr(resolucao.config, "MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS", 0, raising=False)

    await resolucao.resolve_music_tracks_on_worker(
        "https://example.test/faixa",
        metadata_only=False,
        guild_id=123,
    )

    assert bases == ["http://worker-a:8766"]
    roteamento.limpar_vinculos_worker()


@pytest.mark.asyncio
async def test_monitor_para_e_libera_vinculo_de_worker_morto(monkeypatch) -> None:
    state = SimpleNamespace(agent_monitor_task=None)
    router = SimpleNamespace(get_state=lambda guild_id: state)
    roteamento.limpar_vinculos_worker()
    roteamento.vincular_guild_worker(9, roteamento.DestinoWorker("dead", "Dead", "http://dead:8766", "token"))

    async def status_fake(**kwargs):
        return {"ok": False, "available": False, "error": "connection refused"}

    async def sleep_fake(delay):
        await asyncio.sleep(0)

    real_sleep = asyncio.sleep

    async def yield_once(delay):
        await real_sleep(0)

    monkeypatch.setattr(monitor, "music_agent_status", status_fake)
    monkeypatch.setattr(monitor.asyncio, "sleep", yield_once)

    monitor.iniciar_monitor_music_agent(router, 9)
    task = state.agent_monitor_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert roteamento.destino_vinculado(9) is None


def test_poll_adaptativo_prioriza_inicio_e_relaxa_playing(monkeypatch) -> None:
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_STATUS_POLL_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_PANEL_POLL_SECONDS", 2.0, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_PAUSED_POLL_SECONDS", 3.0, raising=False)

    assert monitor._intervalo_poll_music_agent("preparing") == 0.5
    assert monitor._intervalo_poll_music_agent("starting") == 0.5
    assert monitor._intervalo_poll_music_agent("playing") == 2.0
    assert monitor._intervalo_poll_music_agent("paused") == 3.0


def test_snapshot_local_do_watcher_nao_precisa_consultar_worker() -> None:
    current = SimpleNamespace(
        title="Faixa",
        webpage_url="https://example.test/faixa",
        duration=90,
        uploader="Artista",
        thumbnail="",
        source="youtube",
    )
    state = SimpleNamespace(
        current_status="playing",
        current=current,
        paused=False,
        current_status_detail="playing",
        agent_monitor_task=None,
    )
    router = SimpleNamespace(get_state=lambda guild_id: state)

    snapshot = monitor.estado_local_music_agent(router, 33)

    assert snapshot["status"] == "playing"
    assert snapshot["confirmed_playing"] is True
    assert snapshot["current"]["title"] == "Faixa"


@pytest.mark.asyncio
async def test_monitor_nao_resincroniza_snapshot_com_mesma_revisao(monkeypatch) -> None:
    real_sleep = asyncio.sleep
    state = SimpleNamespace(agent_monitor_task=None, now_message=object(), current_status="playing")
    sync_calls: list[str] = []
    known_revisions: list[str] = []
    status_calls = 0

    class Router:
        def get_state(self, guild_id):
            return state

        async def sync_music_agent_state(self, guild_id, track, remote, **kwargs):
            sync_calls.append(str(remote.get("state_revision") or ""))

    async def status_fake(**kwargs):
        nonlocal status_calls
        status_calls += 1
        known_revisions.append(str(kwargs.get("known_revision") or ""))
        if status_calls == 1:
            return {
                "ok": True,
                "available": True,
                "guilds": {
                    "9": {
                        "status": "playing",
                        "confirmed_playing": True,
                        "voice_connected": True,
                        "player_present": True,
                        "state_revision": "rev-1",
                        "current": {"title": "A", "webpage_url": "https://example.test/a"},
                        "queue": [],
                        "queue_size": 0,
                    }
                },
            }
        if status_calls <= 3:
            return {
                "ok": True,
                "available": True,
                "unchanged": True,
                "state_revision": "rev-1",
                "guilds": {},
            }
        return {"ok": False, "available": False, "error": "offline"}

    async def yield_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(monitor, "music_agent_status", status_fake)
    monkeypatch.setattr(monitor.asyncio, "sleep", yield_sleep)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_PANEL_REFRESH_SECONDS", 300.0, raising=False)

    monitor.iniciar_monitor_music_agent(Router(), 9)
    task = state.agent_monitor_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert sync_calls == ["rev-1"]
    assert known_revisions[:3] == ["", "rev-1", "rev-1"]


@pytest.mark.asyncio
async def test_monitor_transient_failure_recovers_and_resyncs_panel(monkeypatch) -> None:
    real_sleep = asyncio.sleep
    current = SimpleNamespace(title="Faixa A")
    state = SimpleNamespace(
        agent_monitor_task=None,
        now_message=object(),
        current_status="playing",
        current_status_detail="playing",
        current=current,
        music_session_active=True,
        agent_monitor_failures=0,
        agent_monitor_last_error="",
        agent_monitor_reconnecting_since=0.0,
        agent_monitor_recoveries=0,
    )
    panel_updates: list[bool] = []
    sync_calls: list[str] = []
    calls = 0

    class Router:
        def get_state(self, guild_id):
            return state

        def _set_current_status(self, st, status):
            st.current_status = status

        async def update_panel(self, guild_id, *, create=True, repost=False):
            panel_updates.append(bool(create))

        async def sync_music_agent_state(self, guild_id, track, remote, **kwargs):
            sync_calls.append(str(remote.get("status") or ""))
            state.current_status = str(remote.get("status") or "idle")
            state.current = current if remote.get("current") else None

    async def status_fake(**kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return {"ok": False, "available": False, "error": "tailscale route unavailable"}
        if calls == 3:
            return {
                "ok": True,
                "available": True,
                "guilds": {
                    "99": {
                        "status": "playing",
                        "confirmed_playing": True,
                        "voice_connected": True,
                        "player_present": True,
                        "state_revision": "recovered-1",
                        "current": {"title": "Faixa A", "webpage_url": "https://example.test/a"},
                        "queue": [],
                        "queue_size": 0,
                    }
                },
            }
        return {
            "ok": True,
            "available": True,
            "guilds": {
                "99": {
                    "status": "idle",
                    "state_revision": f"idle-{calls}",
                    "current": None,
                    "queue": [],
                    "queue_size": 0,
                }
            },
        }

    async def yield_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(monitor, "music_agent_status", status_fake)
    monkeypatch.setattr(monitor.asyncio, "sleep", yield_sleep)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_REBIND_FAILURES", 4, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_MAX_FAILURES", 8, raising=False)

    monitor.iniciar_monitor_music_agent(Router(), 99)
    task = state.agent_monitor_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert panel_updates == [False]
    assert "playing" in sync_calls
    assert state.agent_monitor_failures == 0
    assert state.agent_monitor_last_error == ""
    assert state.agent_monitor_recoveries == 1


@pytest.mark.asyncio
async def test_tts_remote_command_has_http_headroom_for_synthesis(monkeypatch) -> None:
    roteamento.limpar_vinculos_worker()
    monkeypatch.setattr(roteamento.config, "PHONE_WORKER_TOKEN", "token", raising=False)
    monkeypatch.setattr(comandos.config, "MUSIC_AGENT_TTS_HTTP_HEADROOM_SECONDS", 12.0, raising=False)

    async def selecionar():
        return _selection("worker-a", "http://worker-a:8766")

    seen = {}

    async def post_fake(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "state": {}}

    monkeypatch.setattr(comandos, "require_music_worker_available_async", selecionar)
    monkeypatch.setattr(comandos, "post_json_worker", post_fake)

    await comandos.music_agent_command(
        "tts",
        guild_id=88,
        voice_channel_id=99,
        text="oi",
        timeout_seconds=30.0,
    )

    assert seen["timeout_seconds"] == pytest.approx(42.0)
    roteamento.limpar_vinculos_worker()


def test_poll_de_outage_longo_reduz_pressao_sem_matar_monitor(monkeypatch) -> None:
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_STATUS_POLL_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_PANEL_POLL_SECONDS", 2.0, raising=False)

    assert monitor._intervalo_poll_music_agent("playing", falhas=1) == 2.0
    assert monitor._intervalo_poll_music_agent("playing", falhas=12) >= 4.0
    assert monitor._intervalo_poll_music_agent("playing", falhas=30) >= 6.0


@pytest.mark.asyncio
async def test_monitor_rebind_periodico_sobrevive_outage_e_recupera(monkeypatch) -> None:
    real_sleep = asyncio.sleep
    current = SimpleNamespace(title="Faixa A")
    state = SimpleNamespace(
        agent_monitor_task=None,
        now_message=object(),
        current_status="playing",
        current_status_detail="playing",
        current=current,
        music_session_active=True,
        agent_monitor_failures=0,
        agent_monitor_last_error="",
        agent_monitor_reconnecting_since=0.0,
        agent_monitor_recoveries=0,
    )
    calls = 0
    unbinds: list[int] = []
    synced: list[str] = []

    class Router:
        def get_state(self, guild_id):
            return state

        def _set_current_status(self, st, status):
            st.current_status = status

        async def update_panel(self, guild_id, *, create=True, repost=False):
            return None

        async def sync_music_agent_state(self, guild_id, track, remote, **kwargs):
            synced.append(str(remote.get("status") or ""))
            state.current_status = str(remote.get("status") or "idle")
            state.current = current if remote.get("current") else None

    async def status_fake(**kwargs):
        nonlocal calls
        calls += 1
        if calls <= 5:
            return {"ok": False, "available": False, "error": "route down"}
        if calls == 6:
            return {
                "ok": True,
                "available": True,
                "guilds": {
                    "101": {
                        "status": "playing",
                        "confirmed_playing": True,
                        "voice_connected": True,
                        "player_present": True,
                        "state_revision": "back-1",
                        "current": {"title": "Faixa A", "webpage_url": "https://example.test/a"},
                        "queue": [],
                        "queue_size": 0,
                    }
                },
            }
        return {
            "ok": True,
            "available": True,
            "guilds": {
                "101": {
                    "status": "idle",
                    "state_revision": f"idle-{calls}",
                    "current": None,
                    "queue": [],
                    "queue_size": 0,
                }
            },
        }

    async def yield_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(monitor, "music_agent_status", status_fake)
    monkeypatch.setattr(monitor, "desvincular_guild_worker", lambda gid: unbinds.append(int(gid)))
    monkeypatch.setattr(monitor.asyncio, "sleep", yield_sleep)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_REBIND_FAILURES", 2, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_MAX_FAILURES", 20, raising=False)

    monitor.iniciar_monitor_music_agent(Router(), 101)
    task = state.agent_monitor_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    # Durante 5 falhas, a afinidade é liberada nas falhas 2 e 4; o último
    # unbind ocorre naturalmente quando o snapshot remoto fica idle.
    assert unbinds.count(101) >= 2
    assert "playing" in synced
    assert state.agent_monitor_recoveries == 1
