"""Keep phone TTS on the same verified voice session during the canary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.musica.integracoes import tts as integration
from test_tts_helpers import tts_audio

TOKEN = "a" * 32


@pytest.fixture
def canary(monkeypatch):
    monkeypatch.setenv("TTS_FIRST_AUDIO_GUILDS", "77")
    monkeypatch.setenv("TTS_NATIVE_EXISTING_VOICE_ENABLED", "1")
    monkeypatch.delenv("WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED", raising=False)
    for flag in ("WORKER_VOICE_AGENT_ENABLED", "WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED",
                 "WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED"):
        monkeypatch.setattr(tts_audio, flag, True)

    class VoiceProbe(tts_audio.TTSAudioMixin):
        def _tts_agent_route_available(self):
            return self.route_available

        def _tts_agent_route_state(self):
            return {"worker_id": self.worker_id}

    probe = VoiceProbe()
    probe.bot = SimpleNamespace()
    probe.route_available = True
    probe.worker_id = "phone-1"
    probe.voice_client = None
    probe._tts_native_existing_voice = (tts_audio.time.monotonic(), "phone-1", {(77, 88): TOKEN})
    probe._get_voice_client_for_guild = lambda _: probe.voice_client
    probe._voice_client_is_connected = lambda client: client.connected
    probe._worker_voice_direct_tts_payload = lambda _, item: {"tts_request_id": item.request_id,
                                                              "timeout_seconds": 10}
    probe._maybe_attach_prebuilt_direct_tts_audio = AsyncMock()
    probe._request_worker_voice_agent_json = AsyncMock()

    async def cancel(item):
        assert item._tts_remote_active is True
        item._tts_remote_active = False

    probe._cancel_remote_tts_request = AsyncMock(side_effect=cancel)
    probe._worker_voice_agent_release_transfer = AsyncMock()
    probe._record_worker_voice_session_metric = lambda _: None
    probe._get_metrics_store = dict
    item = SimpleNamespace(guild_id=77, channel_id=88, engine="gtts", text="Olá, tudo bem?",
                           language="pt-br", request_id="request-1")
    return probe, SimpleNamespace(id=77), item


def receipt(ok, observed, *, request="request-1"):
    return {"ok": ok, "worker_result": {"ok": ok, "first_frame_observed": observed,
                                      "tts_request_id": request, "playback_ms": 25}}


@pytest.mark.parametrize("case", ["cold", "stale", "wrong_channel", "other_worker", "active_vps", "effect", "explicit_off"])
def test_gtts_never_moves_or_opens_phone_voice_in_ineligible_cases(canary, monkeypatch, case):
    probe, guild, item = canary
    observed, worker, sessions = probe._tts_native_existing_voice
    if case == "cold":
        probe._tts_native_existing_voice = (observed, worker, {})
    elif case == "stale":
        probe._tts_native_existing_voice = (observed - 60, worker, sessions)
    elif case == "other_worker":
        probe.worker_id = "phone-2"
    elif case == "effect":
        item.advanced_reverb_level = 1
    elif case == "wrong_channel":
        item.channel_id = 99
    elif case == "active_vps":
        probe.voice_client = SimpleNamespace(connected=True)
    elif case == "explicit_off":
        monkeypatch.setenv("WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED", "false")
        monkeypatch.setattr(tts_audio, "WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED", False)
    assert not probe._worker_voice_direct_tts_available_for(guild, item)[0]
    assert not probe._tts_agent_should_try_worker(item)[0]
    assert not probe._edge_phone_available(item, streaming=True)


@pytest.mark.asyncio
async def test_matching_session_only_posts_one_play_and_passes_canary_flags(canary, monkeypatch):
    probe, guild, item = canary
    monkeypatch.setenv("TTS_FIRST_AUDIO_SEGMENTS", "1")
    monkeypatch.setenv("TTS_FIRST_AUDIO_ASYNC_CACHE", "1")
    probe._request_worker_voice_agent_json.return_value = receipt(True, True)
    result = await probe._try_worker_voice_direct_tts(guild, item)
    assert result["ok"] is True
    assert item._tts_remote_active is False
    probe._request_worker_voice_agent_json.assert_awaited_once()
    request = probe._request_worker_voice_agent_json.await_args.kwargs
    assert request["task"] == "voice_agent_play_tts_existing"
    assert request["payload"] == {
        "tts_request_id": item.request_id, "timeout_seconds": 10, "auto_leave_enabled": True,
        "require_existing_voice": True, "expected_voice_session": TOKEN,
        "confirm_transfer": False, "startup_segments": True, "startup_async_cache": True,
    }
    probe._maybe_attach_prebuilt_direct_tts_audio.assert_awaited_once()
    assert not probe._maybe_attach_prebuilt_direct_tts_audio.await_args.kwargs["generate_if_missing"]
    probe._cancel_remote_tts_request.assert_not_awaited()
    probe._worker_voice_agent_release_transfer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "code", "cancel"), [
    (receipt(False, False), None, False),
    (receipt(False, True), "stream_incomplete", False),
    (receipt(False, False, request="other"), "remote_playback_unconfirmed", True),
    (receipt(True, False), "remote_playback_unconfirmed", True),
    ({}, "remote_playback_unconfirmed", True),
    (TimeoutError(), "remote_playback_unconfirmed", True),
])
async def test_only_matched_before_frame_receipt_can_fallback(canary, reply, code, cancel):
    probe, guild, item = canary
    if isinstance(reply, Exception):
        probe._request_worker_voice_agent_json.side_effect = reply
    else:
        probe._request_worker_voice_agent_json.return_value = reply
    if code is None:
        assert await probe._try_worker_voice_direct_tts(guild, item) is None
    else:
        with pytest.raises(tts_audio.TTSPlaybackError) as exc:
            await probe._try_worker_voice_direct_tts(guild, item)
        assert exc.value.code == code
    assert probe._cancel_remote_tts_request.await_count == int(cancel)
    probe._worker_voice_agent_release_transfer.assert_not_awaited()
    assert item._tts_remote_active is False


@pytest.mark.asyncio
async def test_routing_hint_rechecked_after_cache_io(canary):
    probe, guild, item = canary

    async def new_voice(*_, **_kw):
        probe.voice_client = SimpleNamespace(connected=True)

    probe._maybe_attach_prebuilt_direct_tts_audio.side_effect = new_voice
    assert await probe._try_worker_voice_direct_tts(guild, item) is None
    probe._request_worker_voice_agent_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_caller_cancels_remote_without_releasing_voice(canary):
    probe, guild, item = canary
    probe._request_worker_voice_agent_json.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await probe._try_worker_voice_direct_tts(guild, item)
    probe._cancel_remote_tts_request.assert_awaited_once_with(item)
    probe._worker_voice_agent_release_transfer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "cache_hit", "legacy", "stale", "partial", "uncertain"])
async def test_overlay_avoids_full_prebuild_only_for_proven_compatible_cache_miss(canary, monkeypatch, mode):
    probe, guild, item = canary
    monkeypatch.setattr(integration, "deve_rotear_tts_para_agente", lambda *_: True)
    monkeypatch.setattr(integration, "suporta_cache_tts_agente", lambda *_: True)
    legacy_play = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(integration, "tocar_tts_via_agente", legacy_play)
    probe._cache_key = lambda _: "key"
    probe._estimate_playback_timeout = lambda _: 3
    probe._record_queue_timing = lambda **_: None
    probe._record_worker_first_frame_timing = lambda *_args, **_kwargs: None
    probe._schedule_worker_voice_agent_register_session = lambda *_args, **_kwargs: None
    if mode == "legacy":
        item.advanced_reverb_level = 1
    if mode == "stale":
        probe._tts_native_existing_voice = (0, "", {})
    if mode == "cache_hit":
        async def attach(payload, *_args, **_kwargs):
            payload["audio_b64"] = "audio"
        probe._maybe_attach_prebuilt_direct_tts_audio.side_effect = attach
    probe._request_worker_voice_agent_json.return_value = {} if mode == "uncertain" else receipt(mode != "partial", True)
    if mode in {"uncertain", "partial"}:
        code = "remote_playback_unconfirmed" if mode == "uncertain" else "stream_incomplete"
        with pytest.raises(tts_audio.TTSPlaybackError, match=code):
            await integration.rotear_item_tts_para_musica(probe, guild, item, None)
    else:
        assert await integration.rotear_item_tts_para_musica(probe, guild, item, None) == (mode != "stale", None)
    cache_reader = probe._maybe_attach_prebuilt_direct_tts_audio
    if mode == "stale":
        cache_reader.assert_not_awaited()
    else:
        assert cache_reader.await_args.kwargs["generate_if_missing"] is (mode == "legacy")
    assert legacy_play.await_count == int(mode == "legacy")
    assert probe._request_worker_voice_agent_json.await_count == int(mode not in {"legacy", "stale"})
    if mode == "cache_hit":
        request = probe._request_worker_voice_agent_json.await_args.kwargs
        assert request["task"] == "voice_agent_play_tts_existing"
        assert request["payload"]["audio_b64"] == "audio"
        assert request["payload"]["expected_voice_session"] == TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [True, False])
async def test_cancel_uses_dedicated_task_only_for_strict_playback(canary, strict):
    probe, _, item = canary
    requester = probe._request_phone_worker_json = AsyncMock()
    item._tts_remote_active = True
    item._tts_remote_existing_voice = strict
    await integration.cancelar_tts_remoto(probe, item)
    request = requester.await_args.kwargs
    assert request["task"] == ("voice_agent_cancel_tts_existing" if strict else "music_agent_command")
    assert request["payload"].get("require_existing_voice", False) is strict
    assert item._tts_remote_active is False
    assert item._tts_remote_existing_voice is False
