"""A CD reopen retires its old panel and visibly sends a fresh panel.

Production command methods execute with explicit Discord transport doubles.
This exercises lifecycle and failure handling without claiming API validation.
The filename replaces the obsolete v9.2 acknowledgment tests on installed bots.
"""
import asyncio
import logging
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import weakref

try:
    from test_cd_panel_reuse_runtime import extract, DISCORD, View
except ModuleNotFoundError:
    from tests.test_cd_panel_reuse_runtime import extract, DISCORD, View


class CDReopenReplacementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        namespace = {'__package__': 'cogs.tts', 'asyncio': asyncio, 'time': time,
                     'weakref': weakref, 'discord': DISCORD,
                     'logger': logging.getLogger('test-cd-reopen-replacement')}
        methods = extract('cogs/tts/cog.py',
                          ['_send_cd_panel', '_delete_prefix_panel', '_prefix_panel_key'],
                          namespace)
        self.cog = type('ProductionCommand', (), methods)()
        self.cog._get_db = lambda: object()
        self.cog._check_prefix_panel_cooldown = AsyncMock(return_value=False)
        self.cog._get_panel_prefix_hint = AsyncMock(return_value='`_cd`')
        self.cog._active_prefix_panels = {}
        self.cog._public_panel_states = {}
        self.cog._prefix_panel_cooldowns = {}
        self.sent_panels, self.notices, self.views, self.events = [], [], [], []
        self.channel = SimpleNamespace(id=30, send=AsyncMock(side_effect=self.send))
        self.message = SimpleNamespace(id=90, guild=SimpleNamespace(id=20),
                                       author=SimpleNamespace(id=10), channel=self.channel)
        self.key = (20, 10, 'cd')
        View.generation = 0
        module = ModuleType('cogs.tts.interface.painel_canto')
        module.CDPanelView = self.construct
        replacement = patch.dict(sys.modules, {'cogs.tts.interface.painel_canto': module})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.refresh_error = None

    def construct(self, *args, **kwargs):
        view = View(*args, **kwargs)
        if self.refresh_error is not None:
            view.refresh.side_effect = self.refresh_error
        self.views.append(view)
        return view

    async def send(self, *args, **kwargs):
        if 'view' not in kwargs:
            self.notices.append((args, kwargs))
            return SimpleNamespace(id=901)
        self.events.append('send')
        panel = SimpleNamespace(id=100 + len(self.sent_panels), channel=self.message.channel,
                                delete=AsyncMock(), view=kwargs['view'])
        self.sent_panels.append(panel)
        return panel

    async def test_reopen_stops_and_deletes_old_panel_before_sending_fresh_panel(self):
        await self.cog._send_cd_panel(self.message)
        previous_message = self.sent_panels[0]
        previous = self.views[0]

        async def deleted():
            self.assertTrue(previous.finished, 'Controls must be retired before Discord deletion')
            self.assertEqual(previous.expires_at_monotonic, 0)
            self.assertNotIn(self.key, self.cog._active_prefix_panels)
            self.assertNotIn(previous_message.id, self.cog._public_panel_states)
            self.events.append('delete')

        previous_message.delete.side_effect = deleted
        self.events.clear()
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(self.events, ['delete', 'send'])
        self.assertEqual(len(self.sent_panels), 2)
        self.assertFalse(self.notices, 'Reopening must send a panel, never an Abrir painel link')
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent_panels[1])
        self.assertIs(self.cog._cd_panel_views[self.key], self.views[1])
        previous._edit_panel.assert_not_awaited()

    async def test_already_missing_old_message_does_not_block_replacement(self):
        await self.cog._send_cd_panel(self.message)
        previous_message = self.sent_panels[0]
        previous_message.delete.side_effect = RuntimeError('404 old message already deleted')
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        previous_message.delete.assert_awaited_once()
        self.assertTrue(self.views[0].finished)
        self.assertEqual(self.views[0].expires_at_monotonic, 0)
        self.assertEqual(len(self.sent_panels), 2)
        self.assertNotIn(previous_message.id, self.cog._public_panel_states)
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent_panels[1])

    async def test_cooldown_keeps_previous_panel_available_without_creating_replacement(self):
        await self.cog._send_cd_panel(self.message)
        previous = self.views[0]
        self.cog._check_prefix_panel_cooldown.return_value = True
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.views), 1)
        self.assertEqual(len(self.sent_panels), 1)
        self.sent_panels[0].delete.assert_not_awaited()
        previous.stop.assert_not_called()
        self.assertIs(self.cog._cd_panel_views[self.key], previous)

    async def test_reopen_in_another_channel_replaces_same_user_previous_panel(self):
        await self.cog._send_cd_panel(self.message)
        previous_message = self.sent_panels[0]
        next_channel = SimpleNamespace(id=31, send=AsyncMock(side_effect=self.send))
        self.message = SimpleNamespace(id=91, guild=self.message.guild,
                                       author=self.message.author, channel=next_channel)
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        previous_message.delete.assert_awaited_once()
        next_channel.send.assert_awaited_once()
        self.assertIs(self.cog._active_prefix_panels[self.key].channel, next_channel)
        self.assertTrue(self.views[0].finished)

    async def test_settings_failure_keeps_previous_panel_live_until_replacement_is_ready(self):
        await self.cog._send_cd_panel(self.message)
        previous = self.views[0]
        self.refresh_error = RuntimeError('private configuration must not be logged')
        with self.assertLogs('test-cd-reopen-replacement', level='WARNING') as records:
            self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.sent_panels), 1)
        self.sent_panels[0].delete.assert_not_awaited()
        self.assertFalse(previous.finished)
        self.assertIs(self.cog._cd_panel_views[self.key], previous)
        self.assertTrue(self.views[1].finished)
        self.assertEqual(len(self.notices), 1)
        self.assertNotIn('private configuration', '\n'.join(records.output))
        self.assertFalse(self.cog._cd_panel_opening)

    async def test_other_users_previous_panel_is_preserved_on_reopen(self):
        await self.cog._send_cd_panel(self.message)
        previous_message = self.sent_panels[0]
        other = SimpleNamespace(id=91, guild=self.message.guild,
                                author=SimpleNamespace(id=11), channel=self.channel)
        await self.cog._send_cd_panel(other)
        other_panel = self.sent_panels[1]
        await self.cog._send_cd_panel(self.message)
        previous_message.delete.assert_awaited_once()
        other_panel.delete.assert_not_awaited()
        self.assertIs(self.cog._active_prefix_panels[(20, 11, 'cd')], other_panel)
        self.assertFalse(self.views[1].finished)
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent_panels[2])


if __name__ == '__main__':
    unittest.main()
