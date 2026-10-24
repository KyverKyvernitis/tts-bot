"""Native FFmpeg→bounded packets→mixer→Opus checks, without Discord/network.

The actual production buffer and hybrid mixer are executed. Only Discord's
AudioSource and codec Python bindings are adapted to the installed libopus.
These checks prove packet/PCM compatibility, not perceptual transparency.
"""
from __future__ import annotations

from array import array
import asyncio
import ctypes
import ctypes.util
import importlib.util
import io
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_music_opus_packets import NativeDecoder, buffer
from tests.test_music_output_native_opus import NativeOpus


ROOT = Path(__file__).resolve().parents[1]


def load_mixer():
    path = ROOT / "cogs/musica/runtime_telefone/agente/mixer_pcm.py"
    spec = importlib.util.spec_from_file_location("native_direct_real_mixer", path)
    module = importlib.util.module_from_spec(spec)
    class AudioSource:
        pass
    with mock.patch.dict("sys.modules", {"discord": SimpleNamespace(AudioSource=AudioSource)}):
        spec.loader.exec_module(module)
    return module


class NativeOutputEncoder(NativeOpus):
    def encode(self, frame, frame_size):
        if frame_size != 960 or len(frame) != 3840:
            raise ValueError("expected 20ms stereo PCM")
        samples = (ctypes.c_int16 * 1920).from_buffer_copy(frame)
        raw = (ctypes.c_ubyte * 4000)()
        size = self._check(self.lib.opus_encode(self.encoder, samples, frame_size, raw, len(raw)))
        self.calls.append(("encode", frame_size))
        return bytes(raw[:size])


class TTSTone:
    def __init__(self, frames=3):
        self.left = frames
        self.closed = False
        samples = array("h")
        for i in range(960):
            samples.extend([round(5000 * math.sin(2 * math.pi * 700 * i / 48000))] * 2)
        self.frame = samples.tobytes()

    def read(self):
        if self.left <= 0 or self.closed:
            return b""
        self.left -= 1
        return self.frame

    def cleanup(self):
        self.closed = True


@unittest.skipUnless(shutil.which("ffmpeg") and ctypes.util.find_library("opus"), "FFmpeg and native libopus required")
class NativeDirectPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mixer = load_mixer()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.directory = tempfile.TemporaryDirectory(prefix="native-opus-pipeline-")
        self.addCleanup(self.directory.cleanup)
        self.encoder = NativeOutputEncoder(ctypes.util.find_library("opus"))
        self.addCleanup(self.encoder.close)
        self.receiver = NativeDecoder()

    def pipeline(self, frame_ms=20):
        target = Path(self.directory.name) / f"music-{frame_ms}.ogg"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=0.6", "-ac", "2",
                        "-c:a", "libopus", "-b:a", "64k", "-frame_duration", str(frame_ms), str(target)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5)
        reader = buffer.OggOpusPacketReader(io.BytesIO(target.read_bytes()))
        expected = []
        while packet := reader.read_packet():
            expected.append(packet)
        raw = buffer.OpusPCMDecoderSource.from_ffmpeg(str(target), decoder_factory=NativeDecoder)
        self.upstream = buffer.BufferedOpusPCMSource(raw, max_frames=75)
        self.addCleanup(self.upstream.cleanup)
        self.loop.run_until_complete(self.upstream.wait_ready(timeout=3, min_frames=75))
        self.hybrid = self.mixer.AgentHybridOpusAudioSource(
            loop=self.loop, music_source=self.upstream, music_volume=1, persistent=False,
            telemetry_enabled=False, output_encoder=self.encoder,
            output_policy={"bitrate_kbps": 256, "fec": True, "expected_packet_loss": 0.15,
                           "opus_direct": "auto"})
        self.addCleanup(self.hybrid.cleanup)
        return expected

    def assert_packet_decodes(self, packet):
        self.assertTrue(packet)
        self.assertTrue(self.hybrid._opus_packet_is_20ms(packet))
        pcm = self.receiver.decode(packet, fec=False)
        self.assertEqual(len(pcm), 3840)
        self.assertTrue(any(pcm), "real music/overlay must not silently disappear in fallback")

    def test_native_copy_keeps_every_source_packet_bit_identical(self):
        expected = self.pipeline()
        observed = []
        decoder_identity = self.upstream.source.decoder.state
        encoder_identity = self.encoder.encoder
        while packet := self.hybrid.read():
            observed.append(packet)
            self.assert_packet_decodes(packet)
        self.assertEqual(observed, expected)
        self.assertEqual(len(observed), self.hybrid.opus_direct_status()["copied_packets"])
        self.assertEqual(self.hybrid.opus_direct_status()["encoded_packets"], 0)
        self.assertFalse(any(name == "encode" for name, _ in self.encoder.calls))
        self.assertEqual(self.upstream.source.decoder.state, decoder_identity)
        self.assertEqual(self.encoder.encoder, encoder_identity)

    def test_volume_fallback_keeps_clock_encoder_and_nonzero_audio(self):
        expected = self.pipeline()
        for index in range(3):
            packet = self.hybrid.read()
            self.assertEqual(packet, expected[index])
            self.assert_packet_decodes(packet)
        identity = self.encoder.encoder
        self.hybrid.set_music_volume(0.5)
        for _ in range(4):
            self.assert_packet_decodes(self.hybrid.read())
        self.hybrid.set_music_volume(1)
        for _ in range(4):
            self.assert_packet_decodes(self.hybrid.read())
        status = self.hybrid.opus_direct_status()
        self.assertEqual(status["state"], "pcm_fallback")
        self.assertEqual(status["reason"], "volume_requires_pcm")
        self.assertEqual(status["copied_packets"], 3)
        self.assertEqual(status["encoded_packets"], 8)
        self.assertEqual(self.encoder.encoder, identity)

    def test_tts_fallback_keeps_decode_clock_and_does_not_resume_copy(self):
        expected = self.pipeline()
        for index in range(3):
            packet = self.hybrid.read()
            self.assertEqual(packet, expected[index])
            self.assert_packet_decodes(packet)
        identity = self.encoder.encoder
        overlay = TTSTone(frames=3)
        future = self.hybrid.add_tts(overlay, volume=1)
        for _ in range(7):
            self.assert_packet_decodes(self.hybrid.read())
        self.loop.run_until_complete(asyncio.sleep(0))
        self.assertTrue(future.done())
        self.assertIsNone(future.result())
        self.assertTrue(overlay.closed)
        self.assertFalse(self.hybrid.has_tts())
        status = self.hybrid.opus_direct_status()
        self.assertEqual(status["copied_packets"], 3)
        self.assertEqual(status["encoded_packets"], 7)
        self.assertEqual(status["reason"], "tts_overlay")
        self.assertTrue(status["fallback_latched"])
        self.assertEqual(self.encoder.encoder, identity)

    def test_native_40ms_input_falls_back_to_valid_20ms_output(self):
        self.pipeline(frame_ms=40)
        count = 0
        while packet := self.hybrid.read():
            self.assert_packet_decodes(packet)
            count += 1
        self.assertGreater(count, 10)
        status = self.hybrid.opus_direct_status()
        self.assertEqual(status["copied_packets"], 0)
        self.assertEqual(status["encoded_packets"], count)
        self.assertEqual(status["state"], "pcm_fallback")


if __name__ == "__main__":
    unittest.main()
