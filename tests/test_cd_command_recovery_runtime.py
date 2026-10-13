"""Exercise production CD command deadlines with stalled transport doubles.

No Discord SDK or external API is involved. Only the production deadline clock
is accelerated so a never-completing await can be reproduced without a long run.
"""
import asyncio
import logging
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import weakref

try:
    from test_cd_panel_reuse_runtime import extract, DISCORD, View
except ModuleNotFoundError:
    from tests.test_cd_panel_reuse_runtime import extract, DISCORD, View


class CDCommandRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.timeouts = []

        async def accelerated_wait(awaitable, timeout):
            self.timeouts.append(timeout)
            return await asyncio.wait_for(awaitable, min(0.05, timeout))

        clock = SimpleNamespace(wait_for=accelerated_wait, CancelledError=asyncio.CancelledError)
        namespace = {'__package__': 'cogs.tts', 'asyncio': clock, 'time': time,
                     'weakref': weakref, 'discord': DISCORD,
                     'logger': logging.getLogger('test-cd-command-recovery')}
        methods = extract('cogs/tts/cog.py', ['_send_cd_panel', '_delete_prefix_panel', '_prefix_panel_key'], namespace)
        self.cog = type('ProductionCommand', (), methods)()
        self.cog._get_db = lambda: object()
        self.cog._make_embed = lambda *args, **kwargs: SimpleNamespace(title=args[0])
        self.cog._check_prefix_panel_cooldown = AsyncMock(return_value=False)
        self.cog._get_panel_prefix_hint = AsyncMock(return_value='`!cd`')
        self.cog._active_prefix_panels = {}
        self.cog._public_panel_states = {}
        self.cog._prefix_panel_cooldowns = {(20, 10, 'cd'): 999999999.0}
        self.key = (20, 10, 'cd')
        self.notices, self.sent, self.views = [], [], []
        self.channel = SimpleNamespace(id=30, send=AsyncMock(side_effect=self.send))
        self.message = SimpleNamespace(guild=SimpleNamespace(id=20), author=SimpleNamespace(id=10),
                                       channel=self.channel)
        View.generation = 0
        module = ModuleType('cogs.tts.interface.painel_canto')
        module.CDPanelView = self.construct
        self.modules = patch.dict(sys.modules, {'cogs.tts.interface.painel_canto': module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.refresh_stalled = False
        self.upload = None

    def construct(self, *args, **kwargs):
        view = View(*args, **kwargs)
        if self.refresh_stalled:
            view.refresh.side_effect = self.never
        if self.upload is not None:
            view.panel_files.return_value = [self.upload]
        self.views.append(view)
        return view

    async def never(self, *args, **kwargs):
        await asyncio.Event().wait()

    async def send(self, *args, **kwargs):
        if 'view' not in kwargs:
            self.notices.append(args[0] if args else kwargs.get('embed'))
            return SimpleNamespace(id=900)
        message = SimpleNamespace(id=100 + len(self.sent), channel=self.channel,
                                  delete=AsyncMock(), view=kwargs['view'])
        self.sent.append(message)
        return message

    async def test_stalled_refresh_returns_notice_releases_busy_state_and_retries_immediately(self):
        self.refresh_stalled = True
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.views), 1)
        self.assertTrue(self.views[0].finished)
        self.assertIn('`!cd`', self.notices[0])
        self.assertNotIn(self.key, self.cog._cd_panel_opening)
        self.assertNotIn(self.key, self.cog._prefix_panel_cooldowns)
        self.refresh_stalled = False
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.sent), 1)
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent[0])

    async def test_stalled_send_closes_upload_preserves_old_panel_and_clears_busy_state(self):
        old = SimpleNamespace(id=99, delete=AsyncMock())
        self.cog._active_prefix_panels[self.key] = old
        self.cog._public_panel_states[old.id] = {'panel_kind': 'cd', 'owner_id': 10}
        self.upload = SimpleNamespace(close=Mock())

        async def stalled(*args, **kwargs):
            if 'view' in kwargs:
                return await self.never()
            return await self.send(*args, **kwargs)

        self.channel.send.side_effect = stalled
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.upload.close.assert_called_once()
        self.assertIs(self.cog._active_prefix_panels[self.key], old)
        old.delete.assert_not_awaited()
        self.assertIn(old.id, self.cog._public_panel_states)
        self.assertTrue(self.views[0].finished)
        self.assertFalse(self.cog._cd_panel_opening)

    async def test_stalled_old_delete_still_registers_confirmed_new_panel(self):
        old = SimpleNamespace(id=99, delete=AsyncMock(side_effect=self.never))
        self.cog._active_prefix_panels[self.key] = old
        self.cog._public_panel_states[old.id] = {'panel_kind': 'cd', 'owner_id': 10}
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent[0])
        self.assertIs(self.cog._cd_panel_views[self.key], self.views[0])
        self.assertFalse(self.views[0].finished)
        self.assertNotIn(old.id, self.cog._public_panel_states)
        self.assertFalse(self.notices)

    async def test_stalled_reuse_edit_keeps_previous_panel_and_receives_recovery_notice(self):
        await self.cog._send_cd_panel(self.message)
        view = self.views[0]
        previous_expiry = view.expires_at_monotonic
        view._edit_panel.side_effect = self.never
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(view.finished)
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent[0])
        self.assertEqual(view.expires_at_monotonic, previous_expiry)
        self.assertIn('`!cd`', self.notices[-1])
        self.assertFalse(self.cog._cd_panel_opening)

    async def test_panel_expiring_during_edit_is_replaced_without_reactivation(self):
        await self.cog._send_cd_panel(self.message)
        view = self.views[0]

        async def expire_during_edit():
            view.expires_at_monotonic = 0
            return True

        view._edit_panel.side_effect = expire_during_edit
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(view.finished)
        self.assertEqual(view.expires_at_monotonic, 0)
        self.assertIs(self.cog._cd_panel_views[self.key], self.views[1])

    async def test_cancellation_during_old_delete_preserves_confirmed_new_panel(self):
        entered = asyncio.Event()

        async def pending_delete():
            entered.set()
            await self.never()

        old = SimpleNamespace(id=99, delete=AsyncMock(side_effect=pending_delete))
        self.cog._active_prefix_panels[self.key] = old
        self.cog._public_panel_states[old.id] = {'panel_kind': 'cd', 'owner_id': 10}
        task = asyncio.create_task(self.cog._send_cd_panel(self.message))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent[0])
        self.assertIs(self.cog._cd_panel_views[self.key], self.views[0])
        self.assertFalse(self.views[0].finished)
        self.assertFalse(self.cog._cd_panel_opening)

    async def test_concurrent_same_user_gets_busy_reply_without_duplicate_open(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pending(*args, **kwargs):
            if 'view' in kwargs:
                entered.set()
                await release.wait()
            return await self.send(*args, **kwargs)

        self.channel.send.side_effect = pending
        first = asyncio.create_task(self.cog._send_cd_panel(self.message))
        await entered.wait()
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertEqual(len(self.views), 1)
        self.assertIn('já está sendo aberto', self.notices[0])
        release.set()
        self.assertTrue(await first)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.cog._cd_panel_opening)

    async def test_burst_while_opening_shares_one_busy_notice_and_cleans_reservation(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pending(*args, **kwargs):
            if 'view' in kwargs:
                entered.set()
                await release.wait()
            return await self.send(*args, **kwargs)

        self.channel.send.side_effect = pending
        first = asyncio.create_task(self.cog._send_cd_panel(self.message))
        await entered.wait()
        replies = await asyncio.gather(*(self.cog._send_cd_panel(self.message) for _ in range(6)))
        self.assertEqual(replies, [True] * 6)
        self.assertEqual(len(self.views), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn('já está sendo aberto', self.notices[0])
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertIn(self.key, self.cog._cd_panel_busy_notices)
        release.set()
        self.assertTrue(await first)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.cog._cd_panel_opening)
        self.assertNotIn(self.key, self.cog._cd_panel_busy_notices)

    async def test_opening_one_user_does_not_block_another(self):
        self.cog._cd_panel_opening = {self.key}
        other = SimpleNamespace(guild=self.message.guild, author=SimpleNamespace(id=11), channel=self.channel)
        self.assertTrue(await self.cog._send_cd_panel(other))
        self.assertIn((20, 11, 'cd'), self.cog._active_prefix_panels)
        self.assertEqual(self.cog._cd_panel_opening, {self.key})
        self.assertFalse(self.notices)

    async def test_cancelled_open_releases_busy_marker_and_closes_upload(self):
        entered = asyncio.Event()
        self.upload = SimpleNamespace(close=Mock())

        async def pending(*args, **kwargs):
            entered.set()
            return await self.never()

        self.channel.send.side_effect = pending
        task = asyncio.create_task(self.cog._send_cd_panel(self.message))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.cog._cd_panel_opening)
        self.assertNotIn(self.key, self.cog._prefix_panel_cooldowns)
        self.upload.close.assert_called_once()
        self.assertTrue(self.views[0].finished)

    async def test_emoji_followup_timeout_does_not_discard_accepted_panel(self):
        async def send_and_finish_emojis(*args, **kwargs):
            if 'view' in kwargs:
                View.generation += 1
                kwargs['view']._edit_panel.side_effect = self.never
            return await self.send(*args, **kwargs)

        self.channel.send.side_effect = send_and_finish_emojis
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertIs(self.cog._active_prefix_panels[self.key], self.sent[0])
        self.assertFalse(self.views[0].finished)
        self.assertFalse(self.notices)
        self.assertFalse(self.cog._cd_panel_opening)

    async def test_notice_and_prefix_hint_timeouts_are_also_bounded(self):
        self.refresh_stalled = True
        self.cog._get_panel_prefix_hint.side_effect = self.never
        self.channel.send.side_effect = self.never
        self.assertTrue(await self.cog._send_cd_panel(self.message))
        self.assertFalse(self.cog._cd_panel_opening)
        self.assertNotIn(self.key, self.cog._prefix_panel_cooldowns)
        self.assertTrue(all(0 < timeout <= 20 for timeout in self.timeouts))


if __name__ == '__main__':
    unittest.main()
