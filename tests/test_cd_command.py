"""The CD panel routes through the real prefix gate and Discord V2 payload."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.http import handle_message_parameters
import pytest

import config
from cogs.tts.aliases import get_prefixed_aliases
from cogs.tts.cog import TTSVoice
from cogs.tts.mensagens.triagem import analisar_mensagem_para_tts
from cogs.tts.prefix import dispatch_prefix_control_command, match_prefix_control_command


class _DB:
    def __init__(self, prefix="_", **preferences):
        self.prefix = prefix
        self.preferences = preferences

    def get_guild_tts_defaults(self, guild_id):
        return {"bot_prefix": self.prefix, "enabled": False}

    def resolve_tts(self, guild_id, user_id):
        return dict(self.preferences)

    def get_user_tts(self, guild_id, user_id):
        return dict(self.preferences)


def _cog(db):
    cog = TTSVoice.__new__(TTSVoice)
    cog.bot = SimpleNamespace(settings_db=db)
    cog._prefix_panel_cooldowns = {}
    cog._active_prefix_panels = {}
    cog._public_panel_states = {}
    cog._ttv_voice_status = lambda *_args, **_kwargs: {"ready": False, "online": False}
    cog._tts_phone_worker_online_for_ui = lambda: False
    cog._ensure_connected = AsyncMock(side_effect=AssertionError("CD must not join voice"))
    cog._get_phone_worker_json = AsyncMock(side_effect=AssertionError("CD must not request a worker"))
    cog._enqueue_tts = AsyncMock(side_effect=AssertionError("CD must not queue speech"))
    return cog


def _message(content="_cd", *, guild=True):
    sent = SimpleNamespace(id=990, delete=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=sent))
    return SimpleNamespace(
        id=77, content=content, guild=SimpleNamespace(id=20) if guild else None,
        author=SimpleNamespace(id=10, bot=False, voice=None), channel=channel,
    )


@pytest.mark.parametrize("content,prefix", [("_cd", "_"), ("  _CD  ", "_"), ("!cd", "!"), ("os!cd", "os!")])
def test_cd_matches_only_the_configured_bot_prefix(content, prefix):
    command = match_prefix_control_command(content, prefix)
    assert command is not None
    assert command.kind == "panel_canto"
    assert command.argument == ""
    assert get_prefixed_aliases(prefix, "panel_canto") == (prefix + "cd",)


@pytest.mark.parametrize("content,prefix", [("_cdjunk", "_"), ("_cd outra", "_"), ("_cd", "!"), ("cd", "_"), ("'cd", "_")])
def test_cd_does_not_consume_unknown_arguments_or_other_commands(content, prefix):
    assert match_prefix_control_command(content, prefix) is None


def test_real_gate_dispatches_cd_offline_without_voice_and_sends_only_v2(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        cog = _cog(_DB("!", ttv_voice_id="hatsune-miku", ttv_language="en-US"))
        message = _message("!cd")
        decision = await analisar_mensagem_para_tts(cog, message)
        assert decision.should_dispatch_prefix_command
        assert not decision.should_process_tts
        assert decision.reason == "prefix_command"
        assert await dispatch_prefix_control_command(cog, message, decision.prefix_command)

        kwargs = message.channel.send.await_args.kwargs
        assert set(kwargs) == {"view", "allowed_mentions"}
        view = kwargs["view"]
        assert isinstance(view, discord.ui.LayoutView)
        assert view.message is message.channel.send.return_value
        assert view.owner_id == 10 and view.guild_id == 20
        with handle_message_parameters(**kwargs) as request:
            payload = request.payload
        assert discord.MessageFlags._from_value(payload["flags"]).components_v2
        assert payload["components"][0]["type"] == 17
        assert not payload.get("content") and not payload.get("embeds")
        assert payload["allowed_mentions"]["parse"] == []
        assert cog._active_prefix_panels[(20, 10, "cd")] is view.message
        assert cog._public_panel_states[990] == {"panel_kind": "cd", "owner_id": 10}
        cog._ensure_connected.assert_not_awaited()
        cog._get_phone_worker_json.assert_not_awaited()
        cog._enqueue_tts.assert_not_awaited()
        view.stop()

    asyncio.run(scenario())


def test_cd_reopen_replaces_only_same_users_cd_panel():
    async def scenario():
        cog = _cog(_DB())
        message = _message()
        old_cd = SimpleNamespace(id=400, delete=AsyncMock())
        other_cd = SimpleNamespace(id=401, delete=AsyncMock())
        tts_panel = SimpleNamespace(id=402, delete=AsyncMock())
        cog._active_prefix_panels.update({(20, 10, "cd"): old_cd, (20, 11, "cd"): other_cd, (20, 10, "launcher"): tts_panel})
        cog._public_panel_states[400] = {"panel_kind": "cd", "owner_id": 10}
        assert await cog._send_cd_panel(message)
        old_cd.delete.assert_awaited_once()
        other_cd.delete.assert_not_awaited()
        tts_panel.delete.assert_not_awaited()
        assert 400 not in cog._public_panel_states
        assert (20, 11, "cd") in cog._active_prefix_panels
        assert (20, 10, "launcher") in cog._active_prefix_panels
        message.channel.send.await_args.kwargs["view"].stop()

    asyncio.run(scenario())


def test_cd_expiry_uses_real_custom_prefix_without_inventing_slash_command():
    async def scenario():
        cog = _cog(_DB("!"))
        cog._get_root_command_ids_cached = AsyncMock(side_effect=AssertionError("No slash lookup for CD"))
        assert await cog._get_panel_prefix_hint(20, "cd") == "`!cd`"
        assert await cog._get_panel_command_mention(20, "cd") == "`!cd`"
        assert "`!cd` novamente" in await cog._build_expired_panel_message(20, "cd")
        cog._get_root_command_ids_cached.assert_not_awaited()

    asyncio.run(scenario())


def test_cd_handles_missing_database_and_dm_without_synthesis():
    async def scenario():
        cog = _cog(None)
        message = _message()
        assert await cog._send_cd_panel(message)
        assert message.channel.send.await_args.kwargs["embed"].title == "Configuração indisponível"
        assert not cog._active_prefix_panels
        dm = _message(guild=False)
        assert not await cog._send_cd_panel(dm)
        dm.channel.send.assert_not_awaited()
        cog._ensure_connected.assert_not_awaited()

    asyncio.run(scenario())
