"""Real HTTP framing and verified in-memory CD stems, without Discord/native banks."""
from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import hashlib
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import socket
import struct
import threading
import time
import unittest
import wave

import aiohttp

from test_cd_arrangement_audio_contract import QueueItem, production_nodes
from cogs.tts.arrangement_audio import frozen_arrangement
from cogs.tts.performance import performance_hash
from cogs.tts.ttv import (
    DEFAULT_VOICE_ID, normalize_language, normalize_pitch, normalize_speech_rate,
    resolve_preferences, worker_voice_details,
)

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / 'deploy/termux/phone-worker/phone_worker.py'


def worker_boundary():
    source = ast.parse(PHONE.read_text())
    klass = next(node for node in source.body if isinstance(node, ast.ClassDef)
                 and node.name == 'WorkerHandler')
    names = {'handle_one_request', 'parse_request', 'send_response_only', 'end_headers',
             '_read_json', '_authorized', '_require_auth', 'token', 'max_body_bytes'}
    methods = [node for node in klass.body if getattr(node, 'name', '') in names]
    protocol = next(node for node in klass.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'protocol_version'
                            for target in node.targets))
    selected = ast.ClassDef(name='WorkerBoundary', bases=[ast.Name(id='BaseHTTPRequestHandler', ctx=ast.Load())],
                           keywords=[], body=[protocol, *methods], decorator_list=[])
    helpers = [node for node in source.body if getattr(node, 'name', '')
               in {'_json_response', '_audio_response', '_header_ascii', '_error'}]
    namespace = dict(globals(), DEFAULT_MAX_BODY_MB=1,
                     _short_text=lambda value, **kw: str(value)[:kw.get('limit', 180)])
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), *helpers, selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(PHONE), 'exec'), namespace)
    return namespace


boundary = worker_boundary()


def waveform():
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as audio:
        audio.setparams((1, 2, 48000, 0, 'NONE', ''))
        audio.writeframes(struct.pack('<h', 1200) * 24000)
    return raw.getvalue()


WAV = waveform()


class FixtureHandler(boundary['WorkerBoundary']):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == '/eof-stream':
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            self.wfile.write(b'complete-stream')
            return
        boundary['_json_response'](self, 200, {'ok': True})

    def do_POST(self):
        if not self._require_auth():
            return
        body = self._read_json()
        if body is None:
            return
        self.server.requests.append((self.client_address[1], body))
        meta = dict(ok=True, audio_format='wav', engine='teto', selected_engine='teto',
            sha256=hashlib.sha256(WAV).hexdigest(), normalization_gain=0.8,
            ttv_voice_id='kasane-teto', ttv_language='pt-BR', ttv_pitch_semitones='+0.0',
            ttv_speech_rate=1.0, ttv_performance_hash=body.get('ttv_performance_hash', ''),
            worker_version='fixture', timing_ms={'worker_synth': 1.0})
        meta.update(self.server.alterations)
        if self.server.hold.is_set():
            self.server.entered.set()
            self.server.release.wait(3)
        if self.server.json_mode:
            boundary['_json_response'](self, 200, dict(meta, data_b64=base64.b64encode(WAV).decode()))
        else:
            boundary['_audio_response'](self, 200, WAV, meta)


namespace = dict(globals(),
    TTS_TETO_MAX_TEXT_LENGTH=180, TTS_TETO_MAX_AUDIO_MB=8,
    TTS_TETO_DEFAULT_PITCH_SEMITONES=0, TTS_TETO_WORKER_TIMEOUT_SECONDS=120,
    TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS=30, TTS_WORKER_AGENT_MAX_TEXT_LENGTH=1200,
    TTS_WORKER_AGENT_MAX_AUDIO_MB=8, TTS_WORKER_AGENT_PREFERRED_ENGINE='teto',
    TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS=0, TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS=.01,
    TTS_WORKER_AGENT_RAW_AUDIO_ENABLED=True, PHONE_WORKER_TOKEN='local-memory-token',
    GTTS_DEFAULT_LANGUAGE='pt-br', TTS_PIPER_MODEL_NAME='unused', TTS_DEBUG_LOGS=False,
    logger=logging.getLogger(__name__))
