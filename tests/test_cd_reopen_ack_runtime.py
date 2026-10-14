"""A prefix reopen must visibly answer while preserving its existing CD panel.

Production command methods are executed with explicit Discord transport doubles;
this checks reply intent and lifecycle, without claiming Discord API validation.
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


class CDReopenAcknowledgmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        namespace = {'__package__': 'cogs.tts', 'asyncio': asyncio, 'time': time,
                     'weakref': weakref, 'discord': DISCORD,
                     'logger': logging.getLogger('test-cd-reopen-ack')}
        self.namespace = namespace
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
        self.sent_panels, self.replies = [], []
        self.channel = SimpleNamespace(id=30, send=AsyncMock(side_effect=self.send))
        self.message = SimpleNamespace(id=90, guild=SimpleNamespace(id=20),
                                       author=SimpleNamespace(id=10), channel=self.channel)
        self.key = (20, 10, 'cd')
        module = ModuleType('cogs.tts.interface.painel_canto')
        module.CDPanelView = View
        replacement = patch.dict(sys.modules, {'cogs.tts.interface.painel_canto': module})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.reply_effect = None

    async def send(self, *args, **kwargs):
        if 'view' not in kwargs:
            self.replies.append((args, kwargs))
            if self.reply_effect is not None:
                await self.reply_effect()
            return SimpleNamespace(id=901)
        panel = SimpleNamespace(id=100 + len(self.sent_panels), channel=self.channel,
                                delete=AsyncMock(), view=kwargs['view'],
                                jump_url='https://discord.com/channels/20/30/100')
        self.sent_panels.append(panel)
        return panel

    async def test_reopening_visibly_replies_to_new_command_and_links_existing_panel(self):
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        view = self.cog._cd_panel_views[self.key]
        reopen = SimpleNamespace(id=91, guild=self.message.guild,
                                 author=self.message.author, channel=self.channel)
        self.assertTrue(await self.cog._send_cd_panel(reopen))
        self.assertEqual(len(self.sent_panels), 1)
        self.assertEqual(len(self.replies), 1, 'Editing an old panel is not a visible command reply')
        content = self.replies[0][0][0]
        self.assertIn(self.sent_panels[0].jump_url, content)
        self.assertIs(self.replies[0][1]['reference'], reopen)
        self.assertFalse(self.replies[0][1]['mention_author'])
        self.assertIs(self.cog._cd_panel_views[self.key], view)
        self.sent_panels[0].delete.assert_not_awaited()

    async def test_failed_acknowledgment_does_not_report_successful_reuse_as_failed_panel(self):
        await self.cog._send_cd_panel(self.message)
        view = self.cog._cd_panel_views[self.key]

        async def failure():
            raise RuntimeError('private token must not be included in logs')

        self.reply_effect = failure
        with self.assertLogs('test-cd-reopen-ack', level='WARNING') as records:
            self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.replies), 1)
        self.assertIs(self.cog._cd_panel_views[self.key], view)
        self.assertFalse(self.cog._cd_panel_opening)
        self.assertNotIn('private token', '\n'.join(records.output))
        self.assertNotIn('não foi possível abrir', '\n'.join(records.output))

    async def test_reuse_acknowledgment_does_not_reactivate_panel_expiring_during_reply(self):
        await self.cog._send_cd_panel(self.message)
        view = self.cog._cd_panel_views[self.key]

        async def expire():
            view.expires_at_monotonic = 0
            view.stop()

        self.reply_effect = expire
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.replies), 1)
        self.assertTrue(view.finished)
        self.assertEqual(view.expires_at_monotonic, 0)
        self.assertFalse(self.cog._cd_panel_opening)

    async def test_stalled_acknowledgment_is_bounded_and_keeps_panel_reusable(self):
        await self.cog._send_cd_panel(self.message)
        view = self.cog._cd_panel_views[self.key]
        received_timeouts = []

        async def accelerated_wait(awaitable, timeout):
            received_timeouts.append(timeout)
            return await asyncio.wait_for(awaitable, min(0.05, timeout))

        async def stalled():
            await asyncio.Event().wait()

        self.namespace['asyncio'] = SimpleNamespace(wait_for=accelerated_wait,
                                                  CancelledError=asyncio.CancelledError)
        self.reply_effect = stalled
        with self.assertLogs('test-cd-reopen-ack', level='WARNING'):
            self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(received_timeouts[-1], 3.0)
        self.assertEqual(len(self.replies), 1)
        self.assertIs(self.cog._cd_panel_views[self.key], view)
        self.assertFalse(view.finished)
        self.assertFalse(self.cog._cd_panel_opening)


if __name__ == '__main__':
    unittest.main()
