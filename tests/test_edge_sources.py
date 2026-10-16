"""Per-origin Edge backoff, stable placement, and connector ownership."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

from test_tts_helpers import tts_audio
from cogs.tts import edge_sources as sources


PRIMARY = '10.0.0.174'
SECONDARY = '10.0.0.237'


def too_many_requests():
    error = RuntimeError('Edge upstream HTTP 429')
    error.status = 429
    return error


class EdgeSourcePoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = sources.EdgeSourcePool((PRIMARY, SECONDARY))
        self.guilds = {}
        for guild in range(1, 101):
            lease = self.pool.reserve(guild)
            self.guilds.setdefault(lease.source_ip, guild)
            self.pool.release(lease)
        self.primary_guild = self.guilds[PRIMARY]
        self.secondary_guild = self.guilds[SECONDARY]

    def test_guild_placement_is_stable_and_distributes_over_both_sources(self):
        placements = {}
        for guild in range(1, 101):
            observed = []
            for _ in range(3):
                lease = self.pool.reserve(guild)
                self.assertIsNotNone(lease)
                observed.append(lease.source_ip)
                self.pool.release(lease)
            self.assertEqual(len(set(observed)), 1)
            placements[guild] = observed[0]
        self.assertEqual(set(placements.values()), {PRIMARY, SECONDARY})
        self.assertGreater(sum(ip == PRIMARY for ip in placements.values()), 25)
        self.assertLess(sum(ip == PRIMARY for ip in placements.values()), 75)

    def test_discord_snowflakes_with_zero_low_bits_use_both_sources(self):
        placements = []
        for value in range(1000, 1100):
            lease = self.pool.reserve(value << 22)
            placements.append(lease.source_ip)
            self.pool.release(lease)
        self.assertEqual(set(placements), {PRIMARY, SECONDARY})
        self.assertGreater(placements.count(PRIMARY), 25)
        self.assertLess(placements.count(PRIMARY), 75)

    def test_guild_assignment_is_stable_across_process_hash_seeds(self):
        script = """
