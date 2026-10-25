"""Real multipart archive packets through the production buffer and hybrid mixer.

Uses local FFmpeg/libopus with the existing Discord codec boundary adapters.
Checks original packet identity, continuous decoder output and conservative
fallback. It does not represent a live Discord, HTTP or perceptual test.
"""
from __future__ import annotations

import asyncio
import ctypes.util
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs.musica.runtime_telefone.agente.archive_ogg import add_opus_preroll, _packets
from tests.test_music_opus_native_pipeline import NativeOutputEncoder, load_mixer
from tests.test_music_opus_packets import NativeDecoder, buffer, page
from tests import test_music_opus_segmented_routing as routing_checks


@unittest.skipUnless(shutil.which("ffmpeg") and ctypes.util.find_library("opus"),
                     "FFmpeg and native libopus required")
class NativeSegmentedPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mixer = load_mixer()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="opus-segmented-pipeline-")
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def archive_fixture(self, frame_ms):
        original = self.folder / "original.ogg"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=450:sample_rate=48000:duration=2.13", "-ac", "2",
            "-c:a", "libopus", "-b:a", "64k", "-frame_duration", str(frame_ms), str(original),
        ], check=True, capture_output=True, timeout=10)
        subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(original), "-map", "0:a:0", "-c:a", "copy",
            "-f", "segment", "-segment_format", "ogg", "-segment_time", "0.7",
            "-reset_timestamps", "1", str(self.folder / "part-%03d.ogg"),
        ], check=True, capture_output=True, timeout=10)
        paths = sorted(self.folder.glob("part-*.ogg"))
        self.assertGreaterEqual(len(paths), 3)
        durations = add_opus_preroll(original, paths)
        segments, offset = [], 0.0
        for order, duration in enumerate(durations):
            segments.append({"order": order, "offset_seconds": offset, "duration": duration,
                             "audio_codec": "opus", "audio_sample_rate": 48000, "audio_channels": 2})
            offset += duration
        return original, paths, segments

    def decode_all(self, reader):
        source = buffer.OpusPCMDecoderSource(reader, decoder_factory=NativeDecoder)
        try:
            frames = []
            while frame := source.read():
                frames.append(frame)
            return b"".join(frames)
        finally:
            source.cleanup()

    def check_native_pipeline(self, frame_ms):
        original, paths, segments = self.archive_fixture(frame_ms)
        expected_packets = list(_packets(original))[2:]
        original_pcm = self.decode_all(buffer.OggOpusPacketReader(io.BytesIO(original.read_bytes())))
        logical = buffer.ArchiveOpusPacketReader(segments, lambda index: paths[index].open("rb"))
        self.assertEqual(self.decode_all(logical), original_pcm)
        self.assertGreater(logical.metadata["archive_preroll_packets_skipped"], 0)

        loop = asyncio.new_event_loop()
        encoder = NativeOutputEncoder(ctypes.util.find_library("opus"))
        receiver = NativeDecoder()
        upstream = buffer.BufferedOpusPCMSource(buffer.OpusPCMDecoderSource(
            buffer.ArchiveOpusPacketReader(segments, lambda index: paths[index].open("rb")),
            decoder_factory=NativeDecoder), max_frames=150)
        hybrid = None
        try:
            # This fixture is shorter than the bounded buffer. Warm it fully
            # so a synthetic fast consumer does not manufacture a network stall.
            loop.run_until_complete(upstream.wait_ready(timeout=5, min_frames=150))
            hybrid = self.mixer.AgentHybridOpusAudioSource(
                loop=loop, music_source=upstream, music_volume=1, persistent=False,
                telemetry_enabled=False, output_encoder=encoder,
                output_policy={"bitrate_kbps": 96, "copy_bitrate_kbps": 256,
                               "channel_bitrate_kbps": 256, "fec": True,
                               "expected_packet_loss": 0.15, "opus_direct": "auto"})
            observed = []
            while packet := hybrid.read():
                observed.append(packet)
                pcm = receiver.decode(packet, fec=False)
                self.assertEqual(len(pcm), 3840)
                self.assertTrue(any(pcm), "music must not disappear into silence on fallback")
            status = hybrid.opus_direct_status()
            if frame_ms == 20:
                self.assertEqual(observed, expected_packets)
                self.assertEqual(status["copied_packets"], len(expected_packets))
                self.assertEqual(status["encoded_packets"], 0)
                self.assertFalse(any(name == "encode" for name, _ in encoder.calls))
            else:
                self.assertEqual(status["copied_packets"], 0)
                self.assertEqual(status["encoded_packets"], len(observed))
                self.assertEqual(status["state"], "pcm_fallback")
                self.assertEqual(len(observed) * 3840, len(original_pcm))
        finally:
            if hybrid is not None:
                hybrid.cleanup()
            else:
                upstream.cleanup()
            encoder.close()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    def test_native_20ms_archive_copies_every_original_packet_and_keeps_continuous_pcm(self):
        self.check_native_pipeline(20)

    def test_native_40ms_archive_keeps_continuous_pcm_and_encodes_valid_20ms_output(self):
        self.check_native_pipeline(40)

    def test_native_packets_cross_actual_async_http_source_and_hybrid_without_reencoding(self):
        original, paths, segments = self.archive_fixture(20)
        expected = list(_packets(original))[2:]
        bodies = [path.read_bytes() for path in paths]
        responses = [routing_checks.FakeResponse(body) for body in bodies]
        created_responses = tuple(responses)
        http = SimpleNamespace(requests=[], sessions=[], session_options=[], responses=responses)
        calls = []
        async def resolve(index, *, force_refresh=False):
            calls.append((index, force_refresh))
            return {**segments[index], "size_bytes": len(bodies[index]),
                    "sha256": hashlib.sha256(bodies[index]).hexdigest(),
                    "url": f"https://cdn.discordapp.com/attachments/1/{index + 10}/part.ogg"}

        async def scenario():
            encoder = NativeOutputEncoder(ctypes.util.find_library("opus"))
            fallback = mock.AsyncMock()
            source = routing_checks.routes.ArchiveOpusPCMSource(
                segments=segments, resolve_segment=resolve, create_fallback=fallback,
                loop=asyncio.get_running_loop(), decoder_factory=NativeDecoder,
                max_frames=150, stall_seconds=2, preparing=True)
            hybrid = None
            try:
                await source.wait_ready(timeout=5, min_frames=150)
                hybrid = self.mixer.AgentHybridOpusAudioSource(
                    loop=asyncio.get_running_loop(), music_source=source, music_volume=1,
                    persistent=False, telemetry_enabled=False, output_encoder=encoder,
                    output_policy={"bitrate_kbps": 96, "copy_bitrate_kbps": 256,
                                   "channel_bitrate_kbps": 256, "fec": True,
                                   "expected_packet_loss": 0.15, "opus_direct": "auto"})
                observed = []
                while packet := hybrid.read():
                    observed.append(packet)
                self.assertEqual(observed, expected)
                self.assertEqual(hybrid.opus_direct_status()["encoded_packets"], 0)
                fallback.assert_not_awaited()
            finally:
                if hybrid is not None:
                    hybrid.cleanup()
                else:
                    source.cleanup()
                encoder.close()
                await asyncio.sleep(0)

        import aiohttp
        # The resolver/session are external boundaries. The actual HTTP bridge,
        # producer thread, continuous reader and hybrid mixer execute together.
        with mock.patch.object(aiohttp, "ClientSession", side_effect=lambda **kwargs:
                               routing_checks.FakeSession(http, **kwargs)), \
             mock.patch.dict("sys.modules", {
                 "cogs.musica.runtime_telefone.agente.validade_stream":
                     SimpleNamespace(valid_cdn_url=lambda url: url)}):
            asyncio.run(scenario())
        self.assertEqual(calls, [(index, False) for index in range(len(paths))])
        self.assertEqual(len(http.requests), len(paths))
        self.assertTrue(all(session.closed for session in http.sessions))
        self.assertTrue(all(response.closed for response in created_responses))
        self.assertTrue(all(not options["allow_redirects"] for _, options in http.requests))

    def test_valid_crc_with_wrong_preroll_fails_before_exposing_any_second_part_packet(self):
        _original, paths, segments = self.archive_fixture(20)
        payloads = [path.read_bytes() for path in paths]
        packets = list(_packets(paths[1]))
        altered = bytearray(packets[2])
        altered[-1] ^= 1
        packets[2] = bytes(altered)
        samples = sum(buffer.opus_packet_samples(packet) for packet in packets[2:])
        payloads[1] = (page([packets[0]], flags=2) + page([packets[1]], sequence=1)
                       + page(packets[2:], sequence=2, flags=4, granule=samples))
        logical = buffer.ArchiveOpusPacketReader(segments, lambda index: io.BytesIO(payloads[index]))
        observed = []
        try:
            with self.assertRaisesRegex(buffer.OpusPacketError, "preroll"):
                while packet := logical.read_packet():
                    observed.append(packet)
            self.assertEqual(observed, list(_packets(paths[0]))[2:])
        finally:
            logical.cleanup()


if __name__ == "__main__":
    unittest.main()
