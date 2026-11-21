"""Strict TTS binds to an already-live Discord session and a compact receipt."""
from types import SimpleNamespace

import pytest

from cogs.musica.runtime_telefone.agente.servidor import MusicAgent


@pytest.fixture
def existing_voice(monkeypatch):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", "/tmp/test-voice.sock")
    player = SimpleNamespace(channel=SimpleNamespace(id=120), is_connected=lambda: True,
                             _connection=SimpleNamespace(session_id="discord-proof-not-for-export"))
    guild = SimpleNamespace(voice_client=player, me=SimpleNamespace(voice=SimpleNamespace(channel=player.channel)))
    agent = object.__new__(MusicAgent)
    agent.executor_mode = "voice"
    agent.client = SimpleNamespace(is_ready=lambda: True, get_guild=lambda gid: guild if gid == 12 else None)
    agent.states = {12: SimpleNamespace(player=player)}
    agent._tts_voice_sessions = {}
    return agent, player, guild


def test_existing_voice_tokens_match_only_the_live_voice_client_and_channel(existing_voice):
    agent, player, guild = existing_voice
    record = agent._tts_existing_voice_status()["native_tts_existing_sessions"][0]
    assert set(record) == {"guild_id", "channel_id", "session"}
    assert record["session"] != player._connection.session_id
    body = {"guild_id": 12, "channel_id": 120, "expected_voice_session": record["session"]}
    assert agent._tts_existing_voice_client(body) is player
    assert agent._tts_existing_voice_client(dict(body, channel_id=121)) is None
    guild.voice_client = object()
    assert agent._tts_existing_voice_client(body) is None
    assert agent._tts_existing_voice_status()["native_tts_existing_sessions"] == []


def test_reconnect_rotates_token_and_disconnect_invalidates_it(existing_voice):
    agent, player, _ = existing_voice
    original = agent._tts_existing_voice_status()["native_tts_existing_sessions"][0]["session"]
    body = {"guild_id": 12, "channel_id": 120, "expected_voice_session": original}
    player._connection.session_id = "different-discord-session"
    assert agent._tts_existing_voice_client(body) is None
    fresh = agent._tts_existing_voice_status()["native_tts_existing_sessions"][0]["session"]
    assert fresh != original
    assert agent._tts_existing_voice_client(dict(body, expected_voice_session=fresh)) is player
    player.is_connected = lambda: False
    assert agent._tts_existing_voice_client(dict(body, expected_voice_session=fresh)) is None


@pytest.mark.asyncio
async def test_only_dedicated_authenticated_endpoint_accepts_strict_tts(existing_voice):
    import json

    agent, _, _ = existing_voice
    agent._auth_ok = lambda _: True
    calls = []

    async def read_body():
        return {"action": "voice_tts", "guild_id": 12, "channel_id": 120,
                "require_existing_voice": True, "tts_request_id": "measure-1"}

    async def dispatch(body):
        calls.append(body["command_id"])
        return {"ok": False, "partial_playback": True, "first_frame_observed": True,
                "first_frame_ms": 80.0, "state": {"queue": ["not in receipt"]}}

    agent.dispatch = dispatch
    request = SimpleNamespace(json=read_body)
    assert (await agent.handle_command(request)).status == 400 and not calls
    first = await agent.handle_tts_existing(request)
    second = await agent.handle_tts_existing(request)
    assert first.status == second.status == 200 and calls[0] == calls[1]
    result = json.loads(first.body)
    assert result["tts_request_id"] == "measure-1" and result["partial_playback"] is True
    assert result["first_frame_observed"] is True and "state" not in result
    agent._auth_ok = lambda _: False
    assert (await agent.handle_tts_existing(request)).status == 401 and len(calls) == 2