import json, sys
sys.path[:0] = json.loads(sys.argv[1])
from test_tts_helpers import tts_audio
from cogs.tts.edge_sources import EdgeSourcePool
pool=EdgeSourcePool(('10.0.0.174','10.0.0.237'))
print(json.dumps([pool.reserve(i << 22).source_ip for i in range(1000,1100)]))
"""
        observations = []
        for seed in ('1', '2'):
            process = subprocess.run([sys.executable, '-c', script, json.dumps(sys.path)],
                env={**os.environ, 'PYTHONHASHSEED':seed}, capture_output=True, text=True,
                check=True, timeout=10)
            observations.append(json.loads(process.stdout))
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(set(observations[0]), {PRIMARY, SECONDARY})

    def test_429_on_one_source_does_not_block_or_reset_other_source(self):
        lease = self.pool.reserve(self.primary_guild)
        failed_ip = lease.source_ip
        self.pool.start(lease)
        self.pool.failure(lease, too_many_requests())
        policy = self.pool.states[failed_ip].policy
        self.assertEqual(policy.level, 1)
        retry = self.pool.reserve(self.primary_guild)
        self.assertNotEqual(retry.source_ip, failed_ip)
        self.pool.start(retry)
        self.pool.success(retry, 20)
        self.assertEqual(policy.level, 1, 'another egress success must preserve this cooldown')
        self.assertEqual(self.pool.states[retry.source_ip].policy.level, 0)

    def test_each_source_can_independently_exhaust_its_cooldown(self):
        first = self.pool.reserve(self.primary_guild)
        self.pool.start(first)
        self.pool.failure(first, too_many_requests())
        second = self.pool.reserve(self.primary_guild)
        self.pool.start(second)
        self.pool.failure(second, too_many_requests())
        self.assertFalse(self.pool.any_available())
        self.assertIsNone(self.pool.reserve(self.primary_guild))
        self.assertEqual([self.pool.states[ip].policy.level for ip in (PRIMARY, SECONDARY)], [1, 1])

    def test_generic_error_circuit_is_per_source(self):
        pool = sources.EdgeSourcePool((PRIMARY, SECONDARY), circuit_failures=2)
        failed = None
        for _ in range(2):
            lease = pool.reserve(self.primary_guild)
            failed = lease.source_ip
            pool.start(lease)
            pool.failure(lease, TimeoutError('upstream timed out'))
        retry = pool.reserve(self.primary_guild)
        self.assertIsNotNone(retry)
        self.assertNotEqual(retry.source_ip, failed)
        self.assertEqual(pool.states[failed].policy.level, 0)
        pool.release(retry)

    def test_reserved_but_never_started_probe_is_released_for_next_request(self):
        first = self.pool.reserve(self.primary_guild)
        ip = first.source_ip
        self.pool.start(first)
        self.pool.failure(first, too_many_requests())
        policy = self.pool.states[ip].policy
        policy.retry_at = 0
        probe = self.pool.reserve(self.primary_guild)
        self.assertEqual(probe.source_ip, ip)
        self.assertTrue(probe.probe)
        self.assertTrue(policy.probing)
        self.pool.release(probe)
        self.pool.release(probe)  # cancellation and final cleanup may both run
        self.assertFalse(policy.probing)
        again = self.pool.reserve(self.primary_guild)
        self.assertEqual(again.source_ip, ip)
        self.assertTrue(again.probe)
        self.pool.release(again)

    def test_success_on_the_recovery_probe_resets_only_that_source(self):
        for guild in (self.primary_guild, self.secondary_guild):
            lease = self.pool.reserve(guild)
            self.pool.start(lease)
            self.pool.failure(lease, too_many_requests())
        self.pool.states[PRIMARY].policy.retry_at = 0
        lease = self.pool.reserve(self.primary_guild)
        self.assertEqual(lease.source_ip, PRIMARY)
        self.pool.start(lease)
        self.pool.success(lease, 20)
        self.assertEqual(self.pool.states[PRIMARY].policy.level, 0)
        self.assertEqual(self.pool.states[SECONDARY].policy.level, 1)

    def test_queued_normal_reservation_losing_eligibility_never_counts_as_an_attempt(self):
        queued = self.pool.reserve(self.primary_guild)
        running = self.pool.reserve(self.primary_guild)
        self.pool.start(running)
        self.pool.failure(running, too_many_requests())
        before = self.pool.snapshot()[PRIMARY].copy()
        with self.assertRaises(sources.EdgeSourceUnavailable) as caught:
            self.pool.start(queued)
        self.pool.failure(queued, caught.exception)
        self.assertTrue(queued.finished)
        self.assertFalse(queued.started)
        after = self.pool.snapshot()[PRIMARY]
        for key in ('attempts', 'failures', 'successes', 'http_429', 'cancellations', 'rate_limit_level'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after['attempts'], 1)
        self.assertEqual(after['failures'], 1)

    def test_old_success_preserves_newer_429_and_generic_error_circuit(self):
        pool = sources.EdgeSourcePool((PRIMARY, SECONDARY), circuit_failures=1)
        old = pool.reserve(self.primary_guild)
        newer = pool.reserve(self.primary_guild)
        pool.start(old)
        pool.start(newer)
        pool.failure(newer, too_many_requests())
        pool.success(old, 42)
        state = pool.states[PRIMARY]
        self.assertEqual(state.policy.level, 1)
        self.assertEqual(state.consecutive_failures, 1)
        self.assertEqual(state.last_error_type, 'RuntimeError')
        self.assertFalse(pool.available(PRIMARY))
        self.assertTrue(pool.snapshot()[PRIMARY]['circuit_open'])
        self.assertEqual(state.successes, 1, 'completed old audio still counts as successful synthesis')

    def test_probe_success_after_newer_inflight_failure_cannot_clear_escalation(self):
        old = self.pool.reserve(self.primary_guild)
        initial = self.pool.reserve(self.primary_guild)
        self.pool.start(old)
        self.pool.start(initial)
        self.pool.failure(initial, too_many_requests())
        state = self.pool.states[PRIMARY]
        state.policy.retry_at = 0
        probe = self.pool.reserve(self.primary_guild)
        self.assertTrue(probe.probe)
        self.pool.start(probe)
        self.pool.failure(old, too_many_requests())
        self.assertEqual(state.policy.level, 2)
        self.pool.success(probe, 42)
        self.assertEqual(state.policy.level, 2)
        self.assertEqual(state.consecutive_failures, 2)
        self.assertFalse(state.policy.probing)
        self.assertIsNone(state.active_probe)
        self.assertFalse(self.pool.available(PRIMARY))

    def test_origin_metrics_count_physical_attempts_and_finalize_each_lease_once(self):
        success = self.pool.reserve(self.primary_guild)
        self.pool.start(success)
        self.pool.success(success, 35)
        self.pool.success(success, 999)
        failed = self.pool.reserve(self.secondary_guild)
        self.pool.start(failed)
        self.pool.failure(failed, too_many_requests(), 12)
        self.pool.failure(failed, too_many_requests(), 999)
        self.pool.release(failed)
        self.pool.release(success)
        snapshot = self.pool.snapshot()
        primary, secondary = snapshot[PRIMARY], snapshot[SECONDARY]
        self.assertEqual((primary['attempts'],primary['successes'],primary['failures'],primary['http_429']),
                         (1,1,0,0))
        self.assertEqual((secondary['attempts'],secondary['successes'],secondary['failures'],secondary['http_429']),
                         (1,0,1,1))
        self.assertEqual(primary['avg_synth_ms'], 35)
        self.assertEqual(primary['last_synth_ms'], 35)
        self.assertEqual(secondary['last_synth_ms'], 12)
        self.assertEqual(primary['cancellations'], 0)
        self.assertEqual(secondary['cancellations'], 0)
        self.assertEqual(primary['rate_limit_level'], 0)
        self.assertEqual(secondary['rate_limit_level'], 1)

    def test_excluded_origin_is_never_retried_in_same_request(self):
        lease = self.pool.reserve(self.primary_guild)
        first = lease.source_ip
        self.pool.release(lease)
        second = self.pool.reserve(self.primary_guild, exclude={first})
        self.assertNotEqual(second.source_ip, first)
        self.pool.release(second)
        self.assertIsNone(self.pool.reserve(self.primary_guild, exclude={PRIMARY, SECONDARY}))




class EdgeSourceParsingTests(unittest.TestCase):
    def test_explicit_sources_accept_separators_and_remove_duplicates(self):
        self.assertEqual(sources.parse_source_ips(' 10.0.0.174,10.0.0.237; 10.0.0.174\n'),
                         (PRIMARY, SECONDARY))
        self.assertEqual(sources.parse_source_ips([PRIMARY, SECONDARY, PRIMARY]), (PRIMARY, SECONDARY))
        self.assertEqual(sources.parse_source_ips(''), ())

    def test_invalid_sources_never_silently_become_unbound_transport(self):
        for value in ('not-an-ip', '::1', '::', '0.0.0.0', '127.0.0.1', '224.0.0.1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sources.parse_source_ips(value)


class BoundConnectorOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # No DNS or sockets: the owner close is mocked, while the subclass's
        # borrowed close is the actual production implementation.
        self.connector = object.__new__(sources.SourceBoundConnector)
        self.constructor = patch.object(sources, 'SourceBoundConnector', return_value=self.connector)
        self.make = self.constructor.start()
        self.base_close_patch = patch.object(sources.aiohttp.TCPConnector, 'close',
                                            new=AsyncMock(),
                                            create=True)
        self.base_close = self.base_close_patch.start()

    async def asyncTearDown(self):
        self.base_close_patch.stop()
        self.constructor.stop()

    async def test_retry403_and_multiple_text_blocks_keep_same_connector_until_owner_exit(self):
        seen = []
        connector = self.connector
        base_close = self.base_close
        class Communicate:
            def __init__(self, **kwargs):
                seen.append(kwargs)
                self.connector = kwargs['connector']
            async def stream(self):
                # Reproduce edge-tts closing each borrowed ClientSession:
                # first 403, retry, and two 4096-byte text blocks.
                for action in ('403', 'retry', 'next-text-block'):
                    await self.connector.close()
                    base_close.assert_not_awaited()
                    if action != '403':
                        yield {'type':'audio', 'data':action.encode()}
        async with sources.bound_communicate(Communicate, PRIMARY, text='x'*9000) as talk:
            chunks = [message['data'] async for message in talk.stream()]
            self.assertEqual(chunks, [b'retry', b'next-text-block'])
            self.assertIs(talk.connector, connector)
            self.base_close.assert_not_awaited()
        self.make.assert_called_once()
        self.assertEqual(self.make.call_args.kwargs['local_addr'], (PRIMARY, 0))
        self.assertIs(seen[0]['connector'], connector)
        self.base_close.assert_awaited_once_with(connector)

    async def test_constructor_failure_closes_owner_connector(self):
        def factory(**kwargs):
            raise RuntimeError('invalid speech request')
        with self.assertRaisesRegex(RuntimeError, 'invalid speech request'):
            async with sources.bound_communicate(factory, SECONDARY):
                self.fail('constructor failed')
        self.base_close.assert_awaited_once_with(self.connector)

    async def test_effective_proxy_is_rejected_only_by_source_bound_connector(self):
        base_proxy = AsyncMock(return_value='normal-proxy-connection')
        with patch.object(sources.aiohttp.TCPConnector, '_create_proxy_connection',
                          new=base_proxy, create=True):
            with self.assertRaisesRegex(RuntimeError, 'proxy de ambiente incompatível'):
                await self.connector._create_proxy_connection('request', 'traces', 'timeout')
            base_proxy.assert_not_awaited()
            normal = object.__new__(sources.aiohttp.TCPConnector)
            result = await normal._create_proxy_connection('request', 'traces', 'timeout')
            self.assertEqual(result, 'normal-proxy-connection')
            base_proxy.assert_awaited_once()

    async def test_unused_proxy_environment_does_not_reject_source_bound_request(self):
        original = dict(os.environ)
        with patch.dict(os.environ, {'HTTPS_PROXY':'http://unused-proxy.invalid:8080'}):
            async with sources.bound_communicate(lambda **kwargs:kwargs, PRIMARY) as talk:
                self.assertIs(talk['connector'], self.connector)
                self.assertEqual(os.environ['HTTPS_PROXY'], 'http://unused-proxy.invalid:8080')
        self.assertEqual(dict(os.environ), original)
        self.base_close.assert_awaited_once_with(self.connector)

    async def test_cancelled_stream_closes_owner_connector(self):
        started = asyncio.Event()
        async def run():
            async with sources.bound_communicate(lambda **kwargs: kwargs, SECONDARY):
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.base_close.assert_awaited_once_with(self.connector)


if __name__ == '__main__':
    unittest.main()
