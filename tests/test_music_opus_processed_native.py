"""Processed music uses one native Opus encode after the existing PCM mixer.

libopus performs encoding and decoding. The reference is the unchanged PCM
mixer, so these checks cover transport integration and control preservation,
not perceptual transparency, Discord timing, or phone CPU performance.
"""
from __future__ import annotations

from array import array
import asyncio
import ctypes.util
import io
import math
import unittest

from tests.test_music_opus_native_pipeline import NativeOutputEncoder, load_mixer
from tests.test_music_opus_packets import NativeDecoder, buffer, fixture


class CaptureNativeEncoder(NativeOutputEncoder):
    def __init__(self):
        super().__init__(ctypes.util.find_library("opus"))
        self.frames = []

    def encode(self, frame, samples):
        self.frames.append(bytes(frame))
        return super().encode(frame, samples)


class TonePCM:
    def __init__(self, *, frames=100, frequency=80, amplitude=19000, underrun=False):
        self.remaining = frames
        self.frequency, self.amplitude = frequency, amplitude
        self.position = 0
        self.closed = False
        self.last_read_had_audio = False
        self.underrun = underrun
        self.on_read = None

    def read(self):
        self.last_read_had_audio = False
        if self.closed or not self.remaining:
            return b""
        if self.underrun:
            self.underrun = False
            return bytes(3840)
        if self.on_read is not None:
            callback, self.on_read = self.on_read, None
            callback()
        output = array("h")
        for index in range(960):
            sample = round(self.amplitude * math.sin(
                2 * math.pi * self.frequency * (self.position + index) / 48000))
            output.extend((sample, sample))
        self.position += 960
        self.remaining -= 1
        self.last_read_had_audio = True
        return output.tobytes()

    def cleanup(self):
        self.closed = True


