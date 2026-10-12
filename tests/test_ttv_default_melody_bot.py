from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from test_tts_helpers import tts_audio as audio
from cogs.tts.performance import singing_request
from cogs.tts.mensagens.preparacao import preparar_payload_tts_mensagem
from cogs.tts.failure_notices import failure_code, _public_notice
from test_ttv_live_midi_regression import LiveRenderCog, FakeDB, make_message, DEMO_MIDI
from test_ttv_audio import Probe, item


@pytest.mark.parametrize("text", ["a ~ ~ ~ a", "/cantar Meu pai tem pão", "/CANTAR Olá"])
def test_simple_opt_in_requests_default_melody(text):
    clean, wants = singing_request(text)
    assert wants and not clean.lower().startswith("/cantar")


@pytest.mark.parametrize("text", ["aaa~", r"a \~ a", 'a "~" a', "a `~` a", "cantar no chuveiro", "https://host/~"])
def test_plain_speech_or_literal_marks_never_enable_music(text):
    assert singing_request(text) == (text, False)


def test_directive_preserves_remaining_escapes():
    assert singing_request(r"/cantar a \~ a") == (r"a \~ a", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", ["kasane-teto", "hatsune-miku"])
@pytest.mark.parametrize("text", ["'a ~ ~ ~ a", "'/cantar Meu pai tem pão"])
async def test_actual_text_renderer_prepares_singing_without_downloads(voice, text):
    cog = LiveRenderCog(db=FakeDB(resolved={"ttv_voice_id": voice, "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.25}))
    msg = make_message(text)
    msg.reply = AsyncMock()
    payload = await preparar_payload_tts_mensagem(cog, msg, guild_defaults={"announce_author": True},
        active_prefix="'", forced_engine="teto")
    assert payload.queue_item.ttv_score == {"version": 1, "preset": "ttv-demo-v1"}
    assert "Dilma" not in payload.text and "/cantar" not in payload.text
    assert payload.queue_item.ttv_voice_id == voice
    assert payload.queue_item.ttv_pitch_semitones == "+0.0" and payload.queue_item.ttv_speech_rate == 1.0
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_attached_midi_keeps_priority_over_default_command():
    cog = LiveRenderCog(db=FakeDB())
    msg = make_message("'/cantar a ~ ~ ~ a")
    msg.attachments = [SimpleNamespace(filename="melodia.mid", size=len(DEMO_MIDI), read=AsyncMock(return_value=DEMO_MIDI))]
    payload = await preparar_payload_tts_mensagem(cog, msg, guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert "preset" not in payload.queue_item.ttv_score
    assert payload.queue_item.ttv_score["duration_ms"] == 5500


def music_item():
    return item(ttv_score={"version": 1, "preset": "ttv-demo-v1"})


def test_performance_budget_does_not_inherit_twenty_five_second_speech_limit(monkeypatch):
    monkeypatch.setattr(audio, "TTS_TETO_WORKER_TIMEOUT_SECONDS", 25)
    probe = Probe()
    assert probe._tts_agent_payload_for_item(item())["timeout_seconds"] == 25
    assert probe._tts_agent_payload_for_item(music_item())["timeout_seconds"] == 120
    assert probe._estimate_playback_timeout(music_item()) >= 70


@pytest.mark.asyncio
async def test_previous_worker_is_refused_before_synthesis_with_specific_notice():
    probe = Probe()
    probe._tts_agent_route_available = lambda: True
    probe._tts_agent_route_state = lambda: {"ttv_performance_protocol": 1}
    with pytest.raises(audio.TTVSingingError) as error:
        await probe._generate_tts_agent_worker_file(music_item())
    assert failure_code("synthesis_failed", error.value) == "ttv_worker_outdated"
    assert "1.11.41" in _public_notice(error.value.code)


def test_singing_notices_do_not_expose_private_provider_errors():
    private = "SECRET_TOKEN=https://private.invalid/path private lyric"
    error = audio.TTVSingingError(private)
    assert failure_code("synthesis_failed", error) == "ttv_singing_failed"
    assert private not in _public_notice(error.code)


@pytest.mark.asyncio
async def test_selected_voice_protocol_is_checked_even_when_aggregate_is_new():
    probe = Probe()
    probe.route["ttv_performance_protocol"] = 2
    probe.route["ttv_voices"][0]["ttv_performance_protocol"] = 1
    with pytest.raises(audio.TTVSingingError) as error:
        await probe._generate_tts_agent_worker_file(music_item())
    assert error.value.code == "ttv_worker_outdated"


def test_singing_cache_tracks_the_actual_english_bank_separately_from_speech():
    probe = Probe()
    route = {"ttv_personal_settings": True, "ttv_voices": [{"id": "kasane-teto", "ready": True,
        "available": True, "languages": ["pt-BR"], "fingerprint": "same-standard-bank",
        "singing_fingerprints": {"pt-BR": "english-bank-one"}}]}
    probe._tts_agent_route_state = lambda: route
    first = probe._cache_key(music_item())
    spoken = probe._cache_key(item())
    route["ttv_voices"][0]["singing_fingerprints"]["pt-BR"] = "english-bank-two"
    assert first != probe._cache_key(music_item())
    assert spoken == probe._cache_key(item())
