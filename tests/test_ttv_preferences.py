from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def settings(monkeypatch):
    # Load the real SettingsDB methods without a Mongo driver or connection.
    motor = types.ModuleType("motor")
    motor_asyncio = types.ModuleType("motor.motor_asyncio")
    motor_asyncio.AsyncIOMotorClient = object
    motor.motor_asyncio = motor_asyncio
    monkeypatch.setitem(sys.modules, "motor", motor)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", motor_asyncio)
    path = Path(__file__).resolve().parents[1] / "db.py"
    spec = importlib.util.spec_from_file_location("ttv_settings_db_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = module.SettingsDB.__new__(module.SettingsDB)
    db.user_cache = {}
    db.guild_cache = {}
    db._resolved_tts_cache = {}
    db.coll = types.SimpleNamespace(update_one=AsyncMock(), delete_one=AsyncMock())
    return db


def test_legacy_minus_two_is_migrated_by_read_only_and_other_engines_stay_isolated(settings):
    settings.user_cache[(1, 2)] = {"tts": {
        "teto_pitch_semitones": "-2.0", "engine": "edge", "voice": "EdgeVoice",
        "rate": "+25%", "pitch": "+35Hz", "language": "en",
    }}
    before = deepcopy(settings.user_cache)
    resolved = settings.resolve_tts(1, 2)
    assert resolved["ttv_voice_id"] == "kasane-teto"
    assert resolved["ttv_pitch_semitones"] == resolved["teto_pitch_semitones"] == "-2.0"
    assert resolved["ttv_speech_rate"] == 1.0
    assert [resolved[key] for key in ("engine", "voice", "rate", "pitch", "language")] == ["edge", "EdgeVoice", "+25%", "+35Hz", "en"]
    assert settings.user_cache == before
    settings.coll.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_writes_new_keys_and_legacy_pitch_only_for_the_target_user(settings):
    settings.user_cache[(1, 2)] = {"tts": {"voice": "EdgeVoice", "rate": "+25%", "pitch": "+35Hz", "teto_pitch_semitones": "-2.0"}}
    settings.user_cache[(1, 3)] = {"tts": {"teto_pitch_semitones": "-3.0"}}
    old_other = deepcopy(settings.user_cache[(1, 3)])
    assert settings.resolve_tts(1, 2)["ttv_pitch_semitones"] == "-2.0"
    await settings.set_user_tts(1, 2, ttv_voice_id="kasane-teto", ttv_pitch_semitones="-1,5", ttv_speech_rate=1.07)
    raw = settings.user_cache[(1, 2)]["tts"]
    assert raw["ttv_voice_id"] == "kasane-teto"
    assert raw["ttv_pitch_semitones"] == raw["teto_pitch_semitones"] == "-1.5"
    assert raw["ttv_speech_rate"] == 1.07
    assert [raw[key] for key in ("voice", "rate", "pitch")] == ["EdgeVoice", "+25%", "+35Hz"]
    assert settings.user_cache[(1, 3)] == old_other
    assert settings.resolve_tts(1, 2)["ttv_speech_rate"] == 1.07
    call = settings.coll.update_one.await_args
    assert call.args[0] == {"type": "user", "guild_id": 1, "user_id": 2}
    assert call.args[1]["$set"]["tts"] == raw


@pytest.mark.asyncio
async def test_legacy_pitch_edit_updates_new_alias_without_leaving_stale_preference(settings):
    await settings.set_user_tts(1, 2, ttv_pitch_semitones="-2")
    await settings.set_user_tts(1, 2, teto_pitch_semitones="+1.5")
    resolved = settings.resolve_tts(1, 2)
    assert resolved["ttv_pitch_semitones"] == resolved["teto_pitch_semitones"] == "+1.5"


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"ttv_voice_id": "missing-character"}, {"ttv_pitch_semitones": "1.25"}, {"ttv_speech_rate": float("nan")}])
async def test_invalid_complete_edit_cannot_partially_mutate_edge_or_ttv(settings, updates):
    settings.user_cache[(1, 2)] = {"tts": {"teto_pitch_semitones": "-2.0", "voice": "OldEdgeVoice"}}
    before = deepcopy(settings.user_cache)
    with pytest.raises(ValueError):
        await settings.set_user_tts(1, 2, voice="NewEdgeVoice", **updates)
    assert settings.user_cache == before
    settings.coll.update_one.assert_not_awaited()


