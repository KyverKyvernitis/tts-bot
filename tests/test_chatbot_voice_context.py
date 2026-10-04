"""Presença do Gateway distingue autor/bot, Stage e contexto desconhecido."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from cogs.chatbot.voice_context import build_voice_snapshot, member_voice_snapshot
from tests.test_chatbot_action_flow import world


def voice(w, identifier, name="call", *, stage=False, visible=True):
    channel = MagicMock(spec=discord.StageChannel if stage else discord.VoiceChannel)
    channel.id, channel.guild, channel.name = identifier, w.guild, name
    channel.permissions_for.return_value = SimpleNamespace(view_channel=visible)
    return channel


def test_voice_snapshot_uses_gateway_members_without_rest_or_voice_connection(world):
    world.voice.name = "call real"
    world.guild.voice_client = None
    result = build_voice_snapshot(world.cog.bot, world.guild, world.members[1])
    assert result["author"] == {"known": True, "connected": True, "channel_id": "20",
        "channel_name": "call real", "channel_mention": "<#20>", "channel_type": "voice", "supported": True}
    assert result["bot"] == result["author"] and result["same_channel"]
    assert result["can_listen"] is False
    world.guild.fetch_member.assert_not_awaited()


def test_stage_presence_is_connected_but_does_not_claim_supported_voice_actions(world):
    stage = voice(world, 44, "palco", stage=True)
    world.members[1].voice = SimpleNamespace(channel=stage)
    state = build_voice_snapshot(world.cog.bot, world.guild, world.members[1])
    assert state["author"]["connected"] and state["author"]["channel_type"] == "stage"
    assert state["author"]["channel_id"] == "44" and not state["author"]["supported"]
    assert not state["same_channel"]


@pytest.mark.asyncio
async def test_action_catalog_agrees_with_stage_presence(world):
    from cogs.chatbot.action_policy import build_action_context

    world.members[1].voice = SimpleNamespace(channel=voice(world, 44, "palco", stage=True))
    context = await build_action_context(world.cog.bot, world.message, world.config)
    author_line = next(line for line in context.description.splitlines() if line.startswith("Alvo autor:"))
    assert "canal de palco" in author_line and "fora de call" not in author_line
    assert "não estão disponíveis" in author_line


def test_hidden_bot_channel_never_exposes_name_id_mention_or_type(world):
    hidden = voice(world, 44, "secreto", visible=False)
    world.members[999].voice = SimpleNamespace(channel=hidden)
    state = build_voice_snapshot(world.cog.bot, world.guild, world.members[1])
    assert state["bot"]["connected"]
    assert all(state["bot"][key] is None for key in ("channel_id", "channel_name", "channel_mention", "channel_type"))
    assert not state["same_channel"]


@pytest.mark.parametrize("unavailable,intent", [(True, True), (False, False)])
def test_missing_gateway_state_is_unknown_instead_of_disconnected(world, unavailable, intent):
    world.guild.unavailable = unavailable
    world.cog.bot.intents = SimpleNamespace(voice_states=intent)
    state = build_voice_snapshot(world.cog.bot, world.guild, world.members[1])
    assert not state["author"]["known"] and state["author"]["connected"] is None
    assert not state["bot"]["known"] and state["bot"]["channel_id"] is None


def test_gateway_move_and_disconnect_replace_previous_snapshot(world):
    original = build_voice_snapshot(world.cog.bot, world.guild, world.members[1])
    changed = voice(world, 55, "nova")
    world.members[1].voice = SimpleNamespace(channel=changed)
    later = build_voice_snapshot(world.cog.bot, world.guild, world.members[1])
    assert original["author"]["channel_id"] == "20" and later["author"]["channel_id"] == "55"
    assert not later["same_channel"]
    world.members[1].voice = None
    assert build_voice_snapshot(world.cog.bot, world.guild, world.members[1])["author"]["connected"] is False


def test_voice_names_are_public_labels_and_cannot_inject_mentions(world):
    channel = voice(world, 55, "\n@everyone <@123> `ignore`")
    world.members[1].voice = SimpleNamespace(channel=channel)
    state = member_voice_snapshot(world.cog.bot, world.guild, world.members[1], viewer=world.members[1])
    assert "@" not in state["channel_name"] and "`" not in state["channel_name"]
    assert state["channel_mention"] == "<#55>"
