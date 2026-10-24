"""Hybrid packet routing using the production mixer and simulated codec/source.

These stdlib checks exercise lifecycle, mixing and exact packet preservation.
Native codec/demux validation is covered separately, not by the fake packets
used here. They do not assert that switching independent encoders is inaudible.
"""
from __future__ import annotations

from array import array
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PCM = array("h", [1000, -1500] * 960).tobytes()
RAW = b"\xf8raw-packet"
ENCODED = b"\xf8encoded-packet"


def load_mixer():
    spec = importlib.util.spec_from_file_location(
        "hybrid_real_mixer", ROOT / "cogs/musica/runtime_telefone/agente/mixer_pcm.py")
    module = importlib.util.module_from_spec(spec)
    class AudioSource:
        pass
    with mock.patch.dict("sys.modules", {"discord": SimpleNamespace(AudioSource=AudioSource)}):
        spec.loader.exec_module(module)
    return module


class Encoder:
    def __init__(self):
        self.calls = []
        self.frames = []

    def set_bitrate(self, value):
        self.calls.append(("bitrate", value))
        return value

    def set_fec(self, value):
        self.calls.append(("fec", value))

    def set_expected_packet_loss_percent(self, value):
        self.calls.append(("loss", value))

    def set_signal_type(self, value):
        self.calls.append(("signal", value))

    def set_bandwidth(self, value):
        self.calls.append(("bandwidth", value))

    def encode(self, frame, samples):
        if samples != 960 or len(frame) != 3840:
            raise AssertionError("encoder received something other than20ms stereo PCM")
        self.frames.append(frame)
        return ENCODED


class Source:
    def __init__(self, *, packet=RAW, frames=None):
        self.packet = packet
        self.frames = frames
        self.closed = False
        self.last_read_had_audio = False
        self.last_opus_packet = None
        self.opus_metadata = {"channels": 2, "sample_rate": 48000, "pre_skip": 312,
                              "output_gain": 0, "mapping_family": 0,
                              "private_url": "must-never-appear"}

    def read(self):
        if self.frames is None:
            frame = PCM
        else:
            frame = self.frames.pop(0) if self.frames else b""
        self.last_read_had_audio = bool(frame)
        self.last_opus_packet = self.packet if frame else None
        return frame

    def cleanup(self):
        self.closed = True


class HybridOpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_mixer()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.encoder = Encoder()
        self.input = Source()
        self.source = self.module.AgentHybridOpusAudioSource(
            output_policy=self.policy(), output_encoder=self.encoder,
            loop=self.loop, music_source=self.input, music_volume=1,
            persistent=True, telemetry_enabled=True)
        self.source.quality_context = {}
        self.addCleanup(self.source.cleanup)

    @staticmethod
    def policy(**updates):
        return {"bitrate_kbps": 192, "fec": True, "expected_packet_loss": 0.15,
                "opus_direct": "auto", **updates}

    def flush_loop(self):
        self.loop.run_until_complete(asyncio.sleep(0))

    def replace(self, source=None):
        candidate = source or Source()
        self.source.replace_music_source(candidate, volume=1, on_music_end=None,
                                         bassboost_level=0)
        return candidate

    def test_untouched_frames_copy_exact_packets_without_encoding_and_keep_trace(self):
        self.assertTrue(self.source.is_opus())
        self.assertEqual(self.source.opus_direct_status()["state"], "ready")
        for _ in range(4):
            self.assertIs(self.source.read(), RAW)
            self.assertTrue(self.source.last_read_had_music)
            self.assertIs(self.source.last_read_music_source, self.input)
        self.assertEqual(self.encoder.frames, [])
        self.assertIsNotNone(self.source.first_frame_monotonic)
        self.assertEqual(self.source.opus_direct_status()["copied_packets"], 4)
        self.assertTrue(self.source.opus_direct_status()["copy_active"])
        self.assertFalse(self.source.opus_direct_status()["packet_validation_required"])
        self.assertEqual(self.source.opus_direct_status()["copied_bytes"], len(RAW) * 4)
        self.assertIsNone(self.source.quality_context["opus_fec"])
        self.assertEqual(self.source.quality_context["audio_output_state"], "copied")
        self.assertEqual(self.source.audio_telemetry()["audio_frame_count"], 4)

    def test_default_pcm_mixer_is_unchanged(self):
        base = self.module.AgentMixedAudioSource(loop=self.loop, music_source=Source(),
                                                music_volume=1, persistent=True)
        self.addCleanup(base.cleanup)
        self.assertFalse(base.is_opus())
        self.assertEqual(base.read(), PCM)

    def test_volume_fallback_preserves_encoder_and_does_not_return_to_copy(self):
        self.source.read()
        self.source.set_music_volume(0.5)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertNotEqual(self.encoder.frames[-1], PCM)
        self.source.set_music_volume(1)
        for _ in range(40):
            self.assertEqual(self.source.read(), ENCODED)
        self.assertIs(self.source.output_encoder, self.encoder)
        self.assertEqual(self.source.opus_direct_status()["reason"], "volume_requires_pcm")
        self.assertFalse(self.source.opus_direct_status()["copy_active"])
        self.assertEqual(self.source.quality_context["opus_bitrate_kbps"], 192)
        self.assertTrue(self.source.quality_context["opus_fec"])

    def test_tts_reservation_even_if_cancelled_latches_then_next_track_can_copy(self):
        self.source.read()
        self.source.reserve_tts()
        self.source.release_tts()
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.opus_direct_status()["reason"], "tts_reservation")
        self.replace()
        self.assertEqual(self.source.read(), RAW)
        self.assertEqual(self.source.opus_direct_status()["encoded_packets"], 0)

    def test_tts_is_mixed_in_existing_session_and_cancel_does_not_rearm_copy(self):
        self.source.read()
        overlay = Source(packet=None, frames=[PCM, PCM])
        future = self.source.add_tts(overlay, volume=1)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertNotEqual(self.encoder.frames[-1], PCM)
        self.assertTrue(self.source.has_tts())
        self.source.cancel_tts(future)
        self.assertTrue(future.cancelled())
        self.assertTrue(overlay.closed)
        self.assertEqual(self.source.read(), ENCODED)

    def test_tts_finishes_successfully_while_music_packets_stay_continuous(self):
        overlay = Source(packet=None, frames=[PCM])
        future = self.source.add_tts(overlay)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.read(), ENCODED)
        self.flush_loop()
        self.assertIsNone(future.result())
        self.assertTrue(overlay.closed)
        self.assertEqual(len(self.encoder.frames), 2)

    def test_bassboost_fallback_is_latched_after_disabling_effect(self):
        self.source.read()
        self.source.set_bassboost_level(1)
        self.assertEqual(self.source.read(), ENCODED)
        self.source.set_bassboost_level(0)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.opus_direct_status()["reason"], "effects_require_pcm")

    def test_pause_emits_encoded_silence_without_consuming_music(self):
        self.source.set_music_paused(True)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.encoder.frames[-1], bytes(3840))
        self.assertFalse(self.source.last_read_had_music)
        self.assertIsNone(self.input.last_opus_packet)
        self.source.set_music_paused(False)
        self.assertEqual(self.source.read(), ENCODED)

    def test_buffer_underrun_silence_latches_before_first_music(self):
        original_read = self.input.read
        def underrun():
            self.input.read = original_read
            self.input.last_read_had_audio = False
            self.input.last_opus_packet = None
            return bytes(3840)
        self.input.read = underrun
        self.assertEqual(self.source.read(), ENCODED)
        self.assertIsNone(self.source.first_frame_monotonic)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertIsNotNone(self.source.first_frame_monotonic)
        self.assertEqual(self.source.opus_direct_status()["reason"], "synthetic_silence")

    def test_non_opus_pcm_source_after_effect_switch_is_encoded(self):
        self.replace(Source(packet=None))
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.encoder.frames[-1], PCM)
        self.assertEqual(self.source.opus_direct_status()["reason"], "source_requires_pcm")

    def test_source_fallback_reason_is_preserved_without_exposing_arbitrary_text(self):
        self.input.passthrough_reason = "packet_duration"
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.opus_direct_status()["reason"], "source_packet_duration")
        candidate = self.replace()
        candidate.passthrough_reason = "invalid https://private.example/secret-token"
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.opus_direct_status()["reason"], "source_requires_pcm")

    def test_duration_checked_for_each_packet(self):
        self.assertEqual(self.source.read(), RAW)
        self.input.packet = b"\xf0ten-ms"
        self.assertEqual(self.source.read(), ENCODED)
        self.input.packet = RAW
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.opus_direct_status()["copied_packets"], 1)
        self.assertEqual(self.source.opus_direct_status()["reason"], "packet_duration_invalid")

    def test_toc_duration_codes_and_malformed_count(self):
        check = self.source._opus_packet_is_20ms
        for packet in (b"\xf8x", b"\xf1xx", b"\xe3\x08xxxxxxxx", b"\x08x"):
            self.assertTrue(check(packet), packet)
        for packet in (b"", b"\xf0x", b"\xf9xx", b"\xe3", b"\xe3\x00x", b"x" * 1276):
            self.assertFalse(check(packet), packet[:4])

    def test_packet_budget_is_conservative_and_latches_after_overrun(self):
        self.source.set_output_policy(self.policy(bitrate_kbps=96))
        self.input.packet = b"\xf8" + b"x" * 239
        self.assertEqual(self.source.read(), self.input.packet)
        self.input.packet += b"x"
        self.assertEqual(self.source.read(), ENCODED)
        self.source.set_output_policy(self.policy(bitrate_kbps=256))
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.opus_direct_status()["reason"], "packet_bitrate_limit")

    def test_output_policy_updates_ctls_but_never_resets_encoder(self):
        self.encoder.calls.clear()
        self.source.set_output_policy(self.policy(bitrate_kbps=256, expected_packet_loss=0.05))
        self.assertEqual(self.encoder.calls, [("bitrate", 256), ("fec", True),
                                            ("loss", 0.05), ("signal", "music"), ("bandwidth", "full")])
        self.encoder.calls.clear()
        self.source.set_output_policy(self.policy(bitrate_kbps=96), apply_encoder=False)
        self.assertEqual(self.encoder.calls, [])
        self.assertEqual(self.source.opus_direct_status()["copy_packet_limit_bytes"], 240)
        self.assertEqual(self.source.opus_direct_status()["fallback_encoder"]["bitrate_kbps"], 256)
        self.assertEqual(self.source.opus_direct_status()["fallback_encoder"]["expected_packet_loss"], 0.05)
        self.assertIs(self.source.output_encoder, self.encoder)

    def test_partial_external_ctl_result_does_not_claim_unapplied_protection(self):
        self.source.set_output_policy(self.policy(bitrate_kbps=96, fec=False), apply_encoder=False,
                                      applied_encoder={"bitrate_kbps": 96, "fec": None,
                                                       "expected_packet_loss": 0.15, "private": "hidden"})
        self.source.set_music_volume(0.9)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertIsNone(self.source.quality_context["opus_fec"])
        self.assertEqual(self.source.quality_context["audio_output_state"], "partial")
        self.assertEqual(self.source.quality_context["opus_expected_packet_loss"], 0.15)
        self.assertEqual(self.source.quality_context["opus_bitrate_kbps"], 96)
        self.assertNotIn("private", self.source.opus_direct_status()["fallback_encoder"])

    def test_external_signal_ctl_failure_keeps_partial_state_until_confirmed_sync(self):
        actual = {"bitrate_kbps": 192, "fec": True, "expected_packet_loss": 0.15, "state": "partial"}
        self.source.set_output_policy(self.policy(), apply_encoder=False, applied_encoder=actual)
        self.source.set_music_volume(0.9)
        self.source.read()
        self.assertEqual(self.source.quality_context["audio_output_state"], "partial")
        actual["state"] = "applied"
        self.source.set_output_policy(self.policy(), apply_encoder=False, applied_encoder=actual)
        self.source.read()
        self.assertEqual(self.source.quality_context["audio_output_state"], "applied")

    def test_policy_off_is_immediate_and_sticky_until_new_track(self):
        self.source.set_output_policy(self.policy(opus_direct="off"))
        self.assertEqual(self.source.read(), ENCODED)
        self.source.set_output_policy(self.policy())
        self.assertEqual(self.source.read(), ENCODED)
        self.replace()
        self.assertEqual(self.source.read(), RAW)

    def test_pending_tts_prevents_rearming_on_new_track(self):
        self.source.reserve_tts()
        self.replace()
        self.source.release_tts()
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.opus_direct_status()["reason"], "tts_pending")

    def test_music_eof_callback_fires_once_and_mixer_remains_for_tts(self):
        callbacks = []
        self.replace(Source(frames=[PCM]))
        self.source._on_music_end = lambda error, metrics: callbacks.append((error, metrics))
        self.assertEqual(self.source.read(), RAW)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.source.read(), ENCODED)
        self.assertTrue(self.source.music_ended)
        self.assertEqual(len(callbacks), 1)
        self.assertIsNone(callbacks[0][0])
        future = self.source.add_tts(Source(frames=[PCM]))
        self.assertEqual(self.source.read(), ENCODED)
        self.source.cancel_tts(future)

    def test_closed_source_returns_eof_and_cleans_input_and_tts(self):
        overlay = Source()
        future = self.source.add_tts(overlay)
        self.source.cleanup()
        self.source.cleanup()
        self.flush_loop()
        self.assertIsInstance(future.exception(), RuntimeError)
        self.assertTrue(self.input.closed)
        self.assertTrue(overlay.closed)
        self.assertEqual(self.source.read(), b"")
        self.assertEqual(self.encoder.frames, [])

    def test_replaced_source_during_read_never_forwards_stale_packet(self):
        replacement = Source(packet=b"\xf8new")
        original_read = self.input.read
        def replace_during_read():
            frame = original_read()
            self.replace(replacement)
            return frame
        self.input.read = replace_during_read
        self.assertEqual(self.source.read(), ENCODED)
        self.assertEqual(self.encoder.frames[-1], bytes(3840))
        self.assertFalse(self.source.last_read_had_music)
        self.assertEqual(self.source.opus_direct_status()["reason"], "source_changed")
        self.assertEqual(self.source.read(), ENCODED)

    def test_status_whitelists_source_metadata_and_does_not_claim_copy_fec(self):
        self.input.opus_metadata.update(pre_skip_samples=312, sample_rate_hz=48000,
                                       trim_mode="raw_packets_no_sample_exact_trim")
        self.source.read()
        status = self.source.opus_direct_status()
        self.assertIsNone(status["copy_fec"])
        self.assertEqual(status["source"]["pre_skip"], 312)
        self.assertEqual(status["source"]["pre_skip_samples"], 312)
        self.assertEqual(status["source"]["sample_rate_hz"], 48000)
        self.assertNotIn("private_url", status["source"])
        self.assertTrue(status["fallback_encoder"]["fec"])

    def test_constructor_failure_cleans_owned_source(self):
        candidate = Source()
        with mock.patch.object(self.encoder, "set_bitrate", side_effect=RuntimeError("ctl failed")):
            with self.assertRaises(RuntimeError):
                self.module.AgentHybridOpusAudioSource(
                    output_policy=self.policy(), output_encoder=self.encoder,
                    loop=self.loop, music_source=candidate, music_volume=1)
        self.assertTrue(candidate.closed)


if __name__ == "__main__":
    unittest.main()
