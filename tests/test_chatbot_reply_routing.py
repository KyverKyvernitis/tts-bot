from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot import constants as C
from cogs.chatbot.config import GuildChatbotConfig


class _RecordingSupervisor:
    def __init__(self) -> None:
        self.names: list[str] = []

    def create(self, coro, *, name: str):
        self.names.append(name)
        # O listener entrega a coroutine ao supervisor sem executar I/O.
        coro.close()
        return None


def _cog(*, enabled: bool = True, channel_ids: tuple[int, ...] = (20,)) -> ChatbotCog:
    cog = object.__new__(ChatbotCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog._router = object()
    cog._config = SimpleNamespace(
        get_config=AsyncMock(return_value=GuildChatbotConfig(
            guild_id=10, enabled=enabled, channel_ids=channel_ids,
        )),
        quick_might_apply=Mock(return_value=False),
    )
    cog._message_index = SimpleNamespace(resolve=AsyncMock(return_value=None))
    cog._supervisor = _RecordingSupervisor()
    return cog


def _target(*, author_id: int = 999, webhook_id: int | None = None):
    target = Mock(spec=discord.Message)
    target.id = 50
    target.author = SimpleNamespace(id=author_id, bot=True)
    target.webhook_id = webhook_id
    target.guild = SimpleNamespace(id=10)
    target.channel = SimpleNamespace(id=20)
    return target


def _message(*, resolved=None, content: str = "Sla", message_type=discord.MessageType.reply):
    channel = Mock(spec=discord.TextChannel)
    channel.id = 20
    channel.fetch_message = AsyncMock(return_value=None)
    return SimpleNamespace(
        id=30,
        author=SimpleNamespace(id=40, bot=False),
        webhook_id=None,
        type=message_type,
        guild=SimpleNamespace(id=10),
        channel=channel,
        content=content,
        reference=SimpleNamespace(message_id=50, resolved=resolved),
    )


def _indexed(cog: ChatbotCog, *, guild_id: int = 10, channel_id: int = 20):
    cog._message_index.resolve.return_value = SimpleNamespace(
        guild_id=guild_id, channel_id=channel_id, message_id=50,
    )


class ReplyListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_reply_to_bot_schedules_validation_without_io(self):
        cog = _cog()
        message = _message(resolved=_target())

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])
        cog._config.get_config.assert_not_awaited()
        cog._message_index.resolve.assert_not_awaited()
        message.channel.fetch_message.assert_not_awaited()

    async def test_unresolved_reply_schedules_persisted_index_validation(self):
        cog = _cog()
        message = _message()

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])
        message.channel.fetch_message.assert_not_awaited()

    async def test_reply_to_ordinary_user_is_ignored(self):
        cog = _cog()
        target = _target(author_id=123)
        target.author.bot = False
        await cog.on_message(_message(resolved=target))
        self.assertEqual(cog._supervisor.names, [])

    async def test_reply_to_another_bot_is_ignored(self):
        cog = _cog()
        await cog.on_message(_message(resolved=_target(author_id=123)))
        self.assertEqual(cog._supervisor.names, [])

    async def test_reply_to_legacy_webhook_is_ignored(self):
        cog = _cog()
        await cog.on_message(_message(resolved=_target(author_id=456, webhook_id=456)))
        self.assertEqual(cog._supervisor.names, [])

    async def test_reply_to_application_original_response_schedules_index_validation(self):
        # edit_original_response de /imagem pertence ao aplicativo, mas tem
        # webhook_id mesmo que o autor continue sendo o próprio bot.
        cog = _cog()
        message = _message(resolved=_target(author_id=999, webhook_id=999))

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])
        cog._message_index.resolve.assert_not_awaited()
        message.channel.fetch_message.assert_not_awaited()

    async def test_system_message_is_ignored(self):
        cog = _cog()
        await cog.on_message(_message(
            message_type=discord.MessageType.pins_add, resolved=_target(),
        ))
        self.assertEqual(cog._supervisor.names, [])

    async def test_incoming_webhook_reply_is_ignored(self):
        cog = _cog()
        message = _message(resolved=_target())
        message.webhook_id = 777
        await cog.on_message(message)
        self.assertEqual(cog._supervisor.names, [])

    async def test_incoming_bot_reply_is_ignored(self):
        cog = _cog()
        message = _message(resolved=_target())
        message.author.bot = True
        await cog.on_message(message)
        self.assertEqual(cog._supervisor.names, [])

    async def test_bot_mention_still_schedules_one_turn(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.default, content="<@999> oi",
        )
        message.reference = None
        await cog.on_message(message)
        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])

    async def test_plain_old_persona_name_has_no_direct_trigger(self):
        cog = _cog()
        cog._config.quick_might_apply.return_value = False
        message = _message(
            message_type=discord.MessageType.default, content="@Osaka oi",
        )
        message.reference = None
        await cog.on_message(message)
        self.assertEqual(cog._supervisor.names, [])

    async def test_spontaneous_cache_miss_schedules_validation_without_io(self):
        cog = _cog()
        cog._config.quick_might_apply.return_value = True
        message = _message(
            message_type=discord.MessageType.default,
            content="bom dia, alguém vai jogar hoje?",
        )
        message.reference = None

        with patch.object(C, "SAFE_MODE", False):
            await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])
        cog._config.get_config.assert_not_awaited()
        cog._message_index.resolve.assert_not_awaited()
        message.channel.fetch_message.assert_not_awaited()


class ReplyResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_index_and_bot_author_resolve_reply_without_identity_lookup(self):
        cog = _cog()
        _indexed(cog)
        message = _message(resolved=_target())

        trigger = await cog._resolve_trigger(message)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.via, "reply")
        self.assertEqual(trigger.content, "Sla")
        self.assertFalse(hasattr(trigger, "profile"))
        self.assertFalse(hasattr(trigger, "is_temporary"))
        cog._message_index.resolve.assert_awaited_once_with(50)
        message.channel.fetch_message.assert_not_awaited()

    async def test_other_function_message_from_same_bot_is_not_a_chatbot_reply(self):
        # Música e jogos usam a mesma identidade, mas não entram no índice.
        cog = _cog()
        trigger = await cog._resolve_trigger(_message(resolved=_target()))
        self.assertIsNone(trigger)

    async def test_index_cannot_authorize_another_user_or_legacy_webhook(self):
        for target in (_target(author_id=123), _target(author_id=456, webhook_id=456)):
            with self.subTest(target=target):
                cog = _cog()
                _indexed(cog)
                self.assertIsNone(await cog._resolve_trigger(_message(resolved=target)))

    async def test_reply_to_slash_image_original_response_uses_index_and_bot_author(self):
        cog = _cog()
        _indexed(cog)
        target = _target(author_id=999, webhook_id=999)
        self.assertIsInstance(target, discord.Message)
        message = _message(resolved=target, content="Faça outra imagem nesse estilo")

        trigger = await cog._resolve_trigger(message)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.via, "reply")
        self.assertEqual(trigger.content, "Faça outra imagem nesse estilo")
        cog._message_index.resolve.assert_awaited_once_with(50)
        message.channel.fetch_message.assert_not_awaited()

    async def test_application_original_response_without_chatbot_index_is_ignored(self):
        cog = _cog()
        target = _target(author_id=999, webhook_id=999)

        self.assertIsNone(await cog._resolve_trigger(_message(resolved=target)))

    async def test_index_mapping_must_match_guild_and_channel(self):
        for guild_id, channel_id in ((999, 20), (10, 999)):
            with self.subTest(guild_id=guild_id, channel_id=channel_id):
                cog = _cog()
                _indexed(cog, guild_id=guild_id, channel_id=channel_id)
                self.assertIsNone(await cog._resolve_trigger(_message(resolved=_target())))

    async def test_index_still_resolves_when_discord_target_is_unavailable(self):
        cog = _cog()
        _indexed(cog)
        message = _message()

        trigger = await cog._resolve_trigger(message)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.via, "reply")

    async def test_reply_is_ignored_when_chatbot_is_disabled(self):
        cog = _cog(enabled=False)
        _indexed(cog)
        self.assertIsNone(await cog._resolve_trigger(_message(resolved=_target())))

    async def test_reply_is_ignored_outside_configured_channels(self):
        cog = _cog(channel_ids=(21,))
        _indexed(cog)
        self.assertIsNone(await cog._resolve_trigger(_message(resolved=_target())))

    async def test_configured_parent_channel_allows_reply_in_its_thread(self):
        cog = _cog(channel_ids=(20,))
        _indexed(cog, channel_id=22)
        message = _message(resolved=_target())
        thread = Mock(spec=discord.Thread)
        thread.id = 22
        thread.parent_id = 20
        thread.fetch_message = AsyncMock(return_value=None)
        message.channel = thread

        trigger = await cog._resolve_trigger(message)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.via, "reply")

    async def test_thread_reply_cannot_use_parent_channel_message_mapping(self):
        cog = _cog(channel_ids=(20,))
        _indexed(cog, channel_id=20)
        message = _message(resolved=_target())
        thread = Mock(spec=discord.Thread)
        thread.id = 22
        thread.parent_id = 20
        message.channel = thread

        self.assertIsNone(await cog._resolve_trigger(message))

    async def test_initial_bot_mention_strips_only_actual_mention(self):
        for content in ("<@999> oi", "  <@!999> oi"):
            with self.subTest(content=content):
                cog = _cog()
                message = _message(content=content)
                message.reference = None
                trigger = await cog._resolve_trigger(message)
                self.assertIsNotNone(trigger)
                self.assertEqual(trigger.via, "bot_mention")
                self.assertEqual(trigger.content, "oi")
                cog._message_index.resolve.assert_not_awaited()

    async def test_name_or_later_mention_cannot_invoke_chatbot(self):
        for content in ("@Osaka oi", "oi <@999>", "<@123> oi"):
            with self.subTest(content=content):
                cog = _cog()
                message = _message(content=content)
                message.reference = None
                self.assertIsNone(await cog._resolve_trigger(message))

    async def test_spontaneous_mode_invokes_same_bot_without_profile(self):
        cog = _cog()
        cog._config.get_config.return_value = GuildChatbotConfig(
            guild_id=10, enabled=True, channel_ids=(20,),
            spontaneous_enabled=True, spontaneous_channel_ids=(20,),
        )
        cog._is_spontaneous_on_cooldown = Mock(return_value=False)
        message = _message(
            message_type=discord.MessageType.default,
            content="bom dia, alguém vai jogar hoje?",
        )
        message.reference = None

        with patch.object(C, "SAFE_MODE", False), patch("cogs.chatbot.cog.roll_chance", return_value=True):
            trigger = await cog._resolve_trigger(message)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.via, "spontaneous")
        self.assertTrue(trigger.behavior_hint)
        self.assertFalse(hasattr(trigger, "profile"))
        cog._message_index.resolve.assert_not_awaited()


class ReplyTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_reply_target_avoids_discord_fetch(self):
        cog = _cog()
        target = _target()
        message = _message(resolved=target)
        self.assertIs(await cog._resolve_reply_target(message), target)
        message.channel.fetch_message.assert_not_awaited()

    async def test_uncached_target_is_fetched_for_reply_context(self):
        cog = _cog()
        target = _target()
        message = _message()
        message.channel.fetch_message.return_value = target
        self.assertIs(await cog._resolve_reply_target(message), target)
        message.channel.fetch_message.assert_awaited_once_with(50)

    async def test_discord_fetch_failure_has_no_reply_context(self):
        cog = _cog()
        message = _message()
        message.channel.fetch_message.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "missing permission",
        )
        self.assertIsNone(await cog._resolve_reply_target(message))


if __name__ == "__main__":
    unittest.main()
