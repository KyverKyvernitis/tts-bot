"""Regression gates for command dispatch, Mongo saved melody and live queued score."""
from __future__ import annotations

from unittest.mock import AsyncMock
import pytest

from cogs.tts.aliases import get_prefixed_aliases
from cogs.tts.prefix import match_prefix_control_command, dispatch_prefix_control_command
from cogs.tts.melodies import DEFAULT_MELODY_ID, list_melodies, score_for_melody
from cogs.tts.mensagens.preparacao import preparar_payload_tts_mensagem
from test_ttv_preferences import settings as settings_fixture
from test_tts_message_flow import FakeCog, FakeDB, make_message


@pytest.mark.parametrize("prefix", ("_", "!", "os!"))
def test_cd_honors_prefix_and_rejects_arguments(prefix):
    assert get_prefixed_aliases(prefix, "cd") == (prefix + "cd",)
    assert match_prefix_control_command(prefix + "cd", prefix).kind == "cd"
    assert match_prefix_control_command(prefix + "cd now", prefix) is None


@pytest.mark.asyncio
async def test_cd_dispatch_calls_handler_only_once():
    from types import SimpleNamespace
    cog = SimpleNamespace(_send_cd_panel=AsyncMock(return_value=True))
    command = match_prefix_control_command("!cd", "!")
    assert await dispatch_prefix_control_command(cog, object(), command)
    cog._send_cd_panel.assert_awaited_once()


@pytest.mark.asyncio
async def test_saved_melody_persists_without_changing_other_fields(settings_fixture):
    db = settings_fixture
    selected = list_melodies()[1].id
    await db.set_user_tts(1, 2, voice="EdgeVoice", ttv_melody_id=selected)
    assert db.get_user_tts(1, 2)["ttv_melody_id"] == selected
    assert db.resolve_tts(1, 2)["ttv_melody_id"] == selected
    assert db.resolve_tts(1, 2)["voice"] == "EdgeVoice"
    with pytest.raises(ValueError):
        await db.set_user_tts(1, 2, ttv_melody_id="../bad", voice="corrupted")
    assert db.get_user_tts(1, 2)["voice"] == "EdgeVoice"


@pytest.mark.asyncio
async def test_message_singing_applies_selected_score_but_plain_speech_unchanged():
    selected = list_melodies()[1].id
    cog = FakeCog(db=FakeDB(resolved={"ttv_melody_id": selected}))
    regular = await preparar_payload_tts_mensagem(cog, make_message("'fala normal"),
       guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert regular.queue_item.ttv_score is None
    sung = await preparar_payload_tts_mensagem(cog, make_message("'/cantar la la la la"),
       guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert sung.queue_item.ttv_score == score_for_melody(selected)
    assert sung.queue_item.ttv_pitch_semitones == "+0.0"
    assert sung.queue_item.ttv_speech_rate == 1.0
