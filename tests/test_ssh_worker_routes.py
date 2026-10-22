"""SSH must isolate transfers and recover HTTP without clearing provider limits."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_tts_helpers import tts_audio
from cogs.musica import configuracao as music_config
from cogs.musica.agente_telefone import saude, transporte_http, utilitarios
from cogs.musica.agente_telefone.conversao_resolucao import url_stream_worker


class BulkRouteTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.multiple(music_config, PHONE_WORKER_TRANSPORT='ssh',
            PHONE_WORKER_ENABLED=True, PHONE_WORKER_HOST='127.0.0.1', PHONE_WORKER_PORT=18766,
            PHONE_WORKER_BULK_PORT=18767, PHONE_WORKER_SCHEME='http', PHONE_WORKER_TOKEN='private',
            PHONE_WORKER_ID='phone-one')
        self.config.start(); self.addCleanup(self.config.stop)

    def test_only_transfer_port_changes_for_configured_origin(self):
        base = 'http://127.0.0.1:18766/music/stream/example?offset=3'
        self.assertEqual(utilitarios._phone_worker_bulk_url(base),
                         'http://127.0.0.1:18767/music/stream/example?offset=3')
        self.assertEqual(utilitarios._phone_worker_base_url(), 'http://127.0.0.1:18766')

    def test_other_worker_and_public_urls_are_unchanged(self):
        for base in ('http://127.0.0.1:18866/music/stream/example',
                     'https://media.example/music/stream/example',
                     'http://user:private@127.0.0.1:18766/music/stream/example'):
            self.assertEqual(utilitarios._phone_worker_bulk_url(base), base)

    def test_legacy_keeps_original_url(self):
        with patch.object(music_config, 'PHONE_WORKER_TRANSPORT', 'legacy'):
            base = 'http://127.0.0.1:18766/music/stream/example'
            self.assertEqual(utilitarios._phone_worker_bulk_url(base), base)

    def test_pcm_stream_path_uses_bulk_without_changing_cdn(self):
        base = 'http://127.0.0.1:18766'
        self.assertEqual(url_stream_worker(base, {'worker_stream_path':'/music/stream/abc'}),
                         'http://127.0.0.1:18767/music/stream/abc')
        self.assertEqual(url_stream_worker(base, {'worker_stream_url':base+'/music/stream/abc'}),
                         'http://127.0.0.1:18767/music/stream/abc')
        self.assertEqual(url_stream_worker(base, {'worker_stream_url':'https://cdn.example/song.mp3'}),
                         'https://cdn.example/song.mp3')


class MusicHealthTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(music_config, PHONE_WORKER_TRANSPORT='ssh', PHONE_WORKER_ID='phone-one'))
        self.stack.enter_context(patch.object(saude, '_SSH_HEALTH_CACHE', None))
        self.response = Mock(status=200)
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.body = {'ok':True,'worker_id':'phone-one','profile':'turbo',
                     'music_agent':{'ok':True,'available':True},'connection':{'generation':1}}
        self.response.read.side_effect = lambda limit: json.dumps(self.body).encode()
        self.opener = Mock()
        self.opener.open.return_value = self.response
        self.build = self.stack.enter_context(patch.object(saude.urllib.request, 'build_opener', return_value=self.opener))

    def test_ssh_uses_light_endpoint_and_no_proxy_or_redirect(self):
        result = saude.consultar_saude_worker_configurado('http://127.0.0.1:18766', 'private')
        self.assertTrue(result['ok'])
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:18766/music-agent/health')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private')
        self.assertTrue(any(isinstance(x, saude._NoHealthRedirect) for x in self.build.call_args.args))
        self.assertIsNone(saude._NoHealthRedirect().redirect_request(None,None,None,None,None,None))

    def test_fresh_authenticated_health_is_cached(self):
        with patch.object(saude.time, 'monotonic', return_value=100):
            first = saude.consultar_saude_worker_configurado('http://127.0.0.1:18766', 'private')
        with patch.object(saude.time, 'monotonic', return_value=104):
            second = saude.consultar_saude_worker_configurado('http://127.0.0.1:18766', 'private')
            self.assertEqual(saude.geracao_ssh_verificada(), 1)
        self.assertIs(first, second)
        self.assertEqual(self.opener.open.call_count, 1)
        with patch.object(saude.time, 'monotonic', return_value=106):
            self.assertEqual(saude.geracao_ssh_verificada(), 0)

    def test_wrong_or_missing_expected_identity_is_not_ready(self):
        self.body['worker_id'] = 'other-phone'
        self.assertIsNone(saude.consultar_saude_worker_configurado('http://127.0.0.1:18766', 'private'))
        self.assertIsNone(saude._SSH_HEALTH_CACHE)
        with patch.object(music_config, 'PHONE_WORKER_ID', ''):
            self.assertIsNone(saude.consultar_saude_worker_configurado('http://127.0.0.1:18766', 'private'))

    def test_health_disabled_does_not_skip_identity_in_ssh_mode(self):
        self.body['worker_id'] = 'wrong'
        with patch.object(music_config, 'MUSIC_WORKER_CONFIGURED_HEALTHCHECK_ENABLED', False):
            self.assertIsNone(saude.consultar_saude_worker_configurado('http://127.0.0.1:18766', 'private'))

    def test_old_worker_legacy_health_contract_is_preserved(self):
        with patch.object(music_config, 'PHONE_WORKER_TRANSPORT', 'legacy'), \
             patch.object(saude.urllib.request, 'urlopen', return_value=self.response) as request:
            self.assertTrue(saude.consultar_saude_worker_configurado('http://phone:8766', 'private')['ok'])
            self.assertEqual(request.call_args.args[0].full_url, 'http://phone:8766/health')


def health(generation=1, worker_id='phone-one'):
    return {'ok':True,'worker_id':worker_id,'version':'1',
            'connection':{'mode':'ssh','generation':generation,'interactive_state':'connected'},
            'tts_agent':{'ok':True,'available':True,'synth_ready':True,'available_engines':['edge','teto'],
                         'state':'ready','stream_protocol':2,'cache_binary_protocol':2}}


class TTSHealthSSHTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(tts_audio, PHONE_WORKER_TRANSPORT='ssh', PHONE_WORKER_ID='phone-one',
            PHONE_WORKER_HOST='127.0.0.1', PHONE_WORKER_PORT=18766, PHONE_WORKER_BULK_PORT=18767,
            PHONE_WORKER_ENABLED=True, PHONE_WORKER_TOKEN='private', PHONE_WORKER_SCHEME='http'))
        self.cog = tts_audio.TTSAudioMixin()
        self.cog.bot = SimpleNamespace(audio_router=None)
        self.cog.guild_states = {}
        self.cog._tts_agent_base_configured = lambda: True
        self.cog._fetch_tts_agent_light_health = AsyncMock(return_value=health())

    async def test_authenticated_recovery_only_clears_transport_cooldown(self):
        self.cog._edge_phone_policy().failed_429()
        level = self.cog._edge_phone_policy().level
        self.cog._tts_edge_phone_unavailable_until = time.monotonic()+20
        await self.cog._probe_tts_agent_health_once()
        self.assertEqual(self.cog._tts_edge_phone_unavailable_until, 0)
        self.assertEqual(self.cog._edge_phone_policy().level, level)
        self.assertGreater(self.cog._edge_phone_policy().remaining_seconds(), 0)
        self.assertTrue(self.cog._tts_agent_route_state()['ok'])

    async def test_wrong_worker_cannot_clear_transport_cooldown(self):
        deadline = time.monotonic()+20
        self.cog._tts_edge_phone_unavailable_until = deadline
        self.cog._fetch_tts_agent_light_health.return_value = health(worker_id='wrong')
        await self.cog._probe_tts_agent_health_once()
        self.assertEqual(self.cog._tts_edge_phone_unavailable_until, deadline)
        self.assertFalse(self.cog._tts_agent_route_state()['ok'])

    async def test_identity_mismatch_does_not_keep_recent_healthy_route(self):
        self.cog._tts_agent_set_route(route='worker', ok=True, reason='ready', worker_id='phone-one')
        self.cog._fetch_tts_agent_light_health.return_value = health(worker_id='wrong')
        await self.cog._probe_tts_agent_health_once()
        self.assertFalse(self.cog._tts_agent_route_state()['ok'])
        self.assertEqual(self.cog._tts_agent_route_state()['reason'], 'worker_identity_mismatch')

    async def test_new_generation_retires_only_after_verified_health(self):
        self.cog._tts_phone_verified_generation = 1
        self.cog._retire_phone_worker_http_generation = Mock()
        self.cog._fetch_tts_agent_light_health.return_value = health(generation=2)
        await self.cog._probe_tts_agent_health_once()
        self.cog._retire_phone_worker_http_generation.assert_called_once()
        self.assertEqual(self.cog._tts_phone_verified_generation, 2)

    async def test_recreated_transport_counter_recovers_without_bot_restart(self):
        self.cog._tts_phone_verified_generation = 3
        self.cog._tts_edge_phone_unavailable_until = time.monotonic()+20
        self.cog._retire_phone_worker_http_generation = Mock()
        self.cog._fetch_tts_agent_light_health.return_value = health(generation=1)
        await self.cog._probe_tts_agent_health_once()
        self.assertEqual(self.cog._tts_phone_verified_generation, 1)
        self.assertTrue(self.cog._tts_agent_route_state()['ok'])
        self.assertEqual(self.cog._tts_edge_phone_unavailable_until, 0)
        self.cog._retire_phone_worker_http_generation.assert_called_once()

    async def test_late_probe_response_does_not_revert_new_generation(self):
        late = asyncio.Future()
        self.cog._fetch_tts_agent_light_health.side_effect = [late, health(generation=2)]
        async def fetch(**kwargs):
            if not hasattr(fetch, 'started'):
                fetch.started = True
                return await late
            return health(generation=2)
        self.cog._fetch_tts_agent_light_health.side_effect = fetch
        first = asyncio.create_task(self.cog._probe_tts_agent_health_once())
        await asyncio.sleep(0)
        await self.cog._probe_tts_agent_health_once()
        late.set_result(health(generation=1))
        await first
        self.assertEqual(self.cog._tts_phone_verified_generation, 2)

    async def test_retirement_preserves_inflight_audio_and_new_pool(self):
        acquired = {object()}
        old = SimpleNamespace(closed=False, connector=SimpleNamespace(_acquired=acquired))
        async def close(): old.closed=True
        old.close=close
        self.cog._phone_worker_http_session=old
        self.cog._retire_phone_worker_http_generation()
        replacement=object(); self.cog._phone_worker_http_session=replacement
        await asyncio.sleep(0)
        self.assertFalse(old.closed)
        acquired.clear()
        await asyncio.sleep(0.07)
        self.assertTrue(old.closed)
        self.assertIs(self.cog._phone_worker_http_session, replacement)

    async def test_cache_uses_bulk_but_synthesis_keeps_interactive_port(self):
        class Response:
            status=200
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return False
            async def text(self): return '{"ok":true,"audio_format":"mp3"}'
        fast=SimpleNamespace(post=Mock(return_value=Response()))
        bulk=SimpleNamespace(post=Mock(return_value=Response()))
        self.cog._get_phone_worker_http_session=AsyncMock(return_value=fast)
        self.cog._get_phone_worker_bulk_http_session=AsyncMock(return_value=bulk)
        with patch.object(tts_audio.aiohttp, 'ClientTimeout', side_effect=lambda **k: SimpleNamespace(**k)):
            await self.cog._request_phone_worker_json(task='tts_cache_lookup',payload={},timeout_seconds=2,max_audio_mb=1)
            await self.cog._request_phone_worker_json(task='tts_agent_synthesize',payload={},timeout_seconds=2,max_audio_mb=1)
        self.assertEqual(bulk.post.call_args.args[0], 'http://127.0.0.1:18767/task')
        self.assertEqual(fast.post.call_args.args[0], 'http://127.0.0.1:18766/task')


class MusicPoolGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_transport_error_cannot_close_new_generation_pool(self):
        class Session:
            def __init__(self, acquired=()):
                self.closed=False
                self.connector=SimpleNamespace(_acquired=set(acquired))
            async def close(self): self.closed=True
        old=Session([object()]); replacement=Session()
        loop=asyncio.get_running_loop()
        with patch.multiple(transporte_http, _SESSAO=old, _SESSAO_LOOP=loop, _SESSAO_GENERATION=1,
                            _RETIRED_SESSIONS=set(), _SESSAO_LOCK=None, _SESSAO_LOCK_LOOP=None), \
             patch.object(saude, 'geracao_ssh_verificada', return_value=2), \
             patch.object(transporte_http.aiohttp, 'TCPConnector', return_value=object()), \
             patch.object(transporte_http.aiohttp, 'ClientSession', return_value=replacement):
            result=await transporte_http.obter_sessao_http()
            self.assertIs(result, replacement)
            self.assertFalse(old.closed)
            await transporte_http.invalidar_sessao_http(old)
            self.assertIs(transporte_http._SESSAO, replacement)
            old.connector._acquired.clear()
            await asyncio.sleep(.07)
            self.assertTrue(old.closed)
            self.assertFalse(replacement.closed)
            await transporte_http.fechar_sessao_http()


if __name__ == '__main__':
    unittest.main()
