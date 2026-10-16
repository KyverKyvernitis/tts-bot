"""Public Edge paths must exhaust local origins before Phone and gTTS."""
from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_tts_helpers import tts_audio
from test_tts_edge_phone_recovery import _Probe, stream_runtime
from test_edge_sources import PRIMARY, SECONDARY, too_many_requests


class MultiSourcePublicFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = []
        self.config_patch = patch.object(tts_audio, 'TTS_EDGE_SOURCE_IPS', PRIMARY+','+SECONDARY)
        self.config_patch.start()
        self.runtime = stream_runtime()
        self.directory = self.runtime.__enter__()
        self.cog = _Probe()
        self.state = tts_audio.GuildTTSState(queue=asyncio.Queue())
        self.item = tts_audio.QueueItem(guild_id=77, channel_id=9, author_id=6,
            text='Oi, bom dia', engine='edge', voice='pt-BR-FranciscaNeural',
            language='pt', rate='+5%', pitch='-3Hz', text_channel_id=11)
        self.guilds = {}
        for guild in range(1, 101):
            lease = self.cog._edge_source_pool().reserve(guild)
            self.guilds.setdefault(lease.source_ip, guild)
            self.cog._edge_source_pool().release(lease)
        self.item.guild_id = self.guilds[PRIMARY]
        self.calls = []
        self.closed = []
        self.fail_ips = set()
        self.partial_failure_ips = set()
        self.late_failure_ips = set()
        self.stall_ips = set()
        self.stream_started = asyncio.Event()
        self.failure_release = asyncio.Event()
        self.tail = asyncio.Event()
        owner = self

        class Speech:
            def __init__(self, **kwargs):
                self.ip = kwargs['connector'].source_ip
                owner.calls.append(self.ip)
            async def stream(self):
                owner.stream_started.set()
                if self.ip in owner.stall_ips:
                    await asyncio.Event().wait()
                if self.ip in owner.late_failure_ips:
                    yield {'type':'audio', 'data':b'bad-prefix'*512}
                    await owner.failure_release.wait()
                    raise too_many_requests()
                if self.ip in owner.partial_failure_ips:
                    yield {'type':'audio', 'data':b'bad-prefix'}
                    raise too_many_requests()
                if self.ip in owner.fail_ips:
                    raise too_many_requests()
                yield {'type':'audio', 'data':b'G'*4096}
                await owner.tail.wait()
                yield {'type':'audio', 'data':b'T'*1024}
            async def save(self, path):
                if self.ip in owner.fail_ips:
                    raise too_many_requests()
                with open(path, 'wb') as audio:
                    audio.write(b'G'*4096)

        @asynccontextmanager
        async def no_network(factory, source_ip, **kwargs):
            try:
                yield factory(connector=SimpleNamespace(source_ip=source_ip), **kwargs)
            finally:
                self.closed.append(source_ip)

        for patcher in (patch.object(tts_audio.edge_tts, 'Communicate', Speech),
                        patch.object(tts_audio, 'bound_communicate', no_network)):
            patcher.start()
            self.patches.append(patcher)
        self.cog._store_in_cache = AsyncMock(side_effect=lambda state,item,path:path)
        self.cog._schedule_worker_turbo_cache_store = Mock()
        self.cog._notify_edge_rate_limit = Mock()

    async def asyncTearDown(self):
        self.tail.set()
        for handle in list(self.cog._get_edge_stream_handles().values()):
            await self.cog._finalize_edge_stream(handle, cancel=True)
        self.cog._shutdown_tts_runtime()
        tasks = list(self.cog._get_tts_background_tasks())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        self.cog._audio_leases().close()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.runtime.__exit__(None, None, None)
        self.config_patch.stop()

    async def test_primary_429_recovers_secondary_stream_without_phone_or_gtts(self):
        self.fail_ips = {PRIMARY}
        self.cog._try_edge_phone_stream = AsyncMock(side_effect=AssertionError('local Edge still available'))
        self.cog._resolve_edge_gtts_fallback = AsyncMock(side_effect=AssertionError('local Edge still available'))
        path, cleanup = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        handle = self.cog._edge_stream_handle_for_path(path)
        self.assertTrue(cleanup)
        self.assertEqual(self.calls, [PRIMARY, SECONDARY])
        self.assertEqual(self.item._tts_actual_engine, 'edge')
        self.assertEqual(self.item._edge_sources_attempted, {PRIMARY, SECONDARY})
        self.assertEqual(bytes(handle.shared_job.buffer._memory), b'G'*4096)
        self.assertFalse(handle.shared_job.task.done(), 'stream must hand off before synthesis completes')
        self.cog._try_edge_phone_stream.assert_not_awaited()
        self.cog._resolve_edge_gtts_fallback.assert_not_awaited()
        self.cog._notify_edge_rate_limit.assert_not_called()
        self.assertEqual(self.cog._edge_source_pool().states[PRIMARY].policy.level, 1)

    async def test_partial_failed_origin_is_not_mixed_into_recovery_audio(self):
        self.partial_failure_ips = {PRIMARY}
        self.cog._try_edge_phone_stream = AsyncMock(side_effect=AssertionError('secondary still available'))
        path, _ = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        handle = self.cog._edge_stream_handle_for_path(path)
        self.assertEqual(self.calls, [PRIMARY, SECONDARY])
        self.assertEqual(bytes(handle.shared_job.buffer._memory), b'G'*4096)
        self.assertNotIn(b'bad-prefix', bytes(handle.shared_job.buffer._memory))

    async def test_all_local_streams_fail_then_phone_success_avoids_gtts(self):
        self.fail_ips = {PRIMARY, SECONDARY}
        async def phone(job):
            self.assertEqual(self.calls, [PRIMARY, SECONDARY])
            self.calls.append('phone')
            job.route = 'worker'
            job.actual_engine = 'edge'
            await self.cog._append_shared_audio(job, b'P'*4096)
            await self.tail.wait()
        self.cog._produce_worker_stream_job = AsyncMock(side_effect=phone)
        self.cog._resolve_edge_gtts_fallback = AsyncMock(side_effect=AssertionError('phone Edge succeeded'))
        path, _ = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(self.calls, [PRIMARY, SECONDARY, 'phone'])
        self.assertEqual(self.item._tts_audio_route, 'worker')
        self.assertEqual(self.item._tts_actual_engine, 'edge')
        self.cog._resolve_edge_gtts_fallback.assert_not_awaited()
        self.cog._notify_edge_rate_limit.assert_not_called()

    async def test_all_local_streams_and_phone_fail_before_gtts_is_allowed(self):
        self.fail_ips = {PRIMARY, SECONDARY}
        async def phone(job):
            self.calls.append('phone')
            job.route = 'worker'
            raise RuntimeError('Phone Edge unavailable')
        async def gtts(*args, **kwargs):
            self.assertEqual(self.calls, [PRIMARY, SECONDARY, 'phone'])
            self.calls.append('gtts')
            return '/tmp/gtts.mp3', True
        self.cog._produce_worker_stream_job = AsyncMock(side_effect=phone)
        self.cog._resolve_edge_gtts_fallback = AsyncMock(side_effect=gtts)
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(result, ('/tmp/gtts.mp3', True))
        self.assertEqual(self.calls, [PRIMARY, SECONDARY, 'phone', 'gtts'])
        self.cog._produce_worker_stream_job.assert_awaited_once()
        self.cog._resolve_edge_gtts_fallback.assert_awaited_once()

    async def test_complete_file_public_path_recovers_secondary_without_phone(self):
        self.fail_ips = {PRIMARY}
        self.tail.set()
        self.cog._try_edge_phone_file = AsyncMock(side_effect=AssertionError('secondary still available'))
        self.cog._generate_gtts_file_with_priority = AsyncMock(side_effect=AssertionError('secondary still available'))
        path, _ = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=False)
        self.assertEqual(self.calls, [PRIMARY, SECONDARY])
        with open(path, 'rb') as audio:
            self.assertEqual(audio.read(), b'G'*4096)
        self.cog._try_edge_phone_file.assert_not_awaited()
        self.cog._generate_gtts_file_with_priority.assert_not_awaited()
        self.cog._notify_edge_rate_limit.assert_not_called()

    async def test_direct_file_generator_binds_each_attempt(self):
        self.fail_ips = {PRIMARY}
        self.cog._try_edge_phone_file = AsyncMock(side_effect=AssertionError('secondary still available'))
        path = await self.cog._generate_audio_file(self.item)
        self.assertEqual(self.calls, [PRIMARY, SECONDARY])
        with open(path, 'rb') as audio:
            self.assertEqual(audio.read(), b'G'*4096)
        self.cog._try_edge_phone_file.assert_not_awaited()
        self.cog._notify_edge_rate_limit.assert_not_called()

    async def test_active_jobs_separate_origins_but_share_final_audio_cache_identity(self):
        first = self.item
        first._edge_sources_attempted = set()
        other = replace(self.item, guild_id=self.guilds[SECONDARY])
        same_origin = replace(self.item)
        self.assertEqual(self.cog._cache_key(first), self.cog._cache_key(other))
        jobs = []
        try:
            one = self.cog._acquire_shared_job(self.state, first, store_in_cache=False)
            jobs.append(one)
            two = self.cog._acquire_shared_job(self.state, other, store_in_cache=False)
            jobs.append(two)
            shared = self.cog._acquire_shared_job(self.state, same_origin, store_in_cache=False)
            jobs.append(shared)
            self.assertIs(shared, one)
            self.assertIsNot(one, two)
            self.assertIn(':edge-source:'+PRIMARY, one.key)
            self.assertIn(':edge-source:'+SECONDARY, two.key)
            self.assertIsNot(one.item, first)
            self.assertIsNot(one.item._edge_sources_attempted, first._edge_sources_attempted)
            self.assertIsNone(first._edge_source_lease)
            self.assertIsNone(same_origin._edge_source_lease)
            first._edge_source_ip = SECONDARY
            first._edge_sources_attempted.add('consumer-only-marker')
            self.assertEqual(one.item._edge_source_ip, PRIMARY)
            self.assertNotIn('consumer-only-marker', one.item._edge_sources_attempted)
        finally:
            for job in jobs:
                self.cog._release_shared_job(job)
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)

    async def test_invalid_explicit_binding_uses_phone_without_unbound_edge(self):
        with patch.object(tts_audio, 'TTS_EDGE_SOURCE_IPS', 'not-an-ip'):
            self.cog._tts_edge_source_pool = None
            self.cog._try_edge_phone_stream = AsyncMock(return_value=('/tmp/phone.fifo', True))
            self.cog._resolve_edge_gtts_fallback = AsyncMock(side_effect=AssertionError('phone worked'))
            result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
            self.assertEqual(result, ('/tmp/phone.fifo', True))
            self.assertEqual(self.calls, [])
            self.assertTrue(self.cog._edge_source_pool().config_error)
            self.cog._try_edge_phone_stream.assert_awaited_once()


    async def test_cancel_before_source_job_starts_releases_recovery_probe(self):
        pool = self.cog._edge_source_pool()
        first = pool.reserve(self.item.guild_id)
        pool.start(first)
        pool.failure(first, too_many_requests())
        policy = pool.states[PRIMARY].policy
        policy.retry_at = 0
        job = self.cog._acquire_shared_job(self.state, self.item, store_in_cache=False)
        lease = job.item._edge_source_lease
        self.assertTrue(lease.probe)
        self.assertTrue(policy.probing)
        self.cog._release_shared_job(job)
        await asyncio.gather(job.task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertFalse(policy.probing)
        self.assertTrue(lease.finished)
        self.assertEqual(self.calls, [])

    async def test_late_old_producer_merge_preserves_new_consumer_reservation(self):
        self.item._edge_sources_attempted = set()
        job = self.cog._acquire_shared_job(self.state, self.item, store_in_cache=False)
        try:
            old_item = job.item
            old_item._edge_sources_attempted.add(PRIMARY)
            self.item._edge_sources_attempted.add(PRIMARY)
            self.assertTrue(self.cog._edge_select_source(self.item))
            new_lease = self.item._edge_source_lease
            self.assertEqual(new_lease.source_ip, SECONDARY)
            self.cog._edge_source_merge(self.item, old_item)
            self.assertEqual(self.item._edge_source_ip, SECONDARY)
            self.assertIs(self.item._edge_source_lease, new_lease)
            self.assertEqual(old_item._edge_source_ip, PRIMARY)
        finally:
            self.cog._edge_source_release(self.item)
            self.cog._release_shared_job(job)
            await asyncio.gather(job.task, return_exceptions=True)

    async def test_actual_gtts_fallback_notices_share_three_hour_window_per_guild(self):
        messages = []
        async def send(content, **kwargs):
            messages.append(content)
        channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        self.cog.bot = SimpleNamespace(get_guild=lambda guild_id:guild, get_channel=lambda channel_id:channel,
                                       audio_router=None)
        self.cog._notify_edge_rate_limit = tts_audio.TTSAudioMixin._notify_edge_rate_limit.__get__(self.cog)
        gtts_path = os.path.join(self.directory, 'gtts-cached.mp3')
        with open(gtts_path, 'wb') as audio:
            audio.write(b'gTTS audio')
        self.cog._try_get_cached_path = lambda state,item: gtts_path if item.engine == 'gtts' else None
        policy = self.cog._edge_rate_limit_policy()
        self.cog._edge_source_pool().states[PRIMARY].policy.level = 1
        self.cog._edge_source_pool().states[SECONDARY].policy.level = 1
        async def fallback(item):
            await self.cog._resolve_edge_gtts_fallback(self.state, item, allow_stream=False)
            tasks = list(self.cog._get_tts_background_tasks())
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        with patch.object(tts_audio.discord, 'AllowedMentions', create=True) as mentions:
            mentions.none = lambda: None
            await fallback(self.item)
            await fallback(replace(self.item))
            self.assertEqual(len(messages), 1)
            await fallback(replace(self.item, guild_id=self.item.guild_id+1000))
            self.assertEqual(len(messages), 2)
            self.cog._edge_source_pool().states[SECONDARY].policy.level = 2
            await fallback(replace(self.item))
            await fallback(replace(self.item))
            self.assertEqual(len(messages), 3, 'one escalation may bypass the three-hour window')
            previous, level = policy.guild_notices[self.item.guild_id]
            policy.guild_notices[self.item.guild_id] = (previous-10801, level)
            await fallback(replace(self.item))
            self.assertEqual(len(messages), 4)

    async def test_failed_gtts_fallback_does_not_notify(self):
        self.cog._edge_source_pool().states[PRIMARY].policy.level = 1
        self.cog._shared_job_file = AsyncMock(side_effect=RuntimeError('gTTS unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'gTTS unavailable'):
            await self.cog._resolve_edge_gtts_fallback(self.state, self.item, allow_stream=False)
        self.cog._notify_edge_rate_limit.assert_not_called()


    async def test_late_stream_failure_recovers_other_source_as_fresh_complete_file(self):
        self.late_failure_ips = {PRIMARY}
        self.cog._try_edge_phone_file = AsyncMock(side_effect=AssertionError('secondary still available'))
        self.cog._generate_gtts_file_with_priority = AsyncMock(side_effect=AssertionError('secondary still available'))
        path, _ = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        handle = self.cog._edge_stream_handle_for_path(path)
        self.assertIn(b'bad-prefix', bytes(handle.shared_job.buffer._memory))
        self.failure_release.set()
        await asyncio.wait_for(handle.shared_job.task, 1)
        await asyncio.wait_for(handle.producer_task, 1)
        complete = await self.cog._tts_local_complete_file_after_stream_failure(self.item)
        self.assertEqual(self.calls, [PRIMARY, SECONDARY])
        with open(complete, 'rb') as audio:
            self.assertEqual(audio.read(), b'G'*4096)
        self.cog._try_edge_phone_file.assert_not_awaited()
        self.cog._generate_gtts_file_with_priority.assert_not_awaited()
        self.cog._notify_edge_rate_limit.assert_not_called()

    async def test_cancel_active_stream_releases_source_probe_and_connector(self):
        pool = self.cog._edge_source_pool()
        first = pool.reserve(self.item.guild_id)
        pool.start(first)
        pool.failure(first, too_many_requests())
        policy = pool.states[PRIMARY].policy
        policy.retry_at = 0
        self.stall_ips = {PRIMARY}
        task = asyncio.create_task(self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True))
        await asyncio.wait_for(self.stream_started.wait(), 1)
        self.assertTrue(policy.probing)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertFalse(policy.probing)
        self.assertEqual(self.closed, [PRIMARY])
        self.assertEqual(pool.states[PRIMARY].cancellations, 1)
        self.cog._notify_edge_rate_limit.assert_not_called()

    async def test_invalid_edge_source_config_preserves_teto_worker_path(self):
        self.cog._tts_agent_should_try_worker = lambda item:(True, 'teto_worker')
        self.cog._generate_tts_agent_worker_file = AsyncMock(return_value='/tmp/teto.wav')
        item = replace(self.item, engine='teto')
        with patch.object(tts_audio, 'TTS_EDGE_SOURCE_IPS', 'not-an-ip'), patch.object(
                self.cog, '_edge_source_pool', side_effect=AssertionError('Teto must not inspect Edge origins')):
            result = await self.cog._generate_audio_file(item)
        self.assertEqual(result, '/tmp/teto.wav')
        self.cog._generate_tts_agent_worker_file.assert_awaited_once_with(item)
        self.assertEqual(self.calls, [])


    async def test_queued_source_invalidated_by_other_429_never_opens_transport_or_counts_failure(self):
        self.assertTrue(self.cog._edge_select_source(self.item))
        lease = self.item._edge_source_lease
        pool = self.cog._edge_source_pool()
        other = pool.reserve(self.item.guild_id)
        pool.start(other)
        pool.failure(other, too_many_requests())
        before_engine = self.cog._get_engine_metrics('edge').copy()
        before_source = pool.snapshot()[PRIMARY].copy()
        with self.assertRaises(tts_audio.EdgeSourceUnavailable) as caught:
            async with self.cog._edge_source_communicate(self.item, text='queued request'):
                self.fail('origin became ineligible before opening transport')
        self.cog._record_engine_failure('edge', caught.exception, item=self.item)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.closed, [])
        self.assertFalse(lease.started)
        self.assertTrue(lease.finished)
        self.assertEqual(self.cog._get_engine_metrics('edge'), before_engine)
        after = pool.snapshot()[PRIMARY]
        for key in ('attempts','successes','failures','http_429','cancellations','rate_limit_level'):
            self.assertEqual(before_source[key], after[key], key)
        self.cog._notify_edge_rate_limit.assert_not_called()


    async def test_phone_producer_snapshot_survives_consumer_retrying_local_origin(self):
        self.item._edge_phone_recovery = True
        self.item._edge_worker_attempted = True
        self.item._edge_sources_attempted = {PRIMARY}
        other = replace(self.item)
        other._edge_phone_recovery = True
        other._edge_worker_attempted = True
        other._edge_sources_attempted = {PRIMARY}
        worker_started, fail_worker = asyncio.Event(), asyncio.Event()
        async def worker(job):
            job.route = 'worker'
            worker_started.set()
            await fail_worker.wait()
            raise RuntimeError('Phone upstream Edge HTTP 429')
        self.cog._produce_worker_stream_job = AsyncMock(side_effect=worker)
        one = self.cog._acquire_shared_job(self.state, self.item, store_in_cache=False)
        two = self.cog._acquire_shared_job(self.state, other, store_in_cache=False)
        try:
            self.assertIs(one, two)
            self.assertIsNot(one.item, self.item)
            self.assertIsNot(one.item._edge_sources_attempted, self.item._edge_sources_attempted)
            # Retry before the queued producer even starts. Another consumer
            # still owns the Phone request and must keep that exact transport.
            self.item._edge_phone_recovery = False
            self.item._edge_vps_recovery = True
            self.item._edge_sources_attempted.add('consumer-only-marker')
            self.assertTrue(self.cog._edge_select_source(self.item))
            self.assertEqual(self.item._edge_source_ip, SECONDARY)
            await asyncio.wait_for(worker_started.wait(), 1)
            self.assertTrue(one.item._edge_phone_recovery)
            self.assertNotIn('consumer-only-marker', one.item._edge_sources_attempted)
            self.assertFalse(other._edge_vps_recovery if hasattr(other, '_edge_vps_recovery') else False)
            fail_worker.set()
            await asyncio.wait_for(one.task, 1)
            self.assertEqual(self.calls, [], 'Phone producer must never open a VPS transport')
            self.assertEqual(sum(s.failures for s in self.cog._edge_source_pool().states.values()), 0)
            self.assertEqual(int(self.cog._get_engine_metrics('edge').get('synth_failures', 0)), 0)
            self.assertEqual(self.cog._get_metrics_store()['edge_stream_failures'], 1)
            self.cog._produce_worker_stream_job.assert_awaited_once()
        finally:
            fail_worker.set()
            self.cog._edge_source_release(self.item)
            self.cog._release_shared_job(one)
            self.cog._release_shared_job(two)
            await asyncio.gather(one.task, return_exceptions=True)


    async def test_cancel_one_shared_consumer_keeps_source_lease_until_other_finishes(self):
        other = replace(self.item)
        first_path, _ = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        second_path, _ = await self.cog._resolve_audio_path(self.state, other, allow_edge_stream=True)
        first = self.cog._edge_stream_handle_for_path(first_path)
        second = self.cog._edge_stream_handle_for_path(second_path)
        self.assertIs(first.shared_job, second.shared_job)
        job = first.shared_job
        lease = job.item._edge_source_lease
        self.assertEqual(job.references, 2)
        self.assertTrue(lease.started)
        self.assertFalse(lease.finished)
        await self.cog._finalize_edge_stream(first, cancel=True)
        self.assertEqual(job.references, 1)
        self.assertFalse(job.task.done())
        self.assertIs(job.item._edge_source_lease, lease)
        self.assertFalse(lease.finished)
        self.assertEqual(self.closed, [])
        self.tail.set()
        await asyncio.wait_for(job.task, 1)
        await asyncio.wait_for(second.producer_task, 1)
        self.assertTrue(lease.finished)
        self.assertEqual(self.calls, [PRIMARY])
        self.assertEqual(self.closed, [PRIMARY])
        source = self.cog._edge_source_pool().snapshot()[PRIMARY]
        self.assertEqual((source['attempts'],source['successes'],source['failures'],source['cancellations']),
                         (1,1,0,0))
        self.assertEqual(second.error, None)


if __name__ == '__main__':
    unittest.main()