@unittest.skipUnless(ctypes.util.find_library("opus"), "native libopus required")
class ProcessedOpusNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mixer = load_mixer()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def make(self, *, volume=.55, bass=0, source=None, persistent=True):
        encoder = CaptureNativeEncoder()
        self.addCleanup(encoder.close)
        source = source if source is not None else TonePCM()
        output = self.mixer.AgentHybridOpusAudioSource(
            loop=self.loop, music_source=source, music_volume=volume,
            persistent=persistent, bassboost_level=bass, duck_factor=.08,
            telemetry_enabled=False, output_encoder=encoder,
            output_policy={"bitrate_kbps": 96, "copy_bitrate_kbps": 256,
                           "channel_bitrate_kbps": 256, "opus_direct": "auto",
                           "fec": True, "expected_packet_loss": .15})
        output.quality_context = {}
        self.addCleanup(output.cleanup)
        return output, encoder

    def reference(self, *, volume=.55, bass=0, source=None):
        output = self.mixer.AgentMixedAudioSource(
            loop=self.loop, music_source=source if source is not None else TonePCM(),
            music_volume=volume, persistent=True, bassboost_level=bass,
            duck_factor=.08, telemetry_enabled=False)
        self.addCleanup(output.cleanup)
        return output

    def decode(self, packet, receiver):
        self.assertTrue(packet)
        self.assertEqual(buffer.opus_packet_samples(packet), 960)
        decoded = receiver.decode(packet, fec=False)
        self.assertEqual(len(decoded), 3840)
        return decoded

    def flush(self):
        self.loop.run_until_complete(asyncio.sleep(0))

    def test_volumes_zero_55_100_150_encode_existing_mixer_output_once_per_frame(self):
        for volume in (0, .55, 1, 1.5):
            with self.subTest(volume=volume):
                source, encoder = self.make(volume=volume)
                reference = self.reference(volume=volume)
                receiver = NativeDecoder()
                self.assertFalse(source.opus_direct_status()["processed_active"])
                for index in range(4):
                    packet = source.read()
                    self.decode(packet, receiver)
                    self.assertEqual(len(encoder.frames), index + 1)
                    self.assertEqual(encoder.frames[-1], reference.read())
                    status = source.opus_direct_status()
                    self.assertTrue(source.is_opus())
                    self.assertTrue(status["opus_transport"])
                    self.assertTrue(status["processed_active"])
                    self.assertFalse(status["copy_active"])
                    self.assertEqual(status["output_mode"], "opus_processed")
                    self.assertEqual(status["processed_bitrate_kbps"], 96)
                    self.assertEqual(source.quality_context["output_mode"], "opus_processed")
                if volume == 0:
                    self.assertEqual(encoder.frames[-1], bytes(3840))
                source.cleanup()
                reference.cleanup()

    def test_all_bassboost_levels_process_pcm_before_one_native_encode(self):
        for level in range(7):
            with self.subTest(level=level):
                source, encoder = self.make(bass=level)
                reference = self.reference(bass=level)
                receiver = NativeDecoder()
                for _ in range(3):
                    self.decode(source.read(), receiver)
                    self.assertEqual(encoder.frames[-1], reference.read())
                self.assertEqual(len(encoder.frames), 3)
                self.assertTrue(source.opus_direct_status()["processed_active"])
                self.assertFalse(source.opus_direct_status()["copy_active"])
                samples = array("h")
                samples.frombytes(encoder.frames[-1])
                self.assertLessEqual(max(abs(value) for value in samples), 32767)
                source.cleanup()
                reference.cleanup()

    def test_live_gain_and_bass_changes_preserve_encoder_and_original_pcm_mixing(self):
        source, encoder = self.make(volume=.55)
        reference = self.reference(volume=.55)
        receiver = NativeDecoder()
        encoder_identity = encoder.encoder
        for volume, bass in ((.55, 0), (.9, 2), (1.5, 6), (0, 0), (1, 0)):
            source.set_music_volume(volume)
            source.set_bassboost_level(bass)
            reference.set_music_volume(volume)
            reference.set_bassboost_level(bass)
            self.decode(source.read(), receiver)
            self.assertEqual(encoder.frames[-1], reference.read())
            self.assertEqual(encoder.encoder, encoder_identity)
            self.assertTrue(source.opus_direct_status()["processed_active"])
        self.assertEqual(len(encoder.frames), 5)

    def test_ducked_tts_uses_same_mixer_and_pause_carries_tts_without_claiming_music(self):
        music, reference_music = TonePCM(), TonePCM()
        source, encoder = self.make(source=music)
        reference = self.reference(source=reference_music)
        receiver = NativeDecoder()
        future = source.add_tts(TonePCM(frames=3, frequency=700, amplitude=6000))
        reference_future = reference.add_tts(TonePCM(frames=3, frequency=700, amplitude=6000))
        for _ in range(6):
            self.decode(source.read(), receiver)
            self.assertEqual(encoder.frames[-1], reference.read())
            self.assertTrue(source.opus_direct_status()["processed_active"])
        self.flush()
        self.assertIsNone(future.result())
        self.assertIsNone(reference_future.result())
        paused_position = music.position
        source.set_music_paused(True)
        reference.set_music_paused(True)
        self.assertFalse(source.opus_direct_status()["processed_active"])
        future = source.add_tts(TonePCM(frames=2, frequency=900, amplitude=5000))
        reference_future = reference.add_tts(TonePCM(frames=2, frequency=900, amplitude=5000))
        for _ in range(4):
            self.decode(source.read(), receiver)
            self.assertEqual(encoder.frames[-1], reference.read())
            status = source.opus_direct_status()
            self.assertFalse(status["processed_active"])
            self.assertEqual(status["output_mode"], "")
            self.assertEqual(music.position, paused_position)
        self.flush()
        self.assertIsNone(future.result())
        self.assertIsNone(reference_future.result())
        source.set_music_paused(False)
        reference.set_music_paused(False)
        self.assertFalse(source.opus_direct_status()["processed_active"])
        self.decode(source.read(), receiver)
        self.assertEqual(encoder.frames[-1], reference.read())
        self.assertTrue(source.opus_direct_status()["processed_active"])

    def test_replacement_generation_requires_a_new_encoded_music_read(self):
        source, encoder = self.make()
        receiver = NativeDecoder()
        self.decode(source.read(), receiver)
        self.assertTrue(source.opus_direct_status()["processed_active"])
        identity = encoder.encoder
        replacement = TonePCM(frequency=300)
        source.replace_music_source(replacement, volume=.75, on_music_end=None)
        self.assertFalse(source.opus_direct_status()["processed_active"])
        self.assertEqual(source.opus_direct_status()["encoded_packets"], 0)
        self.decode(source.read(), receiver)
        self.assertTrue(source.opus_direct_status()["processed_active"])
        self.assertEqual(source.opus_direct_status()["encoded_packets"], 1)
        self.assertEqual(encoder.encoder, identity)

    def test_source_replaced_inside_a_read_never_lends_processed_proof_to_new_source(self):
        old = TonePCM()
        source, encoder = self.make(source=old)
        replacement = TonePCM(frequency=300)
        old.on_read = lambda: source.replace_music_source(replacement, volume=.55, on_music_end=None)
        self.decode(source.read(), NativeDecoder())
        self.assertEqual(encoder.frames[-1], bytes(3840))
        self.assertEqual(replacement.position, 0)
        self.assertFalse(source.opus_direct_status()["processed_active"])
        self.decode(source.read(), NativeDecoder())
        self.assertTrue(source.opus_direct_status()["processed_active"])

    def test_underrun_eof_stop_and_cleanup_do_not_claim_processed_music(self):
        music = TonePCM(frames=1, underrun=True)
        source, encoder = self.make(source=music)
        receiver = NativeDecoder()
        self.decode(source.read(), receiver)
        self.assertFalse(source.opus_direct_status()["processed_active"])
        self.decode(source.read(), receiver)
        self.assertTrue(source.opus_direct_status()["processed_active"])
        self.decode(source.read(), receiver)
        self.assertFalse(source.opus_direct_status()["processed_active"])
        source.stop_music()
        self.assertFalse(source.opus_direct_status()["processed_active"])
        source.cleanup()
        self.assertEqual(source.read(), b"")
        self.assertFalse(source.opus_direct_status()["processed_active"])

    def test_unknown_encoder_controls_do_not_invent_a_processed_bitrate(self):
        source, encoder = self.make()
        policy = dict(source._opus_policy)
        source.set_output_policy(policy, apply_encoder=False, applied_encoder={
            "state": "partial", "bitrate_kbps": None})
        self.decode(source.read(), NativeDecoder())
        self.assertTrue(source.opus_direct_status()["processed_active"])
        self.assertIsNone(source.opus_direct_status()["processed_bitrate_kbps"])
        source.set_output_policy(policy, apply_encoder=False, applied_encoder={
            "state": "partial", "bitrate_kbps": 96})
        self.assertEqual(source.opus_direct_status()["processed_bitrate_kbps"], 96)
        source.set_output_policy(policy, apply_encoder=False, applied_encoder={
            "state": "unsupported", "bitrate_kbps": 96})
        self.assertIsNone(source.opus_direct_status()["processed_bitrate_kbps"])

    def test_original_packets_stay_identical_until_processing_is_required(self):
        origin = CaptureNativeEncoder()
        self.addCleanup(origin.close)
        original_pcm = TonePCM().read()
        packet = origin.encode(original_pcm, 960)
        raw = buffer.OpusPCMDecoderSource(
            buffer.OggOpusPacketReader(io.BytesIO(fixture([packet] * 5))), decoder_factory=NativeDecoder)
        upstream = buffer.BufferedOpusPCMSource(raw, max_frames=8)
        self.addCleanup(upstream.cleanup)
        self.loop.run_until_complete(upstream.wait_ready(timeout=2, min_frames=8))
        source, encoder = self.make(source=upstream, volume=1)
        self.assertEqual(source.read(), packet)
        self.assertTrue(source.opus_direct_status()["copy_active"])
        self.assertFalse(source.opus_direct_status()["processed_active"])
        self.assertEqual(encoder.frames, [])
        source.set_music_volume(.55)
        self.decode(source.read(), NativeDecoder())
        self.assertTrue(source.opus_direct_status()["processed_active"])
        self.assertFalse(source.opus_direct_status()["copy_active"])
        source.set_music_volume(1)
        self.decode(source.read(), NativeDecoder())
        self.assertTrue(source.opus_direct_status()["processed_active"])
        self.assertEqual(source.opus_direct_status()["copied_packets"], 1)
        self.assertEqual(len(encoder.frames), 2)


if __name__ == "__main__":
    unittest.main()
