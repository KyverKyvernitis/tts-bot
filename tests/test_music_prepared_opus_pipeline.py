"""Native prepared-source → cache → buffer → hybrid output integration.

FFmpeg and libopus are real. Only Discord's AudioSource/codec Python bindings
are adapted, as in the existing native suites. This checks packet provenance,
bounded bitrate and cache reuse; it is not a perceptual or live Discord test.
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
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.runtime_telefone.agente import configuracao as config
from cogs.musica.runtime_telefone.agente.estado import AgentTrack
from tests.test_music_opus_native_pipeline import NativeOutputEncoder, load_mixer
from tests.test_music_opus_packets import NativeDecoder, buffer


@unittest.skipUnless(shutil.which("ffmpeg") and ctypes.util.find_library("opus"),
                     "FFmpeg and native libopus required")
class NativePreparedOpusPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mixer = load_mixer()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="prepared-opus-pipeline-")
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def fixture(self, codec):
        path = self.folder / ("original.m4a" if codec == "aac" else "original.mp3")
        encoder = "aac" if codec == "aac" else "libmp3lame"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=543:sample_rate=44100:duration=0.57", "-ac", "1",
            "-c:a", encoder, "-b:a", "128k", str(path),
        ], check=True, capture_output=True, timeout=10)
        return path

    def cache(self):
        return config.PreparedOpusCache(self.folder / "cache", max_bytes=1024 * 1024,
                                        max_item_bytes=512 * 1024, min_free_bytes=0)

    def wrap(self, raw):
        source = buffer.BufferedOpusPCMSource(raw, max_frames=150)
        self.loop.run_until_complete(source.wait_ready(timeout=5, min_frames=150))
        encoder = NativeOutputEncoder(ctypes.util.find_library("opus"))
        hybrid = self.mixer.AgentHybridOpusAudioSource(
            loop=self.loop, music_source=source, music_volume=1, persistent=False,
            telemetry_enabled=False, output_encoder=encoder,
            output_policy={"bitrate_kbps": 96, "copy_bitrate_kbps": 256,
                           "channel_bitrate_kbps": 256, "fec": True,
                           "expected_packet_loss": .15, "opus_direct": "auto"})
        self.addCleanup(encoder.close)
        self.addCleanup(hybrid.cleanup)
        return source, hybrid, encoder

    def collect(self, hybrid):
        receiver = NativeDecoder()
        observed, pcm = [], bytearray()
        while packet := hybrid.read():
            self.assertEqual(buffer.opus_packet_samples(packet), 960)
            frame = receiver.decode(packet, fec=False)
            self.assertEqual(len(frame), 3840)
            observed.append(packet)
            pcm.extend(frame)
        self.assertTrue(any(pcm), "prepared music must remain audible")
        return observed

    def test_aac_mono_44100_is_prepared_once_and_warm_cache_copies_same_packets(self):
        path = self.fixture("aac")
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        cache = self.cache()
        track = AgentTrack(webpage_url="https://www.youtube.com/watch?v=abcdefghijk",
                           audio_format_id="140", audio_codec="aac", audio_sample_rate=44100,
                           audio_channels=1, duration=.57)
        key = config.prepared_opus_cache_key(track, 256)
        self.assertIsNotNone(key)
        raw = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
            str(path), bitrate_kbps=256, source_codec="aac", decoder_factory=NativeDecoder,
            cache_writer=cache.writer(key))
        source, hybrid, encoder = self.wrap(raw)
        observed = self.collect(hybrid)
        self.assertTrue(observed)
        self.assertTrue(all(len(packet) <= 640 for packet in observed))
        self.assertTrue(source.opus_metadata["prepared_opus"])
        self.assertFalse(source.opus_metadata["prepared_cache_hit"])
        self.assertEqual(source.opus_metadata["prepared_source_codec"], "aac")
        self.assertEqual(hybrid.opus_direct_status()["copied_packets"], len(observed))
        self.assertEqual(hybrid.opus_direct_status()["encoded_packets"], 0)
        self.assertFalse(any(name == "encode" for name, _ in encoder.calls))
        committed = cache.get(key)
        self.assertIsNotNone(committed)
        reader = buffer.OggOpusPacketReader(io.BytesIO(committed.read_bytes()))
        stored_packets = []
        while packet := reader.read_packet():
            stored_packets.append(packet)
        self.assertEqual(stored_packets, observed)

        # Reuse after a process-like cache reconstruction. No input resolution
        # or FFmpeg subprocess is needed for the already prepared bytes.
        reloaded_cache = self.cache()
        no_spawn = SimpleNamespace(Popen=mock.Mock(side_effect=AssertionError("warm cache must not encode")),
                                   PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL)
        with mock.patch.object(buffer, "subprocess", no_spawn):
            hit = buffer.OpusPCMDecoderSource.from_prepared_file(
                reloaded_cache.get(key), bitrate_kbps=256, source_codec="aac", decoder_factory=NativeDecoder)
            warm_source, warm_hybrid, warm_encoder = self.wrap(hit)
            self.assertEqual(self.collect(warm_hybrid), observed)
            self.assertTrue(warm_source.opus_metadata["prepared_cache_hit"])
            self.assertEqual(warm_hybrid.opus_direct_status()["encoded_packets"], 0)
            self.assertFalse(any(name == "encode" for name, _ in warm_encoder.calls))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(track.audio_codec, "aac")
        self.assertEqual(track.audio_sample_rate, 44100)

    def test_prepared_mp3_channel_shrink_uses_valid_pcm_fallback_without_reopening_input(self):
        path = self.fixture("mp3")
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        raw = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
            str(path), bitrate_kbps=256, source_codec="mp3", decoder_factory=NativeDecoder)
        source, hybrid, encoder = self.wrap(raw)
        for _ in range(3):
            packet = hybrid.read()
            self.assertEqual(len(packet), 640)
        self.assertEqual(hybrid.opus_direct_status()["copied_packets"], 3)
        no_spawn = SimpleNamespace(Popen=mock.Mock(side_effect=AssertionError("policy change must not restart input")),
                                   PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL)
        with mock.patch.object(buffer, "subprocess", no_spawn):
            hybrid.set_output_policy({"bitrate_kbps": 96, "copy_bitrate_kbps": 96,
                                      "channel_bitrate_kbps": 96, "opus_direct": "auto",
                                      "fec": True, "expected_packet_loss": .15})
            self.collect(hybrid)
        status = hybrid.opus_direct_status()
        self.assertEqual(status["copied_packets"], 3)
        self.assertGreater(status["encoded_packets"], 0)
        self.assertEqual(status["reason"], "packet_bitrate_limit")
        self.assertTrue(status["fallback_latched"])
        self.assertTrue(any(name == "encode" for name, _ in encoder.calls))
        self.assertTrue(source.opus_metadata["prepared_opus"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
