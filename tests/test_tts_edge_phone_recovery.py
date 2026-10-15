"""Phone Edge recovery must not delay healthy VPS Edge or emit false gTTS notices."""
import asyncio
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_tts_helpers import tts_audio


@contextmanager
def stream_runtime():
    with tempfile.TemporaryDirectory() as tmp, patch.multiple(
        tts_audio, TTS_TEMP_DIR=tmp, _RUNTIME_DIR=os.path.join(tmp, 'runtime'),
        _CACHE_DIR=os.path.join(tmp, 'cache'),
        _TTS_REQUIRED_DIRS=(tmp, os.path.join(tmp, 'runtime'), os.path.join(tmp, 'cache')),
    ), patch.object(tts_audio.config, 'TTS_FFMPEG_OVERLAP_ENABLED', False, create=True):
        tts_audio._ensure_tts_temp_dirs()
        yield tmp


class _Probe(tts_audio.TTSAudioMixin):
    def __init__(self):
        self.bot = SimpleNamespace(audio_router=None)
        self.edge_voice_names = {'pt-BR-FranciscaNeural'}
        self.guild_states = {}
        self._online = True

    def _tts_agent_route_available(self):
        return self._online

    def _tts_agent_route_state(self):
        return {'stream_protocol': 2, 'available_engines': ['edge', 'teto']}

    def _tts_agent_should_try_worker(self, item):
        return (False, 'local_fast_path')

    def _edge_streaming_allowed_for(self, item):
        return True, 'allowed'

    def _gtts_streaming_allowed_for(self, item):
        return False, 'disabled_for_test'

    def _try_get_cached_path(self, state, item):
        return None

    def _record_edge_circuit_bypass(self):
        pass

    def _edge_circuit_is_open(self):
        return False


class EdgePhoneRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cog = _Probe()
        self.state = tts_audio.GuildTTSState(queue=asyncio.Queue())
        self.item = tts_audio.QueueItem(
            guild_id=77, channel_id=9, author_id=6, text='Oi, bom dia',
            engine='edge', voice='pt-BR-FranciscaNeural', language='pt',
            rate='+5%', pitch='-3Hz', text_channel_id=11,
        )

    async def asyncTearDown(self):
        for handle in list(self.cog._get_edge_stream_handles().values()):
            await self.cog._finalize_edge_stream(handle, cancel=True)
        self.cog._shutdown_tts_runtime()
        tasks = list(self.cog._get_tts_background_tasks())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        self.cog._audio_leases().close()

    async def test_vps_healthy_does_not_check_phone(self):
        self.cog._prepare_edge_stream = AsyncMock(return_value=SimpleNamespace(fifo_path='/tmp/vps.fifo'))
        self.cog._try_edge_phone_stream = AsyncMock()
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(result, ('/tmp/vps.fifo', True))
        self.cog._try_edge_phone_stream.assert_not_awaited()

    async def test_vps_cooldown_uses_phone_stream_without_gtts_notice(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._prepare_edge_stream = AsyncMock(return_value=SimpleNamespace(fifo_path='/tmp/phone.fifo'))
        self.cog._resolve_edge_gtts_fallback = AsyncMock()
        self.cog._notify_edge_rate_limit = lambda *_: self.fail('notice before a real gTTS fallback')
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(result, ('/tmp/phone.fifo', True))
        self.assertTrue(self.item._edge_phone_recovery)
        self.assertEqual(self.cog._get_metrics_store()['edge_phone_recovery_success'], 1)
        self.cog._resolve_edge_gtts_fallback.assert_not_awaited()

    async def test_first_vps_429_recovers_phone_in_same_request(self):
        async def prepare(state, item, *, store_in_cache):
            if not getattr(item, '_edge_phone_recovery', False):
                exc = RuntimeError('429 from Edge')
                exc.status = 429
                self.cog._record_engine_failure('edge', exc, item=item)
                raise exc
            return SimpleNamespace(fifo_path='/tmp/phone.fifo')
        self.cog._prepare_edge_stream = prepare
        self.cog._resolve_edge_gtts_fallback = AsyncMock()
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(result, ('/tmp/phone.fifo', True))
        self.assertEqual(self.cog._edge_rate_limit_policy().level, 1)
        self.cog._resolve_edge_gtts_fallback.assert_not_awaited()

    async def test_both_origins_limited_go_directly_to_gtts(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._edge_phone_policy().failed_429()
        self.cog._prepare_edge_stream = AsyncMock()
        self.cog._resolve_edge_gtts_fallback = AsyncMock(return_value=('/tmp/gtts.mp3', True))
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(result[0], '/tmp/gtts.mp3')
        self.cog._prepare_edge_stream.assert_not_awaited()
        self.cog._resolve_edge_gtts_fallback.assert_awaited_once()

    async def test_worker_429_only_increases_phone_cooldown(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._edge_phone_result(success=False, error=RuntimeError('worker HTTP 502: WSServerHandshakeError: 429'))
        self.assertEqual(self.cog._edge_rate_limit_policy().level, 1)
        self.assertEqual(self.cog._edge_phone_policy().level, 1)
        self.assertFalse(self.cog._edge_phone_available(self.item, streaming=True))

    async def test_transient_phone_failure_backoff(self):
        self.cog._edge_phone_result(success=False, error=RuntimeError('worker disconnected'))
        self.assertEqual(self.cog._edge_phone_policy().level, 0)
        self.assertFalse(self.cog._edge_phone_available(self.item, streaming=True))

    async def test_recovery_requires_explicit_edge_capability(self):
        self.cog._prepare_edge_stream = AsyncMock()
        for engines in ([], ['teto']):
            self.cog._tts_agent_route_state = lambda: {'stream_protocol': 2, 'available_engines': engines}
            self.assertIsNone(await self.cog._try_edge_phone_stream(self.state, self.item, store_in_cache=False))
        self.cog._prepare_edge_stream.assert_not_awaited()

    async def test_edge_worker_request_disallows_silent_gtts_substitution(self):
        with patch.object(tts_audio, 'TTS_WORKER_AGENT_PREFERRED_ENGINE', 'teto'):
            payload = self.cog._tts_agent_payload_for_item(self.item)
        self.assertEqual(payload['preferred_engine'], 'edge')
        self.assertEqual(payload['fallback_engine'], 'edge')
        self.assertEqual((payload['voice'], payload['rate'], payload['pitch']),
                         (self.item.voice, '+5%', '-3Hz'))

    async def test_file_worker_substitution_is_rejected_before_success_metrics(self):
        self.cog._record_tts_agent_synth_success = Mock()
        self.cog._mark_tts_agent_synth_failure = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'wrong-engine.mp3')
            with open(path, 'wb') as audio:
                audio.write(b'gtts audio')
            self.cog._request_phone_worker_tts_audio = AsyncMock(return_value={
                'audio_path': path, 'audio_format': 'mp3', 'selected_engine': 'gtts',
            })
            with self.assertRaisesRegex(RuntimeError, 'não retornou áudio Edge'):
                await self.cog._generate_tts_agent_worker_file(self.item)
            self.assertFalse(os.path.exists(path))
        self.cog._record_tts_agent_synth_success.assert_not_called()

    async def test_failed_adaptive_worker_stream_is_not_retried_by_recovery(self):
        class Response:
            status = 502
            content = SimpleNamespace(read=AsyncMock(return_value=b'upstream unavailable'))
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        self.cog._get_phone_worker_http_session = AsyncMock(return_value=SimpleNamespace(post=lambda *args, **kwargs: Response()))
        self.cog._phone_worker_tts_base_url = lambda: 'http://worker.test'
        self.cog._mark_tts_agent_synth_failure = lambda error: None
        job = SimpleNamespace(item=self.item, deadline=tts_audio.time.monotonic() + 10)
        with patch.object(tts_audio.aiohttp, 'ClientTimeout', side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 502'):
                await self.cog._produce_worker_stream_job(job)
        self.cog._prepare_edge_stream = AsyncMock()
        self.assertIsNone(await self.cog._try_edge_phone_stream(self.state, self.item, store_in_cache=False))
        self.cog._prepare_edge_stream.assert_not_awaited()

    async def test_phone_is_not_attempted_twice_for_same_request(self):
        self.cog._prepare_edge_stream = AsyncMock(side_effect=RuntimeError('worker disconnected'))
        self.assertIsNone(await self.cog._try_edge_phone_stream(self.state, self.item, store_in_cache=False))
        # Even after the short transport backoff has elapsed, this request has
        # already tried the phone and must continue to its final fallback.
        self.cog._tts_edge_phone_unavailable_until = 0
        self.assertIsNone(await self.cog._try_edge_phone_stream(self.state, self.item, store_in_cache=False))
        self.cog._prepare_edge_stream.assert_awaited_once()

    async def test_non_429_vps_failure_still_tries_second_edge_origin(self):
        async def prepare(state, item, *, store_in_cache):
            if not getattr(item, '_edge_phone_recovery', False):
                raise TimeoutError('VPS upstream timed out')
            return SimpleNamespace(fifo_path='/tmp/phone.fifo')
        self.cog._prepare_edge_stream = prepare
        self.cog._resolve_edge_gtts_fallback = AsyncMock()
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(result, ('/tmp/phone.fifo', True))
        self.assertEqual(self.cog._edge_rate_limit_policy().level, 0)
        self.cog._resolve_edge_gtts_fallback.assert_not_awaited()

    async def test_file_cooldown_recovers_phone_without_new_vps_request(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._shared_job_file = AsyncMock(side_effect=AssertionError('VPS was rate limited'))
        async def phone(item):
            item._tts_agent_selected_engine = 'edge'
            return '/tmp/phone.mp3'
        self.cog._generate_tts_agent_worker_file = AsyncMock(side_effect=phone)
        self.cog._store_in_cache = AsyncMock(side_effect=lambda state, item, path: path)
        self.cog._lease_audio_path = lambda *args: None
        self.cog._resolve_edge_gtts_fallback = AsyncMock()
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=False)
        self.assertEqual(result, ('/tmp/phone.mp3', True))
        self.cog._shared_job_file.assert_not_awaited()
        self.cog._resolve_edge_gtts_fallback.assert_not_awaited()

    async def test_file_vps_failure_recovers_phone_before_gtts(self):
        self.cog._shared_job_file = AsyncMock(side_effect=TimeoutError('VPS failed'))
        async def phone(item):
            item._tts_agent_selected_engine = 'edge'
            return '/tmp/phone.mp3'
        self.cog._generate_tts_agent_worker_file = AsyncMock(side_effect=phone)
        self.cog._store_in_cache = AsyncMock(side_effect=lambda state, item, path: path)
        self.cog._lease_audio_path = lambda *args: None
        self.cog._resolve_edge_gtts_fallback = AsyncMock()
        result = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=False)
        self.assertEqual(result, ('/tmp/phone.mp3', True))
        self.cog._resolve_edge_gtts_fallback.assert_not_awaited()

    async def test_worker_success_must_not_reset_vps_probe(self):
        policy = self.cog._edge_rate_limit_policy()
        policy.failed_429(now=10)
        self.assertFalse(policy.should_bypass(now=100))
        self.cog._record_engine_success('edge', 500, route='worker')
        self.assertEqual(policy.level, 1)
        self.cog._record_engine_success('edge', 500, route='local')
        self.assertEqual(policy.level, 0)

    async def test_open_circuit_does_not_reserve_a_vps_probe(self):
        policy = self.cog._edge_rate_limit_policy()
        policy.failed_429(now=tts_audio.time.monotonic() - 100)
        self.cog._edge_circuit_is_open = lambda: True
        self.assertTrue(self.cog._edge_rate_limit_bypass(self.item))
        self.assertFalse(policy.probing)

    async def test_cancelled_file_probe_releases_vps_guard(self):
        policy = self.cog._edge_rate_limit_policy()
        policy.failed_429(now=tts_audio.time.monotonic() - 100)
        self.assertFalse(self.cog._edge_rate_limit_bypass(self.item))
        with self.assertRaises(asyncio.CancelledError):
            await self.cog._run_timed_generation('edge', AsyncMock(side_effect=asyncio.CancelledError()), item=self.item)
        self.assertFalse(policy.probing)

    async def test_expired_cooldown_probes_vps_even_when_adaptive_route_prefers_phone(self):
        calls, tail = [], asyncio.Event()
        class VPS:
            def __init__(self, **kwargs):
                calls.append(kwargs)
            async def stream(self):
                yield {'type': 'audio', 'data': b'V' * 4096}
                await tail.wait()
        self.cog._tts_agent_should_try_worker = lambda item: (True, 'adaptive_phone')
        self.cog._produce_worker_stream_job = AsyncMock(side_effect=AssertionError('probe must use VPS'))
        policy = self.cog._edge_rate_limit_policy()
        policy.failed_429(now=tts_audio.time.monotonic() - 100)
        with stream_runtime(), patch.object(tts_audio.edge_tts, 'Communicate', VPS):
            self.assertFalse(self.cog._edge_rate_limit_bypass(self.item))
            handle = await self.cog._prepare_edge_stream(self.state, self.item, store_in_cache=False)
            try:
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.item._tts_audio_route, 'local')
                tail.set()
                await asyncio.wait_for(handle.shared_job.task, 1)
                self.assertEqual(policy.level, 0)
                self.assertFalse(policy.probing)
            finally:
                await self.cog._finalize_edge_stream(handle, cancel=True)
        self.cog._produce_worker_stream_job.assert_not_awaited()

    async def test_cancelled_stream_probe_releases_vps_guard(self):
        started = asyncio.Event()
        class VPS:
            def __init__(self, **kwargs):
                pass
            async def stream(self):
                started.set()
                await asyncio.Event().wait()
                yield {'type': 'audio', 'data': b'unreachable'}
        policy = self.cog._edge_rate_limit_policy()
        policy.failed_429(now=tts_audio.time.monotonic() - 100)
        with stream_runtime(), patch.object(tts_audio.edge_tts, 'Communicate', VPS):
            request = asyncio.create_task(self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True))
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(policy.probing)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(request, 1)
            self.assertFalse(policy.probing)

    async def test_partial_shared_phone_failure_recovers_vps_without_mixing_or_retries(self):
        calls, tail = [], asyncio.Event()
        phone_started, phone_failed = asyncio.Event(), asyncio.Event()
        class VPS:
            def __init__(self, **kwargs):
                calls.append(kwargs)
            async def stream(self):
                yield {'type': 'audio', 'data': b'V' * 4096}
                await tail.wait()
        async def phone(job):
            job.route = 'worker'
            job.item._edge_worker_attempted = True
            await self.cog._append_shared_audio(job, b'bad-prefix')
            phone_started.set()
            await phone_failed.wait()
            raise RuntimeError('worker disconnected before prebuffer')
        self.cog._tts_agent_should_try_worker = lambda item: (True, 'adaptive_phone')
        self.cog._produce_worker_stream_job = AsyncMock(side_effect=phone)
        self.cog._resolve_edge_gtts_fallback = AsyncMock(side_effect=AssertionError('VPS was never tried'))
        with stream_runtime(), patch.object(tts_audio.edge_tts, 'Communicate', VPS):
            first = asyncio.create_task(self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True))
            await asyncio.wait_for(phone_started.wait(), 1)
            second_item = replace(self.item, guild_id=88)
            second = asyncio.create_task(self.cog._resolve_audio_path(self.state, second_item, allow_edge_stream=True))
            async def both_consumers():
                while self.item._tts_shared_job.references < 2:
                    await asyncio.sleep(0)
            await asyncio.wait_for(both_consumers(), 1)
            phone_failed.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 1)
            handles = [self.cog._edge_stream_handle_for_path(path) for path, _ in results]
            try:
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.item._tts_audio_route, 'local')
                self.assertTrue(second_item._edge_worker_attempted)
                self.assertIs(handles[0].shared_job, handles[1].shared_job)
                self.assertEqual(bytes(handles[0].shared_job.buffer._memory), b'V' * 4096)
                self.cog._produce_worker_stream_job.assert_awaited_once()
            finally:
                for handle in handles:
                    await self.cog._finalize_edge_stream(handle, cancel=True)

    async def test_file_phone_failure_recovers_vps_before_gtts(self):
        async def phone(job):
            job.route = 'worker'
            job.item._edge_worker_attempted = True
            await self.cog._append_shared_audio(job, b'bad-prefix')
            raise RuntimeError('worker disconnected')
        self.cog._tts_agent_should_try_worker = lambda item: (True, 'adaptive_phone')
        self.cog._produce_worker_stream_job = AsyncMock(side_effect=phone)
        self.cog._resolve_edge_gtts_fallback = AsyncMock(side_effect=AssertionError('VPS was never tried'))
        self.cog._store_in_cache = AsyncMock(side_effect=lambda state, item, path: path)
        with stream_runtime() as tmp:
            path = os.path.join(tmp, 'vps.mp3')
            with open(path, 'wb') as audio:
                audio.write(b'VPS audio')
            self.cog._generate_edge_file = AsyncMock(return_value=path)
            self.assertEqual(await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=False), (path, True))
            self.cog._generate_edge_file.assert_awaited_once()

    async def test_phone_recovery_429_does_not_trip_shared_worker_circuit(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._request_phone_worker_tts_audio = AsyncMock(side_effect=RuntimeError('upstream Edge HTTP 429'))
        self.cog._mark_tts_agent_synth_failure = Mock()
        self.assertIsNone(await self.cog._try_edge_phone_file(self.item))
        self.cog._mark_tts_agent_synth_failure.assert_not_called()
        self.assertEqual(self.cog._edge_phone_policy().level, 1)

    async def test_real_shared_stream_produces_phone_audio_without_edge_vps(self):
        tail = asyncio.Event()
        async def worker_stream(job):
            job.route = 'worker'
            job.actual_engine = 'edge'
            await self.cog._append_shared_audio(job, b'\xff\xfb' + b'A' * 4096)
            await tail.wait()

        self.cog._produce_worker_stream_job = worker_stream
        self.cog._edge_rate_limit_policy().failed_429()
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(
            tts_audio, TTS_TEMP_DIR=tmp, _RUNTIME_DIR=os.path.join(tmp, 'runtime'),
            _CACHE_DIR=os.path.join(tmp, 'cache'),
            _TTS_REQUIRED_DIRS=(tmp, os.path.join(tmp, 'runtime'), os.path.join(tmp, 'cache')),
        ), patch.object(tts_audio.config, 'TTS_FFMPEG_OVERLAP_ENABLED', False, create=True), patch.object(
            tts_audio.edge_tts, 'Communicate', side_effect=AssertionError('VPS Edge never called'),
        ):
            tts_audio._ensure_tts_temp_dirs()
            path, cleanup = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
            self.assertTrue(cleanup)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(self.item._tts_actual_engine, 'edge')
            self.assertEqual(self.item._tts_audio_route, 'worker')
            self.assertEqual(self.cog._edge_rate_limit_policy().level, 1)
            handle = self.cog._edge_stream_handle_for_path(path)
            self.assertIsNotNone(handle)
            await self.cog._finalize_edge_stream(handle, cancel=True)

    async def test_remote_stream_failure_never_retries_vps_edge(self):
        async def worker_stream(job):
            job.route = 'worker'
            raise RuntimeError('worker stream HTTP 502: WSServerHandshakeError 429')

        self.cog._produce_worker_stream_job = worker_stream
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._resolve_edge_gtts_fallback = AsyncMock(return_value=('/tmp/gtts.mp3', True))
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(
            tts_audio, TTS_TEMP_DIR=tmp, _RUNTIME_DIR=os.path.join(tmp, 'runtime'),
            _CACHE_DIR=os.path.join(tmp, 'cache'),
            _TTS_REQUIRED_DIRS=(tmp, os.path.join(tmp, 'runtime'), os.path.join(tmp, 'cache')),
        ), patch.object(tts_audio.config, 'TTS_FFMPEG_OVERLAP_ENABLED', False, create=True), patch.object(
            tts_audio.edge_tts, 'Communicate', side_effect=AssertionError('VPS Edge never called'),
        ):
            tts_audio._ensure_tts_temp_dirs()
            path, _ = await self.cog._resolve_audio_path(self.state, self.item, allow_edge_stream=True)
        self.assertEqual(path, '/tmp/gtts.mp3')
        self.assertEqual(self.cog._edge_rate_limit_policy().level, 1)
        self.assertEqual(self.cog._edge_phone_policy().level, 1)
        self.cog._resolve_edge_gtts_fallback.assert_awaited_once()

    async def test_phone_stream_returns_before_completion_while_vps_job_is_stalled(self):
        vps_tail, phone_tail = asyncio.Event(), asyncio.Event()
        class StalledVPS:
            def __init__(self, **kwargs):
                pass
            async def stream(self):
                await vps_tail.wait()
                yield {'type': 'audio', 'data': b'V' * 4096}
        async def phone(job):
            job.route = 'worker'
            await self.cog._append_shared_audio(job, b'P' * 4096)
            await phone_tail.wait()
            await self.cog._append_shared_audio(job, b'T' * 1024)
        self.cog._produce_worker_stream_job = phone
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(
            tts_audio, TTS_TEMP_DIR=tmp, _RUNTIME_DIR=os.path.join(tmp, 'runtime'),
            _CACHE_DIR=os.path.join(tmp, 'cache'),
            _TTS_REQUIRED_DIRS=(tmp, os.path.join(tmp, 'runtime'), os.path.join(tmp, 'cache')),
        ), patch.object(tts_audio.config, 'TTS_FFMPEG_OVERLAP_ENABLED', False, create=True), patch.object(
            tts_audio.edge_tts, 'Communicate', StalledVPS,
        ):
            tts_audio._ensure_tts_temp_dirs()
            # Another guild still owns the stalled VPS job for identical text.
            local_job = self.cog._acquire_shared_job(self.state, self.item, store_in_cache=False)
            await local_job.slot_ready.wait()
            remote_item = replace(self.item, guild_id=88)
            handle = None
            try:
                path, _ = await asyncio.wait_for(
                    self.cog._try_edge_phone_stream(self.state, remote_item, store_in_cache=False), .5,
                )
                handle = self.cog._edge_stream_handle_for_path(path)
                self.assertIsNot(handle.shared_job, local_job)
                self.assertFalse(handle.shared_job.task.done(), 'must hand off before phone finishes')
                self.assertFalse(local_job.task.done())
                self.assertEqual(remote_item._tts_audio_route, 'worker')
            finally:
                phone_tail.set()
                vps_tail.set()
                if handle is not None:
                    await self.cog._finalize_edge_stream(handle, cancel=True)
                self.cog._release_shared_job(local_job)
                await asyncio.gather(local_job.task, return_exceptions=True)

    async def test_notice_only_after_successful_gtts(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._notify_edge_rate_limit = lambda *_: setattr(self, 'notices', getattr(self, 'notices', 0) + 1)
        self.assertTrue(self.cog._edge_rate_limit_bypass(self.item))
        self.assertEqual(getattr(self, 'notices', 0), 0)
        self.cog._lease_audio_path = lambda *args: None
        self.cog._release_item_audio = lambda *args: None
        self.cog._build_edge_gtts_fallback_item = lambda item: SimpleNamespace(
            text=item.text, language=item.language, engine='gtts', guild_id=item.guild_id,
            _cache_key_value=None, tld='com', _tts_audio_route='local', _tts_audio_origin='generated'
        )
        self.cog._try_get_cached_path = lambda state, item: '/tmp/gtts-cached.mp3'
        await self.cog._resolve_edge_gtts_fallback(self.state, self.item, allow_stream=False)
        self.assertEqual(self.notices, 1)

    async def test_failed_gtts_does_not_notify(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._notify_edge_rate_limit = lambda *_: self.fail('gtts failed')
        self.cog._try_get_cached_path = lambda *args: None
        self.cog._gtts_streaming_allowed_for = lambda *_: (False, 'off')
        self.cog._shared_job_file = AsyncMock(side_effect=RuntimeError('gtts failed'))
        with self.assertRaisesRegex(RuntimeError, 'gtts failed'):
            await self.cog._resolve_edge_gtts_fallback(self.state, self.item, allow_stream=False)
        self.assertEqual(self.cog._edge_rate_limit_policy().guild_notices, {})

    async def test_decoder_recovery_uses_phone_before_gtts(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._try_edge_phone_file = AsyncMock(return_value='/tmp/phone.mp3')
        self.cog._generate_gtts_file_with_priority = AsyncMock()
        self.cog._notify_edge_rate_limit = lambda *_: self.fail('Edge recovery is not gTTS')
        self.assertEqual(await self.cog._tts_local_complete_file_after_stream_failure(self.item), '/tmp/phone.mp3')
        self.cog._generate_gtts_file_with_priority.assert_not_awaited()

    async def test_decoder_recovery_only_notifies_after_nonempty_gtts(self):
        self.cog._edge_rate_limit_policy().failed_429()
        self.cog._try_edge_phone_file = AsyncMock(return_value=None)
        self.cog._notify_edge_rate_limit = lambda *_: setattr(self, 'notices', getattr(self, 'notices', 0) + 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fallback.mp3')
            with open(path, 'wb') as audio:
                audio.write(b'mp3')
            self.cog._generate_gtts_file_with_priority = AsyncMock(return_value=path)
            self.assertEqual(await self.cog._tts_local_complete_file_after_stream_failure(self.item), path)
            self.assertEqual(self.notices, 1)
            with open(path, 'wb'):
                pass
            with self.assertRaisesRegex(RuntimeError, 'vazio'):
                await self.cog._tts_local_complete_file_after_stream_failure(replace(self.item))
            self.assertEqual(self.notices, 1)


if __name__ == '__main__':
    unittest.main()
