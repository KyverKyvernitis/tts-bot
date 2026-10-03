from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.chatbot import constants as C
from cogs.chatbot.config import ConfigStore, GuildChatbotConfig
from cogs.chatbot.spontaneous import is_spontaneous_candidate, roll_chance


def _config_doc(**changes):
    return {
        "type": C.DOC_TYPE_GUILD_CONFIG,
        "guild_id": 10,
        "enabled": False,
        "channel_ids": [],
        "spontaneous_enabled": False,
        "spontaneous_channel_ids": [],
        "spontaneous_chance_percent": 5,
        "schema_version": 3,
        "updated_at": 0,
        "updated_by": 0,
        **changes,
    }


def _interaction(*, user_id=40, guild_id=10):
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=SimpleNamespace(id=guild_id),
        response=SimpleNamespace(
            is_done=Mock(return_value=False),
            defer=AsyncMock(),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    async def _defer(**_kwargs):
        interaction.response.is_done.return_value = True

    interaction.response.defer.side_effect = _defer
    return interaction


class GuildConfigTests(unittest.TestCase):
    def test_new_guild_is_disabled_without_identity_configuration(self):
        config = GuildChatbotConfig(guild_id=10)

        self.assertFalse(config.enabled)
        self.assertEqual(config.channel_ids, ())
        self.assertFalse(config.spontaneous_enabled)
        self.assertEqual(config.spontaneous_channel_ids, ())
        self.assertEqual(config.spontaneous_chance_percent, 5)
        self.assertEqual(config.schema_version, 3)
        self.assertEqual(config.updated_by, 0)
        field_names = {field.name for field in dataclasses.fields(config)}
        self.assertFalse(any("profile" in name or "persona" in name for name in field_names))

    def test_empty_channel_list_allows_channels(self):
        config = GuildChatbotConfig(guild_id=10, enabled=True)

        self.assertTrue(config.allows_channel(20))
        self.assertTrue(config.allows_channel(30))

    def test_explicit_channel_list_restricts_access(self):
        config = GuildChatbotConfig(guild_id=10, enabled=True, channel_ids=(20,))

        self.assertTrue(config.allows_channel(20))
        self.assertFalse(config.allows_channel(30))


class ConfigStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.collection = AsyncMock()
        self.collection.find_one.return_value = None
        self.store = ConfigStore(self.collection)

    async def test_missing_config_is_disabled_and_cached(self):
        self.assertTrue(self.store.quick_might_apply(10, 20))

        first = await self.store.get_config(10)
        second = await self.store.get_config(10)

        self.assertEqual(first, GuildChatbotConfig(guild_id=10))
        self.assertEqual(second, first)
        self.collection.find_one.assert_awaited_once()
        self.assertFalse(self.store.quick_might_apply(10, 20))

    async def test_legacy_enabled_config_is_preserved_without_loading_identity(self):
        self.collection.find_one.return_value = _config_doc(
            enabled=True,
            schema_version=2,
            active_profile_id="obsolete-identity",
        )

        config = await self.store.get_config(10)

        self.assertTrue(config.enabled)
        self.assertFalse(hasattr(config, "active_profile_id"))
        self.collection.find_one.assert_awaited_once_with(
            {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 10}
        )
        self.assertFalse(config.spontaneous_enabled)

    async def test_save_persists_settings_and_replaces_cached_state(self):
        await self.store.get_config(10)
        self.assertFalse(self.store.quick_might_apply(10, 20))

        await self.store.save_config(
            guild_id=10,
            enabled=True,
            channel_ids=(20, 30),
            spontaneous_enabled=True,
            spontaneous_channel_ids=(30,),
            spontaneous_chance_percent=10,
            updated_by=40,
        )
        cached = await self.store.get_config(10)

        self.assertTrue(cached.enabled)
        self.assertEqual(cached.channel_ids, (20, 30))
        self.assertTrue(cached.spontaneous_enabled)
        self.assertEqual(cached.spontaneous_channel_ids, (30,))
        self.assertEqual(cached.spontaneous_chance_percent, 10)
        self.assertEqual(cached.updated_by, 40)
        self.assertGreater(cached.updated_at, 0)
        self.assertEqual(self.collection.find_one.await_count, 2)
        self.assertFalse(self.store.quick_might_apply(10, 20))
        self.assertTrue(self.store.quick_might_apply(10, 30))
        self.assertFalse(self.store.quick_might_apply(10, 99))
        self.collection.update_one.assert_awaited_once()
        args, kwargs = self.collection.update_one.await_args
        self.assertEqual(args[0], {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 10})
        self.assertTrue(kwargs["upsert"])
        saved = args[1]["$set"]
        self.assertEqual(saved["enabled"], True)
        self.assertEqual(saved["schema_version"], 3)
        self.assertEqual(saved["channel_ids"], [20, 30])
        self.assertEqual(saved["spontaneous_channel_ids"], [30])
        self.assertFalse(any("profile" in key or "persona" in key for key in saved))

    async def test_saved_config_can_be_loaded_after_restart(self):
        await self.store.save_config(
            guild_id=10,
            enabled=True,
            channel_ids=(20,),
            spontaneous_enabled=False,
            spontaneous_channel_ids=(),
            spontaneous_chance_percent=5,
            updated_by=40,
        )
        saved = self.collection.update_one.await_args.args[1]["$set"]
        self.collection.find_one.return_value = saved

        restarted = ConfigStore(self.collection)
        config = await restarted.get_config(10)

        self.assertTrue(config.enabled)
        self.assertEqual(config.channel_ids, (20,))
        self.assertFalse(restarted.quick_might_apply(10, 30))

    async def test_disable_replaces_positive_cache_immediately(self):
        self.collection.find_one.return_value = _config_doc(
            enabled=True,
            spontaneous_enabled=True,
            spontaneous_channel_ids=[20],
        )
        await self.store.get_config(10)
        self.assertTrue(self.store.quick_might_apply(10, 20))

        await self.store.save_config(
            guild_id=10,
            enabled=False,
            channel_ids=(),
            spontaneous_enabled=False,
            spontaneous_channel_ids=(),
            spontaneous_chance_percent=5,
            updated_by=40,
        )

        self.assertFalse(self.store.quick_might_apply(10, 20))
        self.assertFalse((await self.store.get_config(10)).enabled)

    async def test_failed_write_does_not_enable_cached_chatbot(self):
        await self.store.get_config(10)
        self.collection.update_one.side_effect = RuntimeError("database unavailable")

        with self.assertRaises(RuntimeError):
            await self.store.save_config(
                guild_id=10,
                enabled=True,
                channel_ids=(),
                spontaneous_enabled=False,
                spontaneous_channel_ids=(),
                spontaneous_chance_percent=5,
                updated_by=40,
            )

        self.assertFalse((await self.store.get_config(10)).enabled)
        self.assertFalse(self.store.quick_might_apply(10, 20))

    async def test_invalid_spontaneous_settings_never_reach_database(self):
        invalid = [
            {"spontaneous_enabled": True, "spontaneous_channel_ids": ()},
            {"spontaneous_enabled": True, "spontaneous_channel_ids": (99,)},
            {"spontaneous_chance_percent": 0},
            {"spontaneous_chance_percent": 21},
        ]
        for override in invalid:
            with self.subTest(override=override):
                settings = dict(
                    guild_id=10,
                    enabled=True,
                    channel_ids=(20,),
                    spontaneous_enabled=False,
                    spontaneous_channel_ids=(),
                    spontaneous_chance_percent=5,
                    updated_by=40,
                )
                settings.update(override)
                with self.assertRaises(ValueError):
                    await self.store.save_config(**settings)

        self.collection.update_one.assert_not_awaited()

    async def test_spontaneous_channels_are_allowed_with_unrestricted_chat_channels(self):
        await self.store.save_config(
            guild_id=10,
            enabled=True,
            channel_ids=(),
            spontaneous_enabled=True,
            spontaneous_channel_ids=(20,),
            spontaneous_chance_percent=5,
            updated_by=40,
        )

        config = await self.store.get_config(10)
        self.assertTrue(config.allows_channel(30))
        self.assertEqual(config.spontaneous_channel_ids, (20,))

    async def test_unsafe_persisted_spontaneous_settings_are_disabled_on_read(self):
        for channels in ([], [99]):
            with self.subTest(channels=channels):
                collection = AsyncMock()
                collection.find_one.return_value = _config_doc(
                    enabled=True,
                    channel_ids=[20],
                    spontaneous_enabled=True,
                    spontaneous_channel_ids=channels,
                )
                config = await ConfigStore(collection).get_config(10)

                self.assertTrue(config.enabled)
                self.assertFalse(config.spontaneous_enabled)


def _spontaneous_message(**changes):
    return SimpleNamespace(
        **{
            "guild": SimpleNamespace(id=10),
            "channel": SimpleNamespace(id=20),
            "author": SimpleNamespace(id=40, bot=False),
            "webhook_id": None,
            "type": discord.MessageType.default,
            "reference": None,
            "content": "Alguém conhece um jogo divertido para jogar neste fim de semana?",
            **changes,
        }
    )


class SpontaneousTests(unittest.TestCase):
    def setUp(self):
        self.config = GuildChatbotConfig(
            guild_id=10,
            enabled=True,
            channel_ids=(20, 30),
            spontaneous_enabled=True,
            spontaneous_channel_ids=(20,),
            spontaneous_chance_percent=5,
        )

    def test_human_message_in_explicit_channel_can_trigger(self):
        self.assertTrue(is_spontaneous_candidate(_spontaneous_message(), self.config))

    def test_regularly_allowed_channel_does_not_imply_spontaneous_permission(self):
        message = _spontaneous_message(channel=SimpleNamespace(id=30))

        self.assertFalse(is_spontaneous_candidate(message, self.config))

    def test_disabled_bot_or_spontaneous_option_blocks_candidates(self):
        for changes in ({"enabled": False}, {"spontaneous_enabled": False}):
            with self.subTest(changes=changes):
                config = dataclasses.replace(self.config, **changes)
                self.assertFalse(is_spontaneous_candidate(_spontaneous_message(), config))

    def test_spontaneous_channel_cannot_bypass_channel_restriction(self):
        config = dataclasses.replace(self.config, channel_ids=(30,))

        self.assertFalse(is_spontaneous_candidate(_spontaneous_message(), config))

    def test_guild_mismatch_or_missing_guild_is_ignored(self):
        for guild in (None, SimpleNamespace(id=99)):
            with self.subTest(guild=guild):
                self.assertFalse(is_spontaneous_candidate(
                    _spontaneous_message(guild=guild), self.config,
                ))

    def test_bot_webhook_reply_and_system_messages_are_ignored(self):
        ignored = [
            {"author": SimpleNamespace(id=40, bot=True)},
            {"webhook_id": 50},
            {"reference": SimpleNamespace(message_id=60)},
            {"type": discord.MessageType.reply},
            {"type": discord.MessageType.pins_add},
        ]
        for changes in ignored:
            with self.subTest(changes=changes):
                self.assertFalse(is_spontaneous_candidate(
                    _spontaneous_message(**changes), self.config,
                ))

    def test_commands_mass_mentions_links_and_trivial_text_are_ignored(self):
        contents = [
            "",
            "oi",
            "🙂" * 100,
            "https://example.com/uma-mensagem-longa-que-nao-deve-ativar-o-chatbot",
            "@everyone Vamos discutir os melhores jogos neste fim de semana",
            "@here Vamos discutir os melhores jogos neste fim de semana",
        ]
        contents.extend(
            prefix + "comando com argumentos suficientes para ultrapassar o mínimo"
            for prefix in ("/", "!", "?", ".", "_", ";", "@")
        )
        for content in contents:
            with self.subTest(content=content):
                self.assertFalse(is_spontaneous_candidate(
                    _spontaneous_message(content=content), self.config,
                ))

    def test_chance_respects_configured_percentage(self):
        with patch("cogs.chatbot.spontaneous.random.random", return_value=0.049):
            self.assertTrue(roll_chance(self.config))
        with patch("cogs.chatbot.spontaneous.random.random", return_value=0.05):
            self.assertFalse(roll_chance(self.config))


class ModalAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_submit_rechecks_authorization_before_saving(self):
        from cogs.chatbot.views import ChatbotConfigModal

        authorized = AsyncMock(return_value=False)
        save = AsyncMock()
        modal = ChatbotConfigModal(
            requester_id=40,
            current_config=GuildChatbotConfig(guild_id=10),
            on_submit_config=save,
            check_authorized=authorized,
        )
        interaction = _interaction()

        await modal.on_submit(interaction)

        authorized.assert_awaited_once_with(interaction)
        save.assert_not_awaited()

    async def test_config_modal_rejects_another_user(self):
        from cogs.chatbot.views import ChatbotConfigModal

        authorized = AsyncMock(return_value=True)
        save = AsyncMock()
        modal = ChatbotConfigModal(
            requester_id=40,
            current_config=GuildChatbotConfig(guild_id=10),
            on_submit_config=save,
            check_authorized=authorized,
        )

        await modal.on_submit(_interaction(user_id=99))

        authorized.assert_not_awaited()
        save.assert_not_awaited()

    async def test_config_default_submission_preserves_disabled_new_guild(self):
        from cogs.chatbot.views import ChatbotConfigModal

        authorized = AsyncMock(return_value=True)
        save = AsyncMock()
        modal = ChatbotConfigModal(
            requester_id=40,
            current_config=GuildChatbotConfig(guild_id=10),
            on_submit_config=save,
            check_authorized=authorized,
        )
        interaction = _interaction()

        await modal.on_submit(interaction)

        authorized.assert_awaited_once_with(interaction)
        save.assert_awaited_once()
        submitted_interaction, config = save.await_args.args
        self.assertIs(submitted_interaction, interaction)
        self.assertEqual(config.guild_id, 10)
        self.assertFalse(config.enabled)
        self.assertFalse(config.spontaneous_enabled)

    async def test_master_submit_rechecks_authorization_before_writing(self):
        from cogs.chatbot.views import MasterEditModal

        store = SimpleNamespace(update_prompt=AsyncMock())
        authorized = AsyncMock(return_value=False)
        modal = MasterEditModal(
            master_store=store,
            current_content="Instrução global do bot",
            requester_id=40,
            check_authorized=authorized,
        )
        interaction = _interaction()

        await modal.on_submit(interaction)

        authorized.assert_awaited_once_with(interaction)
        store.update_prompt.assert_not_awaited()

    async def test_master_modal_rejects_another_user(self):
        from cogs.chatbot.views import MasterEditModal

        store = SimpleNamespace(update_prompt=AsyncMock())
        authorized = AsyncMock(return_value=True)
        modal = MasterEditModal(
            master_store=store,
            current_content="Instrução global do bot",
            requester_id=40,
            check_authorized=authorized,
        )

        await modal.on_submit(_interaction(user_id=99))

        authorized.assert_not_awaited()
        store.update_prompt.assert_not_awaited()

    async def test_authorized_master_submit_writes_as_requester(self):
        from cogs.chatbot.views import MasterEditModal

        store = SimpleNamespace(update_prompt=AsyncMock(
            return_value=SimpleNamespace(prompt="Nova instrução global"),
        ))
        authorized = AsyncMock(return_value=True)
        modal = MasterEditModal(
            master_store=store,
            current_content="Instrução global do bot",
            requester_id=40,
            check_authorized=authorized,
        )
        modal.prompt_input._value = "Nova instrução global"
        interaction = _interaction()

        await modal.on_submit(interaction)

        authorized.assert_awaited_once_with(interaction)
        store.update_prompt.assert_awaited_once_with(
            "Nova instrução global", updated_by=40,
        )


class CommandAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_staff_check_uses_current_guild_permissions(self):
        from cogs.chatbot.commands import _staff_check

        stale_member = Mock(spec=discord.Member)
        stale_member.id = 40
        stale_member.guild_permissions = discord.Permissions(manage_guild=True)
        current_member = Mock(spec=discord.Member)
        current_member.id = 40
        current_member.guild_permissions = discord.Permissions.none()
        interaction = _interaction()
        interaction.user = stale_member
        interaction.guild.owner_id = 99
        interaction.guild.get_member = Mock(return_value=current_member)

        self.assertFalse(await _staff_check(interaction))

    async def test_config_command_rechecks_permission_after_modal_open(self):
        from cogs.chatbot.commands import ChatbotCommandsMixin

        cog = ChatbotCommandsMixin()
        cog._config = SimpleNamespace(
            get_config=AsyncMock(return_value=GuildChatbotConfig(guild_id=10)),
            save_config=AsyncMock(),
        )
        cog._memory = object()
        command_interaction = _interaction()
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(
            side_effect=[True, True, False],
        )) as staff_check:
            await cog._do_configurar(command_interaction)
            view = command_interaction.followup.send.await_args.kwargs["view"]
            click_interaction = _interaction()
            await view._open(click_interaction)
            modal = click_interaction.response.send_modal.await_args.args[0]
            submit_interaction = _interaction()

            await modal.on_submit(submit_interaction)

        self.assertEqual(staff_check.await_count, 3)
        cog._config.save_config.assert_not_awaited()
        submit_interaction.followup.send.assert_awaited_once()

    async def test_config_handler_rechecks_permission_and_guild_before_write(self):
        from cogs.chatbot.commands import ChatbotCommandsMixin

        cog = ChatbotCommandsMixin()
        cog._config = SimpleNamespace(save_config=AsyncMock())
        cog._memory = object()
        settings = GuildChatbotConfig(guild_id=10, enabled=True)
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=False)):
            await cog._handle_config_modal(_interaction(), settings)
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
            await cog._handle_config_modal(_interaction(guild_id=99), settings)

        cog._config.save_config.assert_not_awaited()

    async def test_master_transfer_revokes_already_open_modal(self):
        from cogs.chatbot.commands import ChatbotCommandsMixin

        cog = ChatbotCommandsMixin()
        cog._master = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(
                config_guild_id=10, prompt="Instrução global do bot",
            )),
            update_prompt=AsyncMock(),
        )
        command_interaction = _interaction()
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
            await cog._do_master_editar(command_interaction)
            view = command_interaction.followup.send.await_args.kwargs["view"]
            click_interaction = _interaction()
            await view._open(click_interaction)
            modal = click_interaction.response.send_modal.await_args.args[0]
            cog._master.get.return_value = SimpleNamespace(
                config_guild_id=99, prompt="Instrução global do bot",
            )
            submit_interaction = _interaction()

            await modal.on_submit(submit_interaction)

        self.assertEqual(cog._master.get.await_count, 3)
        cog._master.update_prompt.assert_not_awaited()
        submit_interaction.followup.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
