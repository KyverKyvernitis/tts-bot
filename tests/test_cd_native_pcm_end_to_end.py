"""Real renderer, worker admission, HTTP framing and bot PCM receipt together."""
from __future__ import annotations

import asyncio
import hashlib
from http.server import ThreadingHTTPServer
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import aiohttp

from cogs.tts.cd_worker_stream import request_pcm_stream
from cogs.tts.performance import performance_hash
from test_cd_transport_memory import Probe, item
import test_ttv_latency_renderer as fixtures
from test_ttv_worker_pcm_stream import score


ROOT = Path(__file__).resolve().parents[1]


class NativePCMEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)

    async def test_real_render_crosses_http_before_its_last_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = fixtures.RenderCancellationTests().singing_renderer(Path(directory))
            name = '_cd_native_e2e_worker'
            spec = importlib.util.spec_from_file_location(name,
                ROOT / 'deploy/termux/phone-worker/phone_worker.py')
            worker = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {name: worker}):
                spec.loader.exec_module(worker)
                worker._get_ttv_renderer = lambda _voice: renderer
                worker._teto_backend = lambda: 'utau'
                worker._teto_dependency_snapshot_fast = lambda: {
                    'teto_tts': True, 'teto': {'ready': True, 'enabled': True,
                    'resources': {'ok': True}, 'ttv_performance_protocol': 2}}

                class Handler(worker.WorkerHandler):
                    def _ensure_tts_piper_turbo_allowed(self):
                        return ['cache-worker'], ['cache-worker', 'tts-synth']
                    def log_message(self, *_args):
                        pass

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                server.worker_token = 'local-memory-token'
                server.max_output_bytes = 8 * 1024 * 1024
                server.max_body_bytes = 1024 * 1024
                server.job_timeout = 120
                thread = threading.Thread(target=server.serve_forever,
                                          kwargs={'poll_interval': .01}, daemon=True)
                thread.start()
                prefix = asyncio.Event()
                first_received = threading.Event()
                calls, pieces = [], []
                native = renderer._resample_note
                loop = asyncio.get_running_loop()

                def resample(**arguments):
                    calls.append(arguments['note'])
                    if len(calls) == 3:
                        if not first_received.wait(3):
                            raise RuntimeError('HTTP client did not receive the sealed prefix')
                    return native(**arguments)

                def consume(pcm, _metadata):
                    pieces.append(pcm)
                    if pcm:
                        first_received.set()
                        loop.call_soon_threadsafe(prefix.set)

                selected = item()
                selected.text = 'a a a'
                selected.ttv_score = score()
                selected.ttv_performance_hash = performance_hash(text=selected.text,
                    score=selected.ttv_score, segments=selected.ttv_segments)
                try:
                    async with aiohttp.ClientSession() as session:
                        probe = Probe(server, session)
                        old_route = probe._tts_agent_route_state
                        probe._tts_agent_route_state = lambda: {
                            **old_route(), 'ttv_pcm_stream_protocol': 1}
                        with patch.dict(sys.modules, {'cogs.tts.audio': SimpleNamespace(
                                PHONE_WORKER_TOKEN='local-memory-token')}), \
                             patch.object(renderer, '_resample_note', side_effect=resample):
                            raw, metadata = await asyncio.wait_for(
                                request_pcm_stream(probe, selected, consume), 10)
                    self.assertTrue(prefix.is_set())
                    self.assertEqual(len(calls), 3)
                    with wave.open(io.BytesIO(raw), 'rb') as audio:
                        self.assertEqual(b''.join(pieces), audio.readframes(audio.getnframes()))
                    self.assertEqual(metadata['sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(metadata['normalization_gain'], 1.0)
                    self.assertFalse(worker._HEAVY_RESOURCE_LOCK.locked())
                    self.assertEqual(worker._TTS_AGENT_ACTIVE, 0)
                    self.assertEqual(worker._TTS_AGENT_TOTAL, 1)
                    self.assertEqual(worker._TTS_AGENT_FAILED, 0)
                finally:
                    first_received.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(3)


if __name__ == '__main__':
    unittest.main()