def test_unknown_saved_character_is_preserved_and_edge_keeps_working(settings):
    settings.user_cache[(1, 2)] = {"tts": {"ttv_voice_id": "uninstalled-character", "engine": "edge"}}
    resolved = settings.resolve_tts(1, 2)
    assert resolved["ttv_voice_id"] == "uninstalled-character"
    assert resolved["engine"] == "edge"


def test_numeric_zero_is_preserved_even_with_nonzero_config_default(settings, monkeypatch):
    import config
    monkeypatch.setattr(config, "TTS_TETO_DEFAULT_PITCH_SEMITONES", -2.0)
    settings.user_cache[(1, 2)] = {"tts": {"ttv_pitch_semitones": 0, "teto_pitch_semitones": 0}}
    assert settings.get_user_tts(1, 2)["ttv_pitch_semitones"] == "0"
    assert settings.resolve_tts(1, 2)["ttv_pitch_semitones"] == "+0.0"


@pytest.mark.asyncio
async def test_restore_ttv_controls_keeps_voice_and_edge_choices(settings):
    await settings.set_user_tts(1, 2, voice="EdgeVoice", rate="+25%", pitch="+35Hz", ttv_voice_id="kasane-teto", ttv_pitch_semitones="-2", ttv_speech_rate=1.15)
    await settings.set_user_tts(1, 2, ttv_pitch_semitones="0", ttv_speech_rate=1.0)
    resolved = settings.resolve_tts(1, 2)
    assert [resolved[key] for key in ("ttv_voice_id", "ttv_pitch_semitones", "ttv_speech_rate")] == ["kasane-teto", "+0.0", 1.0]
    assert [resolved[key] for key in ("voice", "rate", "pitch")] == ["EdgeVoice", "+25%", "+35Hz"]


@pytest.mark.asyncio
async def test_message_preparation_transports_personal_ttv_fields_without_edge_controls():
    from test_tts_message_flow import FakeCog, FakeDB, make_message
    from cogs.tts.mensagens.preparacao import preparar_payload_tts_mensagem
    cog = FakeCog(db=FakeDB(resolved={
        "engine": "edge", "voice": "EdgeVoice", "rate": "+25%", "pitch": "+35Hz",
        "ttv_voice_id": "kasane-teto", "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.07,
    }))
    payload = await preparar_payload_tts_mensagem(cog, make_message("'olá"), guild_defaults={}, active_prefix="'", forced_engine="teto")
    item = payload.queue_item
    assert [item.ttv_voice_id, item.ttv_pitch_semitones, item.ttv_speech_rate] == ["kasane-teto", "-2.0", 1.07]
    assert item.teto_pitch_semitones == "-2.0"
    assert [item.piper_fallback_voice, item.piper_fallback_rate, item.piper_fallback_pitch] == ["EdgeVoice", "+25%", "+35Hz"]


@pytest.mark.asyncio
async def test_unknown_ttv_selection_rejects_character_route_but_not_edge_message():
    from test_tts_message_flow import FakeCog, FakeDB, make_message
    from cogs.tts.mensagens.preparacao import preparar_payload_tts_mensagem
    cog = FakeCog(db=FakeDB(resolved={"engine": "edge", "voice": "EdgeVoice", "ttv_voice_id": "missing-character"}))
    teto = await preparar_payload_tts_mensagem(cog, make_message("'olá"), guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert teto is None
    edge = await preparar_payload_tts_mensagem(cog, make_message(",olá"), guild_defaults={}, active_prefix=",", forced_engine="edge")
    assert edge.queue_item.engine == "edge"
    assert edge.queue_item.voice == "EdgeVoice"
