"""Regression tests for global Edge cooldown and per-server messages."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.tts.edge_rate_limit import EdgeRateLimitPolicy


class EdgeRateLimitPolicyTests(unittest.TestCase):
    def test_progressive_delays_only_after_failed_recovery_probe(self):
        policy = EdgeRateLimitPolicy()
        self.assertEqual(policy.failed_429(now=100), (90, True))
        self.assertTrue(policy.should_bypass(now=100))
        self.assertEqual(policy.failed_429(now=101), (90, False))
        self.assertEqual(policy.level, 1)
        self.assertFalse(policy.should_bypass(now=190))  # One probe permitted.
        self.assertTrue(policy.should_bypass(now=190))
        self.assertEqual(policy.failed_429(now=192), (180, True))
        self.assertEqual(policy.failed_429(now=193), (180, False))
        for at, delay in ((372, 360), (732, 720), (1452, 1200), (2652, 1200)):
            self.assertFalse(policy.should_bypass(now=at))
            self.assertEqual(policy.failed_429(now=at), (delay, True))
        self.assertEqual(policy.level, 5)

    def test_recovery_probe_exclusive_and_success_resets(self):
        policy = EdgeRateLimitPolicy()
        policy.failed_429(now=10)
        policy.success()  # An older already-connected request is not a probe.
        self.assertEqual(policy.level, 1)
        self.assertTrue(policy.should_bypass(now=99))
        self.assertFalse(policy.should_bypass(now=100))
        self.assertTrue(policy.should_bypass(now=100))
        policy.success()
        self.assertEqual(policy.level, 0)
        self.assertFalse(policy.should_bypass(now=100))
        self.assertEqual(policy.guild_notices, {})

    def test_other_error_unlocks_probe(self):
        policy = EdgeRateLimitPolicy()
        policy.failed_429(now=10)
        self.assertFalse(policy.should_bypass(now=100))
        policy.failed_other()
        self.assertFalse(policy.should_bypass(now=100))

    def test_each_guild_notices_once_then_3_hour_limit_unless_escalation(self):
        policy = EdgeRateLimitPolicy()
        policy.failed_429(now=20)
        self.assertEqual(policy.reserve_notice(11, now=20), "initial")
        self.assertIsNone(policy.reserve_notice(11, now=60))
        self.assertEqual(policy.reserve_notice(22, now=60), "initial")
        self.assertFalse(policy.should_bypass(now=110))
        policy.failed_429(now=110)
        self.assertEqual(policy.reserve_notice(11, now=111), "followup")
        self.assertIsNone(policy.reserve_notice(11, now=112))
        self.assertEqual(policy.reserve_notice(11, now=111 + 10800), "followup")
        self.assertIsNone(policy.reserve_notice(11, now=112 + 10800))

    def test_first_delay_can_be_configured(self):
        policy = EdgeRateLimitPolicy(first_delay=120)
        self.assertEqual(policy.failed_429(now=0), (120, True))


class EdgeRateLimitIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This test module is also runnable by itself without real Discord.
        from test_tts_helpers import tts_audio
        self.a = tts_audio
        self.messages = []
        async def send(content, **kwargs):
            self.messages.append((content, kwargs))
        self.channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        self.guild = SimpleNamespace(get_channel=lambda channel_id: self.channel if channel_id == 5 else None)
        self.bot = SimpleNamespace(get_guild=lambda guild_id: self.guild, get_channel=lambda channel_id: self.channel)
        self.cog = tts_audio.TTSAudioMixin()
        self.cog.bot = self.bot
        self.item = tts_audio.QueueItem(guild_id=77, channel_id=6, author_id=8, text='teste', engine='edge',
                                       voice='pt-BR-FranciscaNeural', language='pt', rate='+0%', pitch='+0Hz', text_channel_id=5)
        self.allowed = patch.object(tts_audio.discord, 'AllowedMentions', create=True)
        self.allowed.start().none = staticmethod(lambda: None)

    async def asyncTearDown(self):
        self.allowed.stop()
        tasks = list(self.cog._get_tts_background_tasks())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def flush(self):
        await asyncio.sleep(0)
        tasks = list(self.cog._get_tts_background_tasks())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_notice_lifetime_and_per_server_dedup(self):
        policy = self.cog._edge_rate_limit_policy()
        policy.failed_429()
        self.cog._notify_edge_rate_limit(self.item)
        self.cog._notify_edge_rate_limit(self.item)
        await self.flush()
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(self.messages[0][0].startswith('-#'))
        self.assertEqual(self.messages[0][1]['delete_after'], 15)
        self.assertIn('90 segundos', self.messages[0][0])

        policy.retry_at = 0
        policy.probing = True
        policy.failed_429()
        self.cog._notify_edge_rate_limit(self.item)
        await self.flush()
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[1][1]['delete_after'], 10)

    async def test_other_error_never_sends_notice(self):
        self.cog._edge_rate_limit_policy().failed_other()
        self.cog._notify_edge_rate_limit(self.item)
        await self.flush()
        self.assertEqual(self.messages, [])
