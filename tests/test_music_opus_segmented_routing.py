"""Actual multipart source/HTTP bridge with local stream and network boundaries.

No external requests are made. Packet/decoder tests live in the native packet
and pipeline suites; these exercise consumed-position recovery, bounded HTTP
backpressure, verified resolver ownership and cancellation.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_music_opus_packets import buffer, FakeDecoder, fixture
from tests.test_music_opus_segmented_packets import segmented_fixture


ROOT = Path(__file__).resolve().parents[1]


def load_routes():
    name = 'cogs.musica.runtime_telefone.agente.segmented_routing_test'
    spec = importlib.util.spec_from_file_location(name,
        ROOT / 'cogs/musica/runtime_telefone/agente/audio_segmentado.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict('sys.modules', {
            'discord': SimpleNamespace(AudioSource=object),
            'cogs.musica.runtime_telefone.agente.buffer_pcm': buffer}):
        spec.loader.exec_module(module)
    return module


routes = load_routes()


async def worker(function, *args):
    """Use an owned bounded worker, independent of the sandbox's executor."""
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    def run():
        try:
            result = function(*args)
        except BaseException as exc:
            loop.call_soon_threadsafe(completed.set_exception, exc)
        else:
            loop.call_soon_threadsafe(completed.set_result, result)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not completed.done():
        if time.monotonic() >= deadline:
            raise TimeoutError('owned test worker did not complete')
        # Keep a timer on this restricted sandbox's selector while the real
        # worker and HTTP pump exchange wakeups. Production does not poll here.
        await asyncio.sleep(.005)
    return completed.result()


class LocalStreams:
    def __init__(self, parts):
        self.parts = parts
        self.streams = []
        self.opened = []
        self.prefetched = []
        self.active = False
        self.closed = False

    def __call__(self, index):
        self.opened.append(index)
        stream = io.BytesIO(self.parts[index])
        self.streams.append(stream)
        return stream

    def prefetch(self, index):
        self.prefetched.append(index)

    def activate_playback(self):
        self.active = True

    def cleanup(self):
        self.closed = True
        for stream in self.streams:
            stream.close()


class ReadyFallback:
    last_read_had_audio = True

    def __init__(self):
        self.closed = False
        self.waited = False

    async def wait_ready(self, **kwargs):
        self.waited = True

    def read(self):
        return b'\x66' * 3840 if not self.closed else b''

    def cleanup(self):
        self.closed = True


class SegmentedRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.packets, self.parts, self.segments = segmented_fixture()
        self.fallback = ReadyFallback()
        self.create_fallback = mock.AsyncMock(return_value=self.fallback)
        self.streams = LocalStreams(self.parts)
        self.sources = []

    async def asyncTearDown(self):
        for source in self.sources:
            source.cleanup()
        await asyncio.sleep(.01)

    def make_source(self, **kwargs):
        source = routes.ArchiveOpusPCMSource(
            segments=self.segments, resolve_segment=mock.AsyncMock(),
            create_fallback=self.create_fallback, loop=asyncio.get_running_loop(),
            stream_factory=self.streams, decoder_factory=FakeDecoder,
            max_frames=75, stall_seconds=.2, **kwargs)
        self.sources.append(source)
        return source

    async def test_valid_parts_keep_one_decoder_and_original_packet_for_each_pcm_frame(self):
        source = self.make_source()
        await source.wait_ready(timeout=1, min_frames=75)
        decoder = source.source.decoder
        packets, frames = [], []
        while frame := source.read():
            packets.append(source.last_opus_packet)
            frames.append(frame)
        self.assertEqual(packets, self.packets)
        self.assertEqual(frames, [bytes([index]) * 3840 for index in range(1, 13)])
        self.assertIs(source.source.decoder, decoder)
        self.create_fallback.assert_not_awaited()
        self.assertEqual(source.audio_buffer_metrics()['archive_preroll_packets_skipped'], 8)

    async def test_late_invalid_prefix_drains_valid_frames_then_resumes_at_consumed_position(self):
        self.parts[1] = fixture([b'\xfc\x01\xff', *self.packets[2:9]], preskip=3840,
                                end_granule=7680)
        events = []
        source = self.make_source(on_fallback=events.append)
        await source.wait_ready(timeout=1, min_frames=75)
        for index in range(5):
            self.assertEqual(source.read(), bytes([index + 1]) * 3840)
            self.assertEqual(source.last_opus_packet, self.packets[index])
        self.assertEqual(source.read(), bytes(3840))
        self.assertFalse(source.last_read_had_audio)
        self.assertIsNone(source.last_opus_packet)
        # Subtract the original Opus lookahead before seeking ordinary PCM.
        expected_offset = (5 * 960 - 312) / 48000
        await asyncio.sleep(.02)
        self.create_fallback.assert_awaited_once_with(expected_offset)
        self.assertEqual(events, [expected_offset])
        self.assertTrue(self.fallback.waited)
        self.assertEqual(source.read(), b'\x66' * 3840)
        self.assertTrue(source.last_read_had_audio)
        self.assertIsNone(source.last_opus_packet)
        self.assertEqual(source.passthrough_reason, 'archive_read_fallback')
        self.assertTrue(source.audio_buffer_metrics()['archive_opus_fallback'])

    async def test_invalid_first_part_uses_startup_error_instead_of_late_recovery(self):
        self.parts[0] = b'not an Ogg file'
        source = self.make_source()
        with self.assertRaises(buffer.OpusPacketError):
            await source.wait_ready(timeout=1)
        self.create_fallback.assert_not_awaited()

    async def test_preparing_source_does_not_prefetch_before_playback_activation(self):
        source = self.make_source(preparing=True)
        await source.wait_ready(timeout=1)
        source.read()
        await asyncio.sleep(0)
        self.assertEqual(self.streams.prefetched, [])
        source.activate_playback()
        self.assertTrue(self.streams.active)

    async def test_cleanup_cancels_pending_fallback_without_restarting_track(self):
        self.parts[1] = b'invalid later part'
        entered, waiting = asyncio.Event(), asyncio.Event()
        async def delayed(offset):
            entered.set()
            await waiting.wait()
            return self.fallback
        self.create_fallback.side_effect = delayed
        source = self.make_source()
        await source.wait_ready(timeout=1, min_frames=75)
        for _ in range(5):
            source.read()
        source.read()
        await asyncio.wait_for(entered.wait(), timeout=1)
        source.cleanup()
        await asyncio.sleep(.01)
        self.assertTrue(source._fallback_task.cancelled())
        self.assertEqual(source.read(), b'')
        self.assertTrue(self.streams.closed)

    async def test_cleanup_publishes_closed_before_a_racing_fallback_can_attach(self):
        source = self.make_source()
        await source.wait_ready(timeout=1, min_frames=75)
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        finished = threading.Event()
        original = self.streams.cleanup
        candidate = ReadyFallback()
        def paused_cleanup():
            entered.set()
            release.wait(timeout=1)
            original()
        self.streams.cleanup = paused_cleanup
        cleanup_thread = threading.Thread(target=source.cleanup, daemon=True)
        cleanup_thread.start()
        while not entered.is_set():
            await asyncio.sleep(.001)
        def attach():
            attempted.set()
            with source._source_lock:
                # Same publication protocol used by _start_fallback after its
                # candidate finishes waiting for the first PCM frame.
                if source._closed:
                    candidate.cleanup()
                else:
                    source._fallback_source = candidate
            finished.set()
        attach_thread = threading.Thread(target=attach, daemon=True)
        attach_thread.start()
        while not attempted.is_set():
            await asyncio.sleep(.001)
        await asyncio.sleep(.01)
        release.set()
        deadline = time.monotonic() + 1
        while cleanup_thread.is_alive() or attach_thread.is_alive():
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(.001)
        self.assertTrue(finished.is_set())
        self.assertTrue(candidate.closed)
        self.assertIsNone(source._fallback_source)


class FakeContent:
    def __init__(self, body):
        self.body = body
        self.pos = 0
        self.reads = 0

    async def read(self, size):
        self.reads += 1
        chunk = self.body[self.pos:self.pos + size]
        self.pos += len(chunk)
        await asyncio.sleep(0)
        return chunk


class FakeResponse:
    def __init__(self, body=b'', status=200):
        self.content = FakeContent(body)
        self.status = status
        self.headers = {}
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class FakeSession:
    def __init__(self, owner, **kwargs):
        self.owner = owner
        self.closed = False
        self.owner.session_options.append(kwargs)
        self.owner.sessions.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.owner.requests.append((url, kwargs))
        return self.owner.responses.pop(0)


class ArchiveHTTPBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.body = b'a' * (160 * 1024)
        self.requests, self.sessions, self.session_options = [], [], []
        self.response = FakeResponse(self.body)
        self.responses = [self.response]
        self.segment = {'url': 'https://cdn.discordapp.com/attachments/1/2/audio.ogg',
                        'size_bytes': len(self.body), 'sha256': hashlib.sha256(self.body).hexdigest()}
        self.resolve = mock.AsyncMock(side_effect=lambda index, **kwargs: dict(self.segment))
        import aiohttp
        self.patch = mock.patch.object(aiohttp, 'ClientSession', side_effect=lambda **kwargs: FakeSession(self, **kwargs))
        self.patch.start()
        self.url_patch = mock.patch.dict('sys.modules', {
            'cogs.musica.runtime_telefone.agente.validade_stream':
                SimpleNamespace(valid_cdn_url=lambda url: url)})
        self.url_patch.start()
        self.streams = []

    async def asyncTearDown(self):
        for stream in self.streams:
            stream.close()
        await asyncio.sleep(.02)
        self.url_patch.stop()
        self.patch.stop()

    def stream(self):
        stream = routes._ArchiveOpusHTTPStream(loop=asyncio.get_running_loop(), index=0,
                                               resolve_segment=self.resolve, stall_seconds=.2)
        self.streams.append(stream)
        return stream

    async def test_http_backpressure_caps_memory_and_validates_integrity_at_eof(self):
        stream = self.stream()
        await asyncio.sleep(.02)
        self.assertEqual(stream._buffered, 64 * 1024)
        self.assertFalse(stream._task.done())
        self.assertLessEqual(self.response.content.pos, 64 * 1024)
        def consume():
            chunks = []
            while chunk := stream.read(8192):
                chunks.append(chunk)
            return b''.join(chunks)
        self.assertEqual(await worker(consume), self.body)
        await stream._task
        self.assertTrue(self.response.closed)
        self.assertTrue(self.sessions[0].closed)
        self.assertFalse(self.requests[0][1]['allow_redirects'])
        self.assertEqual(self.requests[0][1]['headers'], {'Accept-Encoding': 'identity'})
        self.assertFalse(self.session_options[0]['trust_env'])
        self.assertEqual(self.session_options[0]['read_bufsize'], 16384)

    async def test_invalid_hash_is_reported_after_received_bytes_without_unbounded_retry(self):
        self.segment['sha256'] = '0' * 64
        stream = self.stream()
        def consume():
            while stream.read(16384):
                pass
        with self.assertRaisesRegex(ValueError, 'integridade'):
            await worker(consume)
        self.resolve.assert_awaited_once_with(0, force_refresh=False)

    async def test_expired_url_is_renewed_once_before_reading_any_audio(self):
        expired = FakeResponse(status=403)
        self.responses = [expired, self.response]
        stream = self.stream()
        def consume():
            while stream.read(16384):
                pass
        await worker(consume)
        self.assertEqual(self.resolve.await_args_list,
                         [mock.call(0, force_refresh=False), mock.call(0, force_refresh=True)])
        self.assertTrue(expired.closed)
        self.assertEqual(expired.content.reads, 0)

    async def test_redirect_is_rejected_without_fetching_another_location(self):
        self.responses = [FakeResponse(status=302)]
        stream = self.stream()
        with self.assertRaisesRegex(RuntimeError, 'abrir'):
            await worker(stream.read, 27)
        self.assertEqual(len(self.requests), 1)

    async def test_close_wakes_blocked_read_and_cancels_async_http(self):
        entered, waiting = asyncio.Event(), asyncio.Event()
        async def delayed_resolver(index, **kwargs):
            entered.set()
            await waiting.wait()
            return self.segment
        self.resolve.side_effect = delayed_resolver
        stream = self.stream()
        read = asyncio.create_task(worker(stream.read, 27))
        await entered.wait()
        stream.close()
        self.assertEqual(await asyncio.wait_for(read, timeout=.2), b'')
        await asyncio.sleep(.01)
        self.assertTrue(stream._task.cancelled())


class ArchiveStreamQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.created = []
        owner = self
        class HTTP:
            def __init__(self, *, index, **kwargs):
                self.index = index
                self.closed = False
                owner.created.append(self)
                owner.max_live = max(owner.max_live, sum(not item.closed for item in owner.created))
            def close(self):
                self.closed = True
        self.max_live = 0
        self.patch = mock.patch.object(routes, '_ArchiveOpusHTTPStream', HTTP)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.claim = mock.Mock(return_value=True)
        self.release = mock.Mock()
        self.factory = routes._ArchiveOpusStreams(
            loop=asyncio.get_running_loop(), resolve_segment=mock.AsyncMock(), count=4,
            stall_seconds=.2, claim_prepare=self.claim, release_prepare=self.release)
        self.addCleanup(self.factory.cleanup)

    async def test_only_current_and_one_next_are_open_and_each_quota_is_released(self):
        first = await worker(self.factory, 0)
        self.factory.prefetch(1)
        self.factory.prefetch(1)
        self.factory.prefetch(2)
        self.assertEqual([item.index for item in self.created], [0, 1])
        self.claim.assert_called_once()
        second = await worker(self.factory, 1)
        self.assertTrue(first.closed)
        self.assertIs(second, self.created[1])
        self.release.assert_called_once_with(self.claim.call_args.args[0])
        self.factory.prefetch(2)
        self.factory.cleanup()
        self.assertEqual(self.max_live, 2)
        self.assertTrue(all(item.closed for item in self.created))
        self.assertEqual(self.release.call_count, 2)
        self.factory.cleanup()
        self.assertEqual(self.release.call_count, 2)

    async def test_denied_extra_slot_does_not_block_mandatory_current_part(self):
        await worker(self.factory, 0)
        self.claim.return_value = False
        self.factory.prefetch(1)
        self.assertEqual(len(self.created), 1)
        next_part = await worker(self.factory, 1)
        self.assertEqual(next_part.index, 1)
        self.assertEqual(len(self.created), 2)
        self.release.assert_not_called()

    async def test_initial_preparation_waits_for_playback_before_prefetch(self):
        self.factory._playback_active = False
        await worker(self.factory, 0)
        self.factory.prefetch(1)
        self.claim.assert_not_called()
        self.factory.activate_playback()
        self.factory.prefetch(1)
        self.claim.assert_called_once()

    async def test_closed_factory_rejects_pending_or_new_selection(self):
        self.factory.cleanup()
        with self.assertRaisesRegex(RuntimeError, 'encerrada'):
            await worker(self.factory, 0)
        self.assertEqual(self.created, [])


if __name__ == '__main__':
    unittest.main()
