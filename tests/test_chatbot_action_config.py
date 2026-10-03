from __future__ import annotations

import copy
import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.chatbot import constants as C
from cogs.chatbot.config import ConfigStore, GuildChatbotConfig


class _Collection:
    """Apply Mongo partial updates so tests observe preserved persisted fields."""

    def __init__(self, *configs):
        self.documents = {config.guild_id: config.to_doc() for config in configs}
        self.reads = []
        self.writes = []
        self.before_update = None

    async def find_one(self, query):
        self.reads.append(copy.deepcopy(query))
        assert query["type"] == C.DOC_TYPE_GUILD_CONFIG
        return copy.deepcopy(self.documents.get(query["guild_id"]))

    async def update_one(self, query, update, *, upsert):
        self.writes.append((copy.deepcopy(query), copy.deepcopy(update), upsert))
        assert query["type"] == C.DOC_TYPE_GUILD_CONFIG
        if self.before_update is not None:
            callback, self.before_update = self.before_update, None
            await callback()
        guild_id = query["guild_id"]
        if guild_id not in self.documents:
            self.documents[guild_id] = copy.deepcopy(update.get("$setOnInsert", {}))
        self.documents[guild_id].update(copy.deepcopy(update["$set"]))


def _interaction(*, user_id=40, guild_id=10):
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=SimpleNamespace(id=guild_id),
        response=SimpleNamespace(
            is_done=Mock(return_value=False), defer=AsyncMock(),
            send_message=AsyncMock(), send_modal=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    async def defer(**_kwargs):
        interaction.response.is_done.return_value = True

    interaction.response.defer.side_effect = defer
    return interaction


def _conversation_settings(**changes):
    return {
        "guild_id": 10, "enabled": True, "channel_ids": (20,),
        "spontaneous_enabled": True, "spontaneous_channel_ids": (20,),
        "spontaneous_chance_percent": 8, "updated_by": 40, **changes,
    }


def _action_settings(**changes):
    return {
        "guild_id": 10, "actions_enabled": True, "audio_actions_enabled": False,
        "voice_actions_enabled": True, "moderation_actions_enabled": False,
        "action_staff_role_ids": (80, 90), "updated_by": 40, **changes,
    }


class ActionConfigDocumentTests(unittest.TestCase):
    def test_new_and_legacy_config_enable_actions_without_enabling_chat(self):
        for document in (None, {"enabled": False, "schema_version": 3}):
            with self.subTest(document=document):
                config = GuildChatbotConfig.from_doc(document, guild_id=10)
                self.assertFalse(config.enabled)
                self.assertTrue(config.actions_enabled)
                self.assertTrue(config.audio_actions_enabled)
                self.assertTrue(config.voice_actions_enabled)
                self.assertTrue(config.moderation_actions_enabled)
                self.assertEqual(config.action_staff_role_ids, ())

    def test_roundtrip_preserves_disabled_action_groups_and_staff_roles(self):
        config = GuildChatbotConfig(
            guild_id=10, enabled=True, channel_ids=(20,),
            actions_enabled=False, audio_actions_enabled=True,
            voice_actions_enabled=False, moderation_actions_enabled=False,
            action_staff_role_ids=(80, 90), updated_by=40, updated_at=123.0,
        )

        restored = GuildChatbotConfig.from_doc(config.to_doc(), guild_id=10)

        self.assertEqual(restored, config)
        self.assertEqual(config.to_doc()["action_staff_role_ids"], [80, 90])

    def test_staff_roles_are_positive_and_deduplicated_on_read(self):
        config = GuildChatbotConfig.from_doc({
            "action_staff_role_ids": [80, "90", 80, 0, -1],
        }, guild_id=10)

        self.assertEqual(config.action_staff_role_ids, (80, 90))


class ActionConfigStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_read_observes_external_revocation_and_updates_cache(self):
        collection = _Collection(GuildChatbotConfig(guild_id=10, enabled=True))
        store = ConfigStore(collection)
        initial = await store.get_config(10)
        collection.documents[10]["actions_enabled"] = False

        self.assertIs(await store.get_config(10), initial)
        fresh = await store.get_config(10, fresh=True)

        self.assertFalse(fresh.actions_enabled)
        self.assertIs(await store.get_config(10), fresh)
        self.assertEqual(len(collection.reads), 2)

    async def test_action_save_preserves_conversation_and_survives_restart(self):
        initial = GuildChatbotConfig(
            guild_id=10, enabled=True, channel_ids=(20, 30),
            spontaneous_enabled=True, spontaneous_channel_ids=(30,),
            spontaneous_chance_percent=13,
        )
        collection = _Collection(initial)
        store = ConfigStore(collection)

        saved = await store.save_action_config(**_action_settings())
        restarted = await ConfigStore(collection).get_config(10)

        self.assertEqual(restarted, saved)
        self.assertEqual(saved.channel_ids, (20, 30))
        self.assertTrue(saved.enabled)
        self.assertTrue(saved.spontaneous_enabled)
        self.assertEqual(saved.spontaneous_channel_ids, (30,))
        self.assertEqual(saved.spontaneous_chance_percent, 13)
        self.assertFalse(saved.audio_actions_enabled)
        self.assertFalse(saved.moderation_actions_enabled)
        self.assertEqual(saved.action_staff_role_ids, (80, 90))
        self.assertEqual(saved.updated_by, 40)
        self.assertGreater(saved.updated_at, 0)

    async def test_action_save_does_not_erase_concurrent_conversation_change(self):
        collection = _Collection(GuildChatbotConfig(guild_id=10, enabled=True))
        store = ConfigStore(collection)
        await store.get_config(10)

        async def change_conversation():
            collection.documents[10].update(enabled=False, channel_ids=[30])

        collection.before_update = change_conversation
        saved = await store.save_action_config(**_action_settings())

        self.assertFalse(saved.enabled)
        self.assertEqual(saved.channel_ids, (30,))
        self.assertFalse(saved.audio_actions_enabled)

    async def test_conversation_save_preserves_action_settings_changed_after_cache(self):
        collection = _Collection(GuildChatbotConfig(guild_id=10))
        store = ConfigStore(collection)
        await store.get_config(10)
        collection.documents[10].update(
            actions_enabled=False, audio_actions_enabled=False,
            voice_actions_enabled=False, moderation_actions_enabled=False,
            action_staff_role_ids=[90],
        )

        saved = await store.save_config(**_conversation_settings())

        self.assertTrue(saved.enabled)
        self.assertFalse(saved.actions_enabled)
        self.assertFalse(saved.audio_actions_enabled)
        self.assertFalse(saved.voice_actions_enabled)
        self.assertFalse(saved.moderation_actions_enabled)
        self.assertEqual(saved.action_staff_role_ids, (90,))
        persisted = await ConfigStore(collection).get_config(10)
        self.assertEqual(persisted, saved)

    async def test_conversation_write_does_not_erase_action_change_during_write(self):
        collection = _Collection(GuildChatbotConfig(guild_id=10))
        store = ConfigStore(collection)

        async def change_actions():
            collection.documents[10].update(actions_enabled=False, action_staff_role_ids=[90])

        collection.before_update = change_actions
        await store.save_config(**_conversation_settings())
        persisted = await store.get_config(10, fresh=True)

        self.assertTrue(persisted.enabled)
        self.assertFalse(persisted.actions_enabled)
        self.assertEqual(persisted.action_staff_role_ids, (90,))

    async def test_action_save_upsert_keeps_new_chatbot_disabled(self):
        collection = _Collection()
        saved = await ConfigStore(collection).save_action_config(**_action_settings(
            action_staff_role_ids=(80, "90", 80, 0, -1),
        ))

        self.assertFalse(saved.enabled)
        self.assertFalse(saved.spontaneous_enabled)
        self.assertEqual(saved.schema_version, C.CHATBOT_SCHEMA_VERSION)
        self.assertEqual(saved.action_staff_role_ids, (80, 90))

    async def test_guild_action_write_and_fresh_reads_remain_isolated(self):
        other = GuildChatbotConfig(guild_id=99, actions_enabled=False, action_staff_role_ids=(99,))
        collection = _Collection(GuildChatbotConfig(guild_id=10), other)
        store = ConfigStore(collection)
        await store.get_config(99)
        await store.save_action_config(**_action_settings())

        self.assertEqual(await store.get_config(99, fresh=True), other)
        self.assertEqual(collection.documents[99], other.to_doc())
        self.assertEqual(collection.writes[0][0], {
            "type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 10,
        })

    async def test_failed_action_write_does_not_replace_cached_configuration(self):
        collection = _Collection(GuildChatbotConfig(guild_id=10, actions_enabled=False))
        store = ConfigStore(collection)
        initial = await store.get_config(10)
        collection.update_one = AsyncMock(side_effect=RuntimeError("database unavailable"))

        with self.assertRaises(RuntimeError):
            await store.save_action_config(**_action_settings())

        self.assertIs(await store.get_config(10), initial)
        self.assertFalse(initial.actions_enabled)


class ActionConfigModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_form_has_four_groups_and_optional_staff_roles(self):
        from cogs.chatbot.views import ActionConfigModal

        modal = ActionConfigModal(
            requester_id=40,
            current_config=GuildChatbotConfig(guild_id=10, action_staff_role_ids=(80, 90)),
            on_submit_config=AsyncMock(), check_authorized=AsyncMock(return_value=True),
        )

        self.assertEqual(len(modal.children), 5)
        for group in (modal.actions_group, modal.audio_group, modal.voice_group, modal.moderation_group):
            self.assertEqual([option.value for option in group.options if option.default], ["on"])
        self.assertEqual(modal.staff_roles.min_values, 0)
        self.assertEqual(modal.staff_roles.max_values, 10)
        self.assertEqual([value.id for value in modal.staff_roles.default_values], [80, 90])

    async def test_modal_rejects_another_user_before_authorization_or_write(self):
        from cogs.chatbot.views import ActionConfigModal

        authorized, save = AsyncMock(return_value=True), AsyncMock()
        modal = ActionConfigModal(
            requester_id=40, current_config=GuildChatbotConfig(guild_id=10),
            on_submit_config=save, check_authorized=authorized,
        )

        await modal.on_submit(_interaction(user_id=99))

        authorized.assert_not_awaited()
        save.assert_not_awaited()

    async def test_modal_rechecks_staff_authorization_on_submit(self):
        from cogs.chatbot.views import ActionConfigModal

        authorized, save = AsyncMock(return_value=False), AsyncMock()
        modal = ActionConfigModal(
            requester_id=40, current_config=GuildChatbotConfig(guild_id=10),
            on_submit_config=save, check_authorized=authorized,
        )
        interaction = _interaction()

        await modal.on_submit(interaction)

        authorized.assert_awaited_once_with(interaction)
        save.assert_not_awaited()

    async def test_modal_submission_changes_actions_without_rewriting_conversation(self):
        from cogs.chatbot.views import ActionConfigModal

        initial = GuildChatbotConfig(guild_id=10, enabled=True, channel_ids=(20,))
        save = AsyncMock()
        modal = ActionConfigModal(
            requester_id=40, current_config=initial,
            on_submit_config=save, check_authorized=AsyncMock(return_value=True),
        )
        for group, value in ((modal.actions_group, "on"), (modal.audio_group, "off"),
                             (modal.voice_group, "on"), (modal.moderation_group, "off")):
            group._value = value
        modal.staff_roles._values = [SimpleNamespace(id=80), SimpleNamespace(id=90)]
        interaction = _interaction()

        await modal.on_submit(interaction)

        submitted_interaction, config = save.await_args.args
        self.assertIs(submitted_interaction, interaction)
        self.assertEqual(config, dataclasses.replace(initial,
            actions_enabled=True, audio_actions_enabled=False, voice_actions_enabled=True,
            moderation_actions_enabled=False, action_staff_role_ids=(80, 90),
        ))

    async def test_second_configuration_button_opens_action_form(self):
        from cogs.chatbot.views import ActionConfigModal, EditConfigView

        save_actions = AsyncMock()
        view = EditConfigView(
            requester_id=40, current_config=GuildChatbotConfig(guild_id=10),
            on_submit_config=AsyncMock(), on_submit_actions=save_actions,
            check_authorized=AsyncMock(return_value=True),
        )
        self.assertEqual([button.label for button in view.children], [
            "Editar configuração", "Configurar ações", "Configurar áudios",
        ])
        interaction = _interaction()

        await view.children[1].callback(interaction)

        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, ActionConfigModal)
        self.assertIs(modal._on_submit_config, save_actions)

    async def test_action_button_rejects_another_user(self):
        from cogs.chatbot.views import EditConfigView

        authorized = AsyncMock(return_value=True)
        view = EditConfigView(
            requester_id=40, current_config=GuildChatbotConfig(guild_id=10),
            on_submit_config=AsyncMock(), check_authorized=authorized,
        )
        interaction = _interaction(user_id=99)

        await view.children[1].callback(interaction)

        authorized.assert_not_awaited()
        interaction.response.send_modal.assert_not_awaited()


class ActionConfigCommandTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self):
        from cogs.chatbot.commands import ChatbotCommandsMixin

        cog = ChatbotCommandsMixin()
        cog._config = SimpleNamespace(
            get_config=AsyncMock(return_value=GuildChatbotConfig(guild_id=10)),
            save_action_config=AsyncMock(return_value=GuildChatbotConfig(guild_id=10)),
            save_config=AsyncMock(),
        )
        cog._memory = object()
        return cog

    async def test_staff_permission_loss_after_open_prevents_action_save(self):
        cog = self._cog()
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(
            side_effect=[True, True, False],
        )) as staff_check:
            command = _interaction()
            await cog._do_configurar(command)
            view = command.followup.send.await_args.kwargs["view"]
            click = _interaction()
            await view.children[1].callback(click)
            modal = click.response.send_modal.await_args.args[0]

            await modal.on_submit(_interaction())

        self.assertEqual(staff_check.await_count, 3)
        cog._config.save_action_config.assert_not_awaited()

    async def test_handler_rejects_unauthorized_staff_and_cross_guild_form(self):
        cog = self._cog()
        settings = GuildChatbotConfig(guild_id=10, actions_enabled=False)
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=False)):
            await cog._handle_actions_modal(_interaction(), settings)
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
            await cog._handle_actions_modal(_interaction(guild_id=99), settings)

        cog._config.save_action_config.assert_not_awaited()

    async def test_authorized_handler_writes_only_actions_for_current_guild(self):
        cog = self._cog()
        settings = GuildChatbotConfig(
            guild_id=10, enabled=True, channel_ids=(20,), actions_enabled=True,
            audio_actions_enabled=False, voice_actions_enabled=True,
            moderation_actions_enabled=False, action_staff_role_ids=(80, 90),
        )
        with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
            await cog._handle_actions_modal(_interaction(), settings)

        cog._config.save_action_config.assert_awaited_once_with(**_action_settings())
        cog._config.save_config.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
