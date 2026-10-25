"""Separate forwarding/encoder capacities with real libopus packets and PCM.

The packet is padded through libopus to a valid size above the balanced
encoder target. This proves bounded byte preservation and valid fallback,
not network tolerance or perceptual quality.
"""
from __future__ import annotations

from array import array
import asyncio
import ctypes
import ctypes.util
import io
import math
from types import SimpleNamespace
import unittest

from cogs.musica.runtime_telefone.agente.configuracao import audio_output_policy
from tests.test_music_opus_native_pipeline import NativeOutputEncoder, load_mixer
from tests.test_music_opus_packets import NativeDecoder, buffer, fixture


@unittest.skipUnless(ctypes.util.find_library("opus"), "native libopus required")
class NativeCopyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.origin = NativeOutputEncoder(ctypes.util.find_library("opus"))
        self.addCleanup(self.origin.close)
        self.encoder = NativeOutputEncoder(ctypes.util.find_library("opus"))
        self.addCleanup(self.encoder.close)
        self.origin.set_bitrate(96)
        pcm = array("h")
        for i in range(960):
            pcm.extend([round(6000 * math.sin(2 * math.pi * 440 * i / 48000))] * 2)
        packet = self.origin.encode(pcm.tobytes(), 960)
        self.assertLess(len(packet), 500)
        padded = (ctypes.c_ubyte * 500)()
        padded[:len(packet)] = packet
        self.origin.lib.opus_packet_pad.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32, ctypes.c_int32]
        self.origin.lib.opus_packet_pad.restype = ctypes.c_int
        self.assertEqual(self.origin.lib.opus_packet_pad(padded, len(packet), 500), 0)
        self.packet = bytes(padded)
        self.assertEqual(len(NativeDecoder().decode(self.packet)), 3840)

        raw = buffer.OpusPCMDecoderSource(
            buffer.OggOpusPacketReader(io.BytesIO(fixture([self.packet, self.packet]))),
            decoder_factory=NativeDecoder)
        self.upstream = buffer.BufferedOpusPCMSource(raw, max_frames=4)
        self.addCleanup(self.upstream.cleanup)
        self.loop.run_until_complete(self.upstream.wait_ready(timeout=2, min_frames=4))
        self.agent = SimpleNamespace(audio_output_default_opus_direct="auto")
        self.channel = SimpleNamespace(bitrate=256000)
        self.track = SimpleNamespace(audio_abr=131)
        self.policy = audio_output_policy(self.agent, SimpleNamespace(channel=self.channel), self.track)
        self.source = load_mixer().AgentHybridOpusAudioSource(
            loop=self.loop, music_source=self.upstream, music_volume=1, persistent=False,
            telemetry_enabled=False, output_encoder=self.encoder, output_policy=self.policy)
        self.addCleanup(self.source.cleanup)

    def test_native_packet_above_encoder_target_is_copied_bit_identically(self):
        self.assertEqual(self.policy["bitrate_kbps"], 163)
        self.assertEqual(self.policy["copy_bitrate_kbps"], 256)
        self.assertGreater(len(self.packet), self.policy["bitrate_kbps"] * 2.5)
        self.assertEqual(self.source.read(), self.packet)
        self.assertEqual(self.source.read(), self.packet)
        self.assertFalse(any(name == "encode" for name, _ in self.encoder.calls))
        self.assertEqual(self.source.opus_direct_status()["copied_packets"], 2)

    def test_native_channel_shrink_falls_back_to_valid_opus_without_recopying(self):
        self.assertEqual(self.source.read(), self.packet)
        self.channel.bitrate = 128000
        self.source.set_output_policy(audio_output_policy(
            self.agent, SimpleNamespace(channel=self.channel), self.track))
        encoded = self.source.read()
        self.assertNotEqual(encoded, self.packet)
        self.assertEqual(len(NativeDecoder().decode(encoded)), 3840)
        status = self.source.opus_direct_status()
        self.assertEqual(status["reason"], "packet_bitrate_limit")
        self.assertEqual(status["copy_bitrate_kbps"], 128)
        self.assertEqual(status["copied_packets"], 1)
        self.assertEqual(status["encoded_packets"], 1)


if __name__ == "__main__":
    unittest.main()
