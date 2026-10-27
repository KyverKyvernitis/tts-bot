"""Native PCM framing, authenticated HTTP and verified completion receipts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import aiohttp

from cogs.tts.cd_worker_stream import (
    SingingPCMProtocolError, read_pcm_stream, request_pcm_stream,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_pcm_worker_transport',
    ROOT / 'deploy/termux/phone-worker/tts_transport.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def payload():
    return {'engine': 'teto', 'text': 'a', 'ttv_voice_id': 'kasane-teto',
            'ttv_language': 'pt-BR', 'ttv_performance_hash': 'a' * 64,
            'ttv_score': {'version': 1, 'duration_ms': 100, 'notes': []},
            'cache_mode': 'prefer', 'max_audio_bytes': 1024 * 1024,
            'timeout_seconds': 2}


def start_metadata():
    return {'voice_id': 'kasane-teto', 'reading_language': 'pt-BR',
            'ttv_performance_hash': 'a' * 64, 'sample_rate': 44100,
            'channels': 1, 'sample_width_bytes': 2, 'normalization_gain': 1.0,
            'midi_preroll_ms': 0, 'expected_frames': 4410, 'duration_seconds': .1}


PCM = b'\x12\x03' * 4410


def wav(pcm=PCM):
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setparams((1, 2, 44100, 0, 'NONE', ''))
        target.writeframes(pcm)
    return output.getvalue()


def receipt(pcm=PCM):
    return {'ok': True, 'engine': 'teto', 'ttv_voice_id': 'kasane-teto',
            'ttv_language': 'pt-BR', 'ttv_performance_hash': 'a' * 64,
            'normalization_gain': 1.0, 'sha256': hashlib.sha256(wav(pcm)).hexdigest(),
            'pcm_bytes': len(pcm), 'pcm_sha256': hashlib.sha256(pcm).hexdigest()}


def record(kind, value):
    data = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode()
    return kind + struct.pack('!I', len(data)) + data


def wire(*, meta=None, end=None, pcm=PCM):
    return record(b'M', start_metadata() if meta is None else meta) + record(b'P', pcm) + record(
        b'E', receipt(pcm) if end is None else end)


class BytesReader:
    def __init__(self, data):
        self.data, self.offset, self.requests = data, 0, []

    async def readexactly(self, number):
        self.requests.append(number)
        data = self.data[self.offset:self.offset + number]
        self.offset += len(data)
        if len(data) != number:
            raise asyncio.IncompleteReadError(data, number)
        return data


class NativePCMReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)

    async def read(self, data, callback=None, *, maximum=1024 * 1024):
        return await read_pcm_stream(BytesReader(data), payload(), callback or (lambda *_: None),
                                     maximum=maximum)

    async def test_complete_stream_returns_exact_wav_after_ordered_callbacks(self):
        events = []
        raw, meta = await self.read(wire(), lambda pcm, config: events.append((pcm, config)))
        self.assertEqual(raw, wav())
        self.assertEqual([data for data, _ in events], [b'', PCM])
        self.assertTrue(all(config == start_metadata() for _, config in events))
        self.assertEqual(meta['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(meta['native_pcm_stream'])

    async def test_metadata_is_checked_before_any_callback(self):
        bad_values = {'sample_rate': (48000, True), 'channels': (2, True),
                      'sample_width_bytes': (4, True), 'normalization_gain': (.8, True),
                      'voice_id': ('hatsune-miku',), 'reading_language': ('en-US',),
                      'ttv_performance_hash': ('b' * 64,),
                      'midi_preroll_ms': (-1, 351, float('nan'), True),
                      'expected_frames': (4411 + 2, 0, True, 4410.0)}
        for key, values in bad_values.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    meta, callback = start_metadata(), Mock()
                    meta[key] = value
                    with self.assertRaises(SingingPCMProtocolError):
                        await self.read(wire(meta=meta), callback)
                    callback.assert_not_called()

    async def test_receipt_binds_pcm_wav_voice_language_and_performance(self):
        changes = {'ok': False, 'pcm_bytes': len(PCM) - 2, 'pcm_sha256': '0' * 64,
                   'sha256': '0' * 64, 'ttv_voice_id': 'hatsune-miku',
                   'ttv_language': 'en-US', 'ttv_performance_hash': 'b' * 64,
                   'normalization_gain': .9}
        for key, value in changes.items():
            with self.subTest(key=key):
                end = receipt()
                end[key] = value
                with self.assertRaises(SingingPCMProtocolError):
                    await self.read(wire(end=end))

    async def test_truncated_streams_never_produce_a_verified_result(self):
        complete = wire()
        metadata_end = len(record(b'M', start_metadata()))
        for cut in (0, 2, 5, metadata_end - 1, metadata_end,
                    metadata_end + 9, len(complete) - 1):
            with self.subTest(cut=cut), self.assertRaises(SingingPCMProtocolError):
                await self.read(complete[:cut])

    async def test_bad_record_order_and_duplicate_end_are_rejected(self):
        meta = record(b'M', start_metadata())
        bad_streams = (record(b'P', PCM), record(b'E', receipt()), meta + meta,
                      meta + record(b'E', receipt()), meta + record(b'P', b'\x01'),
                      meta + record(b'P', PCM + b'\0\0'),
                      wire() + record(b'E', receipt()), wire() + b'x',
                      record(b'M', b'not JSON'))
        for number, data in enumerate(bad_streams):
            with self.subTest(number=number), self.assertRaises(SingingPCMProtocolError):
                await self.read(data)

    async def test_malformed_lengths_are_rejected_without_reading_record_bodies(self):
        for kind, size in ((b'M', 0), (b'M', 16385), (b'P', 32769),
                           (b'E', 65537), (b'X', 1)):
            content = BytesReader(kind + struct.pack('!I', size))
            with self.subTest(kind=kind, size=size), self.assertRaises(SingingPCMProtocolError):
                await read_pcm_stream(content, payload(), lambda *_: None, maximum=1024 * 1024)
            self.assertEqual(content.requests, [5])

    async def test_planned_audio_limit_is_enforced_before_callback(self):
        callback = Mock()
        with self.assertRaises(SingingPCMProtocolError):
            await self.read(wire(), callback, maximum=len(PCM) + 43)
        callback.assert_not_called()


class DirectHandler:
    max_output_bytes = 1024 * 1024

    def __init__(self, synthesize):
        self._task_tts_agent_synthesize = synthesize
        self.wfile = io.BytesIO()
        self.connection = SimpleNamespace(settimeout=Mock())
        self.send_response, self.send_header, self.end_headers = Mock(), Mock(), Mock()


class NativePCMServerValidationTests(unittest.TestCase):
    def test_invalid_body_does_not_start_renderer_or_send_headers(self):
        for key, value in (('engine', 'edge'), ('ttv_score', None),
                           ('max_audio_bytes', 100), ('timeout_seconds', float('nan'))):
            body = payload()
            body[key] = value
            handler = DirectHandler(Mock())
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.serve_singing_stream(handler, body, SimpleNamespace())
            handler._task_tts_agent_synthesize.assert_not_called()
            handler.send_response.assert_not_called()

    def test_invalid_native_metadata_never_sends_success_headers(self):
        for key, value in (('voice_id', 'hatsune-miku'), ('reading_language', 'en-US'),
                           ('ttv_performance_hash', 'b' * 64), ('expected_frames', 4500),
                           ('sample_rate', True), ('channels', True),
                           ('sample_width_bytes', True), ('normalization_gain', True),
                           ('midi_preroll_ms', float('nan'))):
            meta = start_metadata()
            meta[key] = value
            def synthesize(_request, *, raw_response, emit_pcm):
                emit_pcm(b'', meta)
                return receipt()
            handler = DirectHandler(synthesize)
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.serve_singing_stream(handler, payload(), SimpleNamespace())
            handler.send_response.assert_not_called()
            self.assertEqual(handler.wfile.getvalue(), b'')


class NativeHTTPFixture:
    def __init__(self, *, fail_after_prefix=False, wrong_content=False):
        self.release = threading.Event()
        self.prefix = threading.Event()
        self.completed = threading.Event()
        self.bodies = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            max_output_bytes = 1024 * 1024

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.bodies.append(body)
                if self.headers.get('Authorization') != 'Bearer test-phone-token':
                    self.send_response(403)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if wrong_content:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'{}')
                    return
                worker.serve_singing_stream(self, body, SimpleNamespace())

            def _task_tts_agent_synthesize(self, body, *, raw_response, emit_pcm):
                emit_pcm(b'', start_metadata())
                emit_pcm(PCM[:len(PCM) // 2], start_metadata())
                fixture.prefix.set()
                if not fixture.release.wait(3):
                    raise TimeoutError('fixture release not signalled')
                if fail_after_prefix:
                    raise RuntimeError('native renderer stopped after prefix')
                emit_pcm(PCM[len(PCM) // 2:], start_metadata())
                fixture.completed.set()
                return {**receipt(), 'raw_audio': wav(), 'audio_path': '/private/never-on-wire.wav'}

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class NativePCMHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)

    def cog(self, fixture, session):
        async def get_session():
            return session
        return SimpleNamespace(_tts_agent_route_state=lambda: {'ttv_pcm_stream_protocol': 1},
            _tts_agent_route_available=lambda: True, _tts_agent_payload_for_item=lambda _: payload(),
            _phone_worker_tts_base_url=lambda: fixture.base,
            _get_phone_worker_http_session=get_session, _get_metrics_store=lambda: {},
            _record_tts_agent_synth_success=Mock())

    async def test_real_http_delivers_prefix_before_native_render_finishes(self):
        fixture = NativeHTTPFixture()
        self.addCleanup(fixture.close)
        prefix_received, events = asyncio.Event(), []
        loop = asyncio.get_running_loop()
        def on_pcm(pcm, meta):
            events.append((pcm, meta))
            if pcm:
                loop.call_soon_threadsafe(prefix_received.set)
        async with aiohttp.ClientSession() as session:
            cog, item = self.cog(fixture, session), SimpleNamespace()
            with patch.dict(sys.modules, {'cogs.tts.audio': SimpleNamespace(PHONE_WORKER_TOKEN='test-phone-token')}):
                task = asyncio.create_task(request_pcm_stream(cog, item, on_pcm))
                await asyncio.wait_for(prefix_received.wait(), 2)
                self.assertTrue(fixture.prefix.is_set())
                self.assertFalse(fixture.completed.is_set())
                self.assertFalse(task.done())
                fixture.release.set()
                raw, meta = await asyncio.wait_for(task, 3)
        self.assertEqual(raw, wav())
        self.assertEqual(b''.join(chunk for chunk, _ in events), PCM)
        self.assertEqual(meta['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotIn('raw_audio', meta)
        self.assertNotIn('audio_path', meta)
        self.assertEqual(fixture.bodies[0]['cache_mode'], 'bypass')
        self.assertEqual(item._tts_agent_selected_engine, 'teto')
        cog._record_tts_agent_synth_success.assert_called_once()

    async def test_disconnect_after_prefix_never_records_verified_success(self):
        fixture = NativeHTTPFixture(fail_after_prefix=True)
        self.addCleanup(fixture.close)
        events = []
        prefix_received = asyncio.Event()
        loop = asyncio.get_running_loop()
        def receive(pcm, _metadata):
            events.append(pcm)
            if pcm:
                loop.call_soon_threadsafe(prefix_received.set)
        async with aiohttp.ClientSession() as session:
            cog, item = self.cog(fixture, session), SimpleNamespace()
            with patch.dict(sys.modules, {'cogs.tts.audio': SimpleNamespace(PHONE_WORKER_TOKEN='test-phone-token')}):
                task = asyncio.create_task(request_pcm_stream(cog, item, receive))
                await asyncio.wait_for(prefix_received.wait(), 2)
                fixture.release.set()
                with self.assertRaises((SingingPCMProtocolError, aiohttp.ClientPayloadError)):
                    await task
        self.assertTrue(any(events))
        self.assertFalse(hasattr(item, '_tts_agent_render_metadata'))
        cog._record_tts_agent_synth_success.assert_not_called()

    async def test_wrong_content_type_is_rejected_before_pcm_callback(self):
        fixture = NativeHTTPFixture(wrong_content=True)
        self.addCleanup(fixture.close)
        callback = Mock()
        async with aiohttp.ClientSession() as session:
            cog = self.cog(fixture, session)
            with patch.dict(sys.modules, {'cogs.tts.audio': SimpleNamespace(PHONE_WORKER_TOKEN='test-phone-token')}):
                with self.assertRaises(SingingPCMProtocolError):
                    await request_pcm_stream(cog, SimpleNamespace(), callback)
        callback.assert_not_called()
        cog._record_tts_agent_synth_success.assert_not_called()


if __name__ == '__main__':
    unittest.main()
