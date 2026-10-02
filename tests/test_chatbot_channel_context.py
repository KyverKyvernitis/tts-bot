"""Restrição de idade herdada em threads sem perder escopo de memória."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.config import GuildChatbotConfig
from cogs.chatbot.media import channel_is_nsfw
from cogs.chatbot.memory import MemoryEpoch


@pytest.mark.parametrize('restricted', [False, True])
def test_actual_discord_thread_inherits_parent_age_restriction(restricted):
    parent = SimpleNamespace(is_nsfw=lambda: restricted)
    thread = object.__new__(discord.Thread)
    thread.parent_id = 10
    thread.guild = SimpleNamespace(get_channel=lambda channel_id: parent if channel_id == 10 else None)
    assert channel_is_nsfw(thread) is restricted
    thread.guild = SimpleNamespace(get_channel=lambda _channel_id: None)
    assert not channel_is_nsfw(thread)


@pytest.mark.asyncio
@pytest.mark.parametrize('guild_id,effective', [(C.MANAGEMENT_GUILD_ID, True), (1, False)])
async def test_slash_image_uses_thread_parent_restriction_and_own_memory_scope(guild_id, effective):
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ChatbotCog(bot)
    config = GuildChatbotConfig(guild_id=guild_id, enabled=True, channel_ids=(10,))
    cog._config = SimpleNamespace(get_config=AsyncMock(return_value=config))
    epoch = MemoryEpoch(user_generation=7)
    cog._memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=epoch))
    cog._image_service = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(
        ok=True, image=SimpleNamespace(data=b'image bytes', mime_type='image/png'),
    )))
    cog._remember_sent_message = AsyncMock()
    cog._persist_turn = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 20, 10
    thread.is_nsfw.return_value = True
    thread.is_private.return_value = False
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=guild_id), channel=thread,
        user=SimpleNamespace(id=3, name='Ana', display_name='Ana'),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(id=77)),
    )

    await cog._run_image_command(interaction, prompt='uma paisagem')

    cog._image_service.generate.assert_awaited_once_with(
        prompt='uma paisagem', channel_is_nsfw=effective, slot_acquired=True,
    )
    cog._remember_sent_message.assert_awaited_once_with(guild_id=guild_id, channel_id=20, message_id=77)
    saved = cog._persist_turn.await_args.kwargs
    assert saved['epoch'] == epoch
    assert saved['channel_id'] == 20
    assert saved['visibility_scope'] == ('nsfw:20' if effective else 'channel:20')
