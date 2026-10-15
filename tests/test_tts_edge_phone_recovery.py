"""Phone Edge recovery must not delay healthy VPS Edge or emit false gTTS notices."""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_tts_helpers import tts_audio


class _Probe(tts_audio.TTSAudioMixin):
    def __init__(self):
        self.bot = SimpleNamespace(audio_router=None)
        self.edge_voice_names = {'pt-BR-FranciscaNeural'}
        self.guild_states = {}
        self._online = True

    def _tts_agent_route_available(self):
        return self._online

    def _tts_agent_route_state(self):
        return {'stream_protocol': 2}

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

    async def test_worker_success_must_not_reset_vps_probe(self):
        policy = self.cog._edge_rate_limit_policy()
        policy.failed_429(now=10)
        self.assertFalse(policy.should_bypass(now=100))
        self.cog._record_engine_success('edge', 500, route='worker')
        self.assertEqual(policy.level, 1)
        self.cog._record_engine_success('edge', 500, route='local')
        self.assertEqual(policy.level, 0)

    async def test_real_shared_stream_produces_phone_audio_without_edge_vps(self):
        async def worker_stream(job):
            job.route = 'worker'
            job.actual_engine = 'edge'
            await self.cog._append_shared_audio(job, b'\xff\xfb' + b'A' * 4096)

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


if __name__ == '__main__':
    unittest.main()
