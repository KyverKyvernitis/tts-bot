"""Admissão reaproveitada sem renovar TTL ou trocar o dono da sessão."""
from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.musica.agente_telefone import comandos, roteamento, selecao
from cogs.musica.agente_telefone.modelos import MusicWorkerSelection, MusicWorkerUnavailable


def immediate_retries(monkeypatch):
    monkeypatch.setattr(comandos, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, current_task=asyncio.current_task,
        Task=asyncio.Task, CancelledError=asyncio.CancelledError, sleep=AsyncMock()))
    monkeypatch.setattr(comandos, "_DEFERRED_TASKS", {})
    monkeypatch.setattr(comandos, "_DEFERRED_STATE", {})


@pytest.fixture
def admission(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(roteamento, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(selecao, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(roteamento, "_DESTINOS_GUILD", {})
    monkeypatch.setattr(roteamento, "_DIRECT_HEALTH_CACHE", None)
    monkeypatch.setattr(selecao, "_SELECTION_CACHE", {"at": 0.0, "selection": None})
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_VOICE_EXECUTOR", "termux")
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_ENABLED", False)
    monkeypatch.setattr(roteamento.config, "PHONE_WORKER_TOKEN", "test-token")
    monkeypatch.setattr(roteamento.config, "MUSIC_WORKER_SELECTION_CACHE_SECONDS", 0.8, raising=False)

    def selection(*, endpoint="http://phone:8766", available=True, ttl=0.8):
        result = MusicWorkerSelection(available, worker_id="phone", worker={"endpoint": endpoint})
        roteamento.stamp_music_worker_selection(
            result, started_at=clock[0], ttl=ttl,
            config_key=roteamento._validation_config_key(),
        )
        return result

    return clock, selection


@pytest.mark.asyncio
async def test_valid_context_reuses_admission_without_serializing_it(admission, monkeypatch):
    _clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    assert context is not None
    ensure = AsyncMock(side_effect=AssertionError("admission repeated"))
    require = AsyncMock(side_effect=AssertionError("selection repeated"))
    post = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(comandos, "ensure_music_worker_available", ensure)
    monkeypatch.setattr(comandos, "require_music_worker_available_async", require)
    monkeypatch.setattr(comandos, "post_json_destino", post)

    await comandos.music_agent_command("prepare_voice", guild_id=7, voice_channel_id=9,
                                       worker_validation=context, command_id="stable-id")
    await comandos.music_agent_command("play", guild_id=7, voice_channel_id=9,
                                       worker_validation=context, command_id="stable-id")

    ensure.assert_not_awaited()
    require.assert_not_awaited()
    assert post.await_count == 2
    for call in post.await_args_list:
        assert call.kwargs["destino"].base == "http://phone:8766"
        assert call.kwargs["payload"]["command_id"] == "stable-id"
        assert "worker_validation" not in call.kwargs["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "guild", "token"])
async def test_stale_or_foreign_context_requires_full_admission(admission, monkeypatch, change):
    clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    guild = 7
    if change == "expired":
        clock[0] += 1.0
    elif change == "guild":
        guild = 8
    else:
        monkeypatch.setattr(roteamento.config, "PHONE_WORKER_TOKEN", "replacement-token")
    refresh = AsyncMock(return_value=selection())
    post = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(comandos, "ensure_music_worker_available", refresh)
    monkeypatch.setattr(comandos, "post_json_destino", post)
    await comandos.music_agent_command("prepare_voice", guild_id=guild, worker_validation=context)
    refresh.assert_awaited_once()
    assert post.await_args.kwargs["destino"].token == roteamento.config.PHONE_WORKER_TOKEN


@pytest.mark.asyncio
async def test_expired_context_does_not_authorize_a_different_bound_owner(admission, monkeypatch):
    clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    clock[0] += 1.0
    owner = roteamento.DestinoWorker("other", "Other", "http://other:8766", "other-token")
    roteamento.vincular_guild_worker(7, owner)
    monkeypatch.setattr(comandos, "ensure_music_worker_available", AsyncMock(return_value=selection()))
    post = AsyncMock()
    monkeypatch.setattr(comandos, "post_json_destino", post)
    with pytest.raises(MusicWorkerUnavailable):
        await comandos.music_agent_command("play", guild_id=7, worker_validation=context)
    post.assert_not_awaited()
    assert roteamento.destino_vinculado(7) is owner


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_refresh", [False, True])
async def test_expiry_outage_retries_only_after_full_admission_recovers(admission, monkeypatch, endpoint_refresh):
    clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    clock[0] += 1.0
    immediate_retries(monkeypatch)
    recovered = selection(endpoint="http://phone-new:8766" if endpoint_refresh else "http://phone:8766")
    unavailable = MusicWorkerSelection(False, reason="music_agent_old")
    refresh = AsyncMock(side_effect=[ConnectionError("offline"), unavailable, recovered])
    post = AsyncMock(return_value={"ok": True, "state": {"confirmed_playing": True}})
    monkeypatch.setattr(comandos, "ensure_music_worker_available", refresh)
    monkeypatch.setattr(comandos, "post_json_destino", post)

    result = await comandos.music_agent_command("play", guild_id=7, voice_channel_id=9,
                                                worker_validation=context, command_id="same-delivery")
    assert result["deferred"]
    post.assert_not_awaited()
    task = comandos._DEFERRED_TASKS[7]
    await asyncio.wait_for(asyncio.shield(task), timeout=1)
    assert refresh.await_count == 3
    post.assert_awaited_once()
    call = post.await_args.kwargs
    assert call["payload"]["command_id"] == "same-delivery"
    assert call["destino"].worker_id == "phone"
    assert call["destino"].base == recovered.worker["endpoint"]


@pytest.mark.asyncio
async def test_expiry_recovery_never_sends_to_new_worker_or_owner(admission, monkeypatch):
    clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    clock[0] += 1.0
    immediate_retries(monkeypatch)
    different = selection(endpoint="http://other:8766")
    different.worker_id = "other"
    refresh = AsyncMock(side_effect=[ConnectionError("offline"), different])
    post = AsyncMock()
    monkeypatch.setattr(comandos, "ensure_music_worker_available", refresh)
    monkeypatch.setattr(comandos, "post_json_destino", post)
    await comandos.music_agent_command("play", guild_id=7, worker_validation=context)
    await asyncio.wait_for(asyncio.shield(comandos._DEFERRED_TASKS[7]), timeout=1)
    post.assert_not_awaited()
    assert comandos._DEFERRED_STATE[7]["status"] == "failed"


@pytest.mark.asyncio
async def test_expired_context_accepts_authenticated_same_phone_endpoint_repair(admission, monkeypatch):
    clock, selection = admission
    original = selection()
    context = roteamento.capture_music_worker_validation(original, guild_id=7)
    roteamento.vincular_guild_worker(7, context.destination)
    clock[0] += 1
    refreshed = selection(endpoint="http://repaired:8766")
    monkeypatch.setattr(comandos, "ensure_music_worker_available", AsyncMock(return_value=refreshed))
    post = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(comandos, "post_json_destino", post)
    await comandos.music_agent_command("play", guild_id=7, worker_validation=context)
    assert post.await_args.kwargs["destino"].base == "http://repaired:8766"


@pytest.mark.asyncio
async def test_expired_context_defers_unavailable_owner_without_posting(admission, monkeypatch):
    clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    roteamento.vincular_guild_worker(7, context.destination)
    clock[0] += 1.0
    monkeypatch.setattr(comandos, "ensure_music_worker_available", AsyncMock(return_value=selection(available=False)))
    post = AsyncMock()
    monkeypatch.setattr(comandos, "post_json_destino", post)
    monkeypatch.setattr(comandos, "_DEFERRED_TASKS", {})
    monkeypatch.setattr(comandos, "_DEFERRED_STATE", {})
    try:
        result = await comandos.music_agent_command("play", guild_id=7, worker_validation=context)
        assert result["deferred"] and not result["queued"]
        post.assert_not_awaited()
    finally:
        await comandos.cancelar_comandos_diferidos()
    post.assert_not_awaited()
    assert comandos._DEFERRED_STATE[7]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_zero_ttl_refresh_is_used_once_without_creating_a_reusable_context(admission, monkeypatch):
    clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    clock[0] += 1.0
    fresh = selection(ttl=0)
    assert roteamento.capture_music_worker_validation(fresh, guild_id=7) is None
    refresh = AsyncMock(return_value=fresh)
    post = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(comandos, "ensure_music_worker_available", refresh)
    monkeypatch.setattr(comandos, "post_json_destino", post)
    await comandos.music_agent_command("prepare_voice", guild_id=7, worker_validation=context)
    refresh.assert_awaited_once()
    assert post.await_args.kwargs["destino"].base == "http://phone:8766"


def test_display_name_change_does_not_change_transport_identity(admission):
    _clock, selection = admission
    context = roteamento.capture_music_worker_validation(selection(), guild_id=7)
    destination = context.destination
    renamed = roteamento.DestinoWorker(destination.worker_id, "Renamed display", destination.base,
                                       destination.token, destination.transport)
    roteamento.vincular_guild_worker(7, renamed)
    assert roteamento.validated_command_destination(context, guild_id=7) == destination


def test_zero_ttl_unavailable_fake_and_wrong_owner_are_not_reusable(admission):
    _clock, selection = admission
    assert roteamento.capture_music_worker_validation(selection(ttl=0), guild_id=7) is None
    assert roteamento.capture_music_worker_validation(selection(available=False), guild_id=7) is None
    assert roteamento.capture_music_worker_validation(SimpleNamespace(available=True), guild_id=7) is None
    roteamento.vincular_guild_worker(7, roteamento.DestinoWorker("other", "Other", "http://other:8766", "token"))
    assert roteamento.capture_music_worker_validation(selection(), guild_id=7) is None


def test_cache_reuse_preserves_original_age_and_configuration(admission, monkeypatch):
    clock, selection = admission
    calls = []
    def fresh():
        calls.append(1)
        return selection()
    monkeypatch.setattr(selecao, "_select_music_worker_uncached", fresh)
    original = selecao.select_music_worker()
    clock[0] += 0.4
    assert selecao.select_music_worker() is original
    context = roteamento.capture_music_worker_validation(original, guild_id=7)
    assert context.valid_until == 100.8
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_MIN_VERSION", "99.0.0")
    assert selecao.select_music_worker() is not original
    assert len(calls) == 2


def test_metadata_selection_ignores_invalid_optional_direct_voice_endpoint(admission, monkeypatch):
    _clock, selection = admission
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_VOICE_EXECUTOR", "vps")
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_ENABLED", True)
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_BASE_URL", "http://8.8.8.8:8767")
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_TOKEN", "direct-token")
    expected = selection()
    monkeypatch.setattr(selecao, "_select_music_worker_uncached", lambda: expected)
    assert selecao.select_music_worker() is expected
    with pytest.raises(ValueError):
        roteamento.configured_direct_destination()


def test_direct_cache_preserves_age_and_invalidates_configuration(admission, monkeypatch):
    clock, _selection = admission
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_VOICE_EXECUTOR", "vps")
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_ENABLED", True)
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_BASE_URL", "http://127.0.0.1:8767")
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_DIRECT_API_TOKEN", "direct-token")
    requests = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({"ok": True, "discord_ready": True, "version": "0.3.85",
                               "executor_mode": "full", "voice_dependencies": {"ok": True}}).encode()
    def open_(request, **kwargs):
        requests.append(request)
        return Reply()
    monkeypatch.setattr(roteamento, "urlopen", open_)
    destination = roteamento.configured_direct_destination()
    original = roteamento._direct_health_selection(destination)
    clock[0] += 0.4
    assert roteamento._direct_health_selection(destination) is original
    assert original.validated_until == 100.8
    monkeypatch.setattr(roteamento.config, "MUSIC_AGENT_MIN_VERSION", "99.0.0")
    assert roteamento._direct_health_selection(destination) is not original
    assert len(requests) == 2