methods = production_nodes('cogs/tts/audio.py', {
    '_ttv_item_preferences', '_ttv_performance_payload', 'TTVSingingError',
    '_tts_agent_payload_for_item', '_generate_tts_agent_worker_file', '_generate_tts_agent_worker_bytes',
    '_request_phone_worker_raw_audio', '_request_phone_worker_tts_audio',
    '_worker_header_value', '_parse_header_float', '_parse_header_bool',
    '_normalize_worker_audio_format', '_decode_worker_audio_payload',
}, namespace)
Transport = type('Transport', (), {key: value for key, value in methods.items()
                                  if key.startswith('_') and not key.startswith('_ttv_')})


class Probe(Transport):
    def __init__(self, server, session):
        self.server, self.session, self.metrics = server, session, {}
        self.failures = []

    def _tts_agent_route_available(self):
        return True

    def _tts_agent_route_state(self):
        return {'ttv_performance_protocol': 1, 'ttv_personal_settings': True,
                'ttv_languages': ['pt-BR'], 'ttv_voices': [{
                    'id': 'kasane-teto', 'ready': True, 'available': True,
                    'singing_available': True, 'languages': ['pt-BR']}]}

    def _phone_worker_tts_base_url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    async def _get_phone_worker_http_session(self):
        return self.session

    def _cache_key(self, item):
        return 'verified-fixture'

    def _get_metrics_store(self):
        return self.metrics

    def _record_tts_agent_synth_success(self, **_values):
        pass

    def _mark_tts_agent_synth_failure(self, error):
        self.failures.append(error)

    def _make_runtime_temp_file(self, **_values):
        raise AssertionError('The in-memory CD stem must not create a file')

    def _is_tts_agent_transient_busy_error(self, _error):
        return False


def item():
    score = {'version': 1, 'track_index': 0, 'duration_ms': 500, 'lyrics_source': 'message',
             'notes': [{'midi_note': 60, 'start_ms': 0, 'duration_ms': 500, 'velocity': 90}]}
    result = QueueItem(1, 2, 3, 'a', 'teto', 'kasane-teto', 'pt-BR', '1.0', '+0Hz', ttv_score=score)
    result._cd_progressive_request = True
    return result


class FramingWithoutNetworkTests(unittest.TestCase):
    """Exercise the HTTP parser/pipelining even when loopback is sandboxed."""

    def exchange(self, data):
        class Socket:
            def __init__(self):
                self.incoming, self.outgoing = io.BytesIO(data), bytearray()

            def makefile(self, *_args):
                return self.incoming

            def sendall(self, part):
                self.outgoing.extend(part)

            def settimeout(self, _timeout):
                pass

        connection = Socket()
        server = type('Server', (), {})()
        server.worker_token, server.max_body_bytes = 'local-memory-token', 1024 * 1024
        server.requests, server.alterations, server.json_mode = [], {}, False
        server.hold, server.entered, server.release = (threading.Event() for _ in range(3))
        FixtureHandler(connection, ('127.0.0.1', 42), server)
        return bytes(connection.outgoing), server

    def post(self, *, auth=True, headers=b''):
        token = b'Authorization: Bearer local-memory-token\r\n' if auth else b''
        return (b'POST /tts-agent/synthesize.raw HTTP/1.1\r\nHost: fixture\r\n' + token
                + b'Content-Length: 2\r\n' + headers + b'\r\n{}')

    def test_pipelined_stems_are_individually_framed_without_closing(self):
        raw, server = self.exchange(self.post() + self.post())
        self.assertEqual(raw.count(b'HTTP/1.1 200 OK\r\n'), 2)
        self.assertEqual(raw.count(f'Content-Length: {len(WAV)}\r\n'.encode()), 2)
        self.assertNotIn(b'Connection: close', raw)
        self.assertEqual(len(server.requests), 2)

    def test_expect_continue_is_interim_and_keeps_the_body_boundary(self):
        raw, server = self.exchange(self.post(headers=b'Expect: 100-continue\r\n') + self.post())
        self.assertEqual(raw.count(b'HTTP/1.1 100 Continue\r\n'), 1)
        self.assertEqual(raw.count(b'HTTP/1.1 200 OK\r\n'), 2)
        self.assertNotIn(b'Connection: close', raw)
        self.assertEqual(len(server.requests), 2)

    def test_unread_rejected_post_does_not_parse_its_body_as_another_request(self):
        raw, server = self.exchange(self.post(auth=False) + self.post())
        self.assertEqual(raw.count(b'HTTP/1.1 '), 1)
        self.assertIn(b'HTTP/1.1 403 Forbidden\r\n', raw)
        self.assertIn(b'Connection: close', raw)
        self.assertFalse(server.requests)

    def test_eof_stream_is_closed_before_pipelined_request(self):
        raw, server = self.exchange(b'GET /eof-stream HTTP/1.1\r\nHost: fixture\r\n\r\n' + self.post())
        self.assertEqual(raw.count(b'HTTP/1.1 '), 1)
        self.assertIn(b'Connection: close', raw)
        self.assertTrue(raw.endswith(b'complete-stream'))
        self.assertFalse(server.requests)

    def test_duplicate_content_length_is_rejected_before_rendering(self):
        raw, server = self.exchange(self.post(headers=b'Content-Length: 2\r\n') + self.post())
        self.assertEqual(raw.count(b'HTTP/1.1 '), 1)
        self.assertIn(b'HTTP/1.1 400 ', raw)
        self.assertIn(b'Connection: close', raw)
        self.assertFalse(server.requests)


class BytesGenerationWithoutNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.probe, self.source, self.requests, self.alterations = Probe(None, None), item(), [], {}

        async def request(**options):
            self.requests.append(options)
            if 'error' in self.alterations:
                raise self.alterations['error']
            result = dict(ok=True, audio_format='wav', engine='teto', selected_engine='teto',
                raw_audio=bytearray(WAV), normalization_gain=.8, ttv_voice_id='kasane-teto',
                ttv_language='pt-BR', ttv_pitch_semitones='+0.0', ttv_speech_rate=1.0,
                ttv_performance_hash=options['payload']['ttv_performance_hash'])
            result.update(self.alterations)
            return result

        self.probe._request_phone_worker_tts_audio = request

    async def test_direct_bytes_keep_identity_and_gain_without_creating_file(self):
        raw, meta = await self.probe._generate_tts_agent_worker_bytes(self.source)
        self.assertIs(type(raw), bytes)
        self.assertEqual(raw, WAV)
        self.assertEqual(meta['normalization_gain'], .8)
        self.assertEqual(meta['ttv_performance_hash'], self.source.ttv_performance_hash)
        self.assertFalse({'raw_audio', 'audio_bytes', 'data_b64'} & meta.keys())
        self.assertFalse(self.requests[0]['stream_to_file'])
        self.assertEqual(self.requests[0]['payload']['cache_mode'], 'bypass')

    async def test_invalid_voice_is_rejected_before_stem_cache_publication(self):
        self.alterations['ttv_voice_id'] = 'hatsune-miku'
        with self.assertRaisesRegex(RuntimeError, 'ajustes de TTV'):
            await self.probe._generate_tts_agent_worker_bytes(self.source)
        self.assertFalse(hasattr(self.source, '_tts_agent_render_metadata'))

    async def test_performance_mismatch_is_rejected_before_stem_cache_publication(self):
        self.alterations['ttv_performance_hash'] = 'incorrect'
        with self.assertRaisesRegex(RuntimeError, 'notas ou efeitos'):
            await self.probe._generate_tts_agent_worker_bytes(self.source)
        self.assertFalse(hasattr(self.source, '_tts_agent_render_metadata'))

    async def test_cancelled_generation_never_retries_or_marks_route_failed(self):
        self.alterations['error'] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.probe._generate_tts_agent_worker_bytes(self.source)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.probe.failures)


class MemoryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.server.daemon_threads = True
        self.server.worker_token = 'local-memory-token'
        self.server.max_body_bytes = 1024 * 1024
        self.server.requests, self.server.alterations = [], {}
        self.server.json_mode = False
        self.server.hold, self.server.entered, self.server.release = (threading.Event() for _ in range(3))
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.session = aiohttp.ClientSession()
        self.probe = Probe(self.server, self.session)
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)

    async def asyncTearDown(self):
        self.server.release.set()
        await self.session.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    async def test_two_verified_stems_reuse_one_connection_and_never_touch_disk(self):
        first = item()
        for source in (first, item()):
            raw, meta = await self.probe._generate_tts_agent_worker_bytes(source)
            self.assertEqual(raw, WAV)
            self.assertEqual(meta['normalization_gain'], .8)
            self.assertEqual(meta['ttv_performance_hash'], source.ttv_performance_hash)
            self.assertFalse({'raw_audio', 'audio_bytes', 'data_b64'} & meta.keys())
            self.assertEqual(source._tts_agent_render_metadata, meta)
        self.assertEqual(len({port for port, _ in self.server.requests}), 1)
        self.assertTrue(all(body['cache_mode'] == 'bypass' for _, body in self.server.requests))

    async def test_regular_ttv_preserves_prefer_and_background_priority(self):
        source = item()
        source._cd_progressive_request = False
        source._cd_background = True
        payload = self.probe._tts_agent_payload_for_item(source)
        self.assertEqual(payload['cache_mode'], 'prefer')
        self.assertEqual(payload['priority'], 'background')

    async def test_json_audio_compatibility_keeps_normalization_metadata(self):
        self.server.json_mode = True
        raw, meta = await self.probe._generate_tts_agent_worker_bytes(item())
        self.assertEqual(raw, WAV)
        self.assertEqual(meta['normalization_gain'], .8)
        self.assertNotIn('data_b64', meta)

    async def test_wrong_voice_or_performance_cannot_enter_stem_cache(self):
        for alteration in ({'ttv_voice_id': 'hatsune-miku'}, {'ttv_performance_hash': 'wrong'}):
            self.server.alterations = alteration
            with self.subTest(alteration=alteration), self.assertRaises(RuntimeError):
                await self.probe._generate_tts_agent_worker_bytes(item())

    async def test_corrupt_audio_hash_is_rejected_before_return(self):
        self.server.alterations = {'sha256': '0' * 64}
        with self.assertRaisesRegex(RuntimeError, 'sha256'):
            await self.probe._generate_tts_agent_worker_bytes(item())

    async def test_cancelled_request_propagates_without_retry_or_temp_file(self):
        self.server.hold.set()
        task = asyncio.create_task(self.probe._generate_tts_agent_worker_bytes(item()))
        for _ in range(100):
            if self.server.entered.is_set():
                break
            await asyncio.sleep(.005)
        self.assertTrue(self.server.entered.is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(self.probe.failures)
        self.server.release.set()

    async def test_unknown_length_stream_closes_after_complete_body(self):
        async with self.session.get(self.probe._phone_worker_tts_base_url() + '/eof-stream') as response:
            self.assertEqual(response.version, aiohttp.HttpVersion11)
            self.assertEqual(response.headers['Connection'], 'close')
            self.assertEqual(await response.read(), b'complete-stream')

    async def test_rejected_unread_post_body_closes_connection(self):
        async with self.session.post(self.probe._phone_worker_tts_base_url() + '/tts-agent/synthesize.raw',
                data=b'{"not":"consumed"}', headers={'Authorization': 'Bearer invalid'}) as response:
            self.assertEqual(response.status, 403)
            self.assertEqual(response.headers['Connection'], 'close')
            self.assertFalse((await response.json())['ok'])
        self.assertFalse(self.server.requests)

    async def test_ambiguous_length_or_chunked_request_cannot_be_reused(self):
        for headers in (b'Content-Length: 2\r\nContent-Length: 2\r\n',
                        b'Transfer-Encoding: chunked\r\n'):
            def send():
                with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2) as client:
                    client.sendall(b'POST /tts-agent/synthesize.raw HTTP/1.1\r\nHost: local\r\n' + headers + b'\r\n{}')
                    output = bytearray()
                    while True:
                        chunk = client.recv(65536)
                        if not chunk:
                            return bytes(output)
                        output.extend(chunk)
            output = await asyncio.to_thread(send)
            self.assertIn(b' 400 ', output)
            self.assertIn(b'Connection: close', output)
        self.assertFalse(self.server.requests)


if __name__ == '__main__':
    unittest.main()
