"""Regressões do handoff de voz Piper e do timeout de prime WAV.

O Music Agent nativo não sintetiza Piper. A VPS deve preservar a call
quando o Phone Worker produz o WAV, mesmo com TTS direto ativado.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from test_tts_helpers import QueueItem, TTSAudioMixin, tts_audio


class Probe(TTSAudioMixin):
    def __init__(self):
        self.bot = SimpleNamespace(audio_router=None)
        self.guild_states = {}
        self.metric_store = {}
        self.records = []

    def _record_worker_voice_session_metric(self, key):
        self.records.append(key)

    def _get_metrics_store(self):
        return self.metric_store


def item(engine="piper"):
    return QueueItem(
        guild_id=77, channel_id=88, author_id=33,
        text="Teste de voz", engine=engine, voice="", language="pt-br",
        rate="+0%", pitch="+0Hz",
    )


def test_piper_direct_unsupported_before_ownership_checks():
    probe = Probe()
    guild = SimpleNamespace(id=77)
    with patch.multiple(
        tts_audio,
        WORKER_VOICE_AGENT_ENABLED=True,
        WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED=True,
        WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED=True,
    ):
        assert probe._worker_voice_direct_tts_available_for(guild, item()) == (
            False, "engine_requires_local_synthesis"
        )
        assert probe._worker_voice_direct_tts_available_for(guild, item("not_yet_supported")) == (
            False, "engine_requires_local_synthesis"
        )


@pytest.mark.asyncio
async def test_piper_never_disconnects_even_if_availability_is_misreported():
    probe = Probe()
    probe._worker_voice_direct_tts_available_for = lambda *_: (True, "allowed")
    probe._worker_voice_agent_begin_transfer = AsyncMock(side_effect=AssertionError("handoff called"))
    probe._disconnect_vps_voice_before_worker_direct_tts = AsyncMock(side_effect=AssertionError("disconnect called"))
    probe._request_worker_voice_agent_json = AsyncMock(side_effect=AssertionError("music agent called"))

    assert await probe._try_worker_voice_direct_tts(SimpleNamespace(id=77), item()) is None
    probe._worker_voice_agent_begin_transfer.assert_not_awaited()
    probe._disconnect_vps_voice_before_worker_direct_tts.assert_not_awaited()
    probe._request_worker_voice_agent_json.assert_not_awaited()
    assert probe.metric_store["worker_voice_direct_last_skip_reason"] == "piper_uses_vps_playback"
    assert probe.records == ["direct_tts_skipped"]


@pytest.mark.asyncio
async def test_piper_uses_longer_ffmpeg_prime_budget_without_affecting_edge():
    probe = Probe()
    probe._edge_stream_handle_for_path = lambda *_: None
    probe._is_music_active_for_guild = lambda *_: False
    probe._record_latency_sample = lambda *_: None
    timeouts = []

    class Source:
        def read(self):
            return b"\x00" * 3840

        def cleanup(self):
            pass

    probe._make_discord_tts_source_for_item = lambda *_: (Source(), "ffmpeg_pcm")
    original_wait_for = asyncio.wait_for

    async def recorded_wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    for engine in ("piper", "edge"):
        audio_ready = asyncio.get_running_loop().create_future()
        audio_ready.set_result(("/unused/test.wav", False))
        with patch.object(tts_audio.asyncio, "wait_for", recorded_wait_for), patch.object(
            tts_audio, "TTS_FFMPEG_PRIME_ENABLED", True
        ):
            _, _, prepared = await probe._resolve_and_prime_audio(audio_ready, item(engine))
        assert prepared is not None
        prepared.cleanup()

    assert timeouts == [
        tts_audio.TTS_PIPER_FFMPEG_PRIME_TIMEOUT_SECONDS,
        tts_audio.TTS_FFMPEG_PRIME_TIMEOUT_SECONDS,
    ]
    assert timeouts[0] > timeouts[1]
