"""Prepared-source processing requests and audio-boundary ownership transfer.

The mixer is production code. Most cases adapt only codec/source boundaries;
the native case compares its PCM with an uninterrupted mixer and uses libopus.
No network, phone timing or perceptual-transparency claim is made here.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes.util
import asyncio
import threading
import unittest
from unittest import mock

from tests import test_music_opus_hybrid as helpers
from tests import test_music_opus_processed_native as native
from tests.test_music_opus_packets import NativeDecoder, buffer


class OriginalPCMHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mixer = helpers.load_mixer()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.prepared = helpers.Source()
        self.prepared._prepared_opus_source = True
        self.prepared.request_original_pcm = mock.Mock()
        self.encoder = helpers.Encoder()
        self.output = self.mixer.AgentHybridOpusAudioSource(
            loop=self.loop, music_source=self.prepared, music_volume=1,
            persistent=True, telemetry_enabled=False, output_encoder=self.encoder,
            output_policy={'bitrate_kbps': 96, 'copy_bitrate_kbps': 256,
                           'channel_bitrate_kbps': 256, 'opus_direct': 'auto',
                           'fec': True, 'expected_packet_loss': .15})
        self.addCleanup(self.prepared.cleanup)
        self.addCleanup(self.output.cleanup)

    def candidate(self):
        source = helpers.Source(packet=None)
        self.addCleanup(source.cleanup)
        return source

    def test_processing_requests_once_for_current_prepared_source_and_generation(self):
        self.output.set_music_volume(.55)
        self.output.set_music_volume(.7)
        self.output.set_bassboost_level(2)
        self.output.reserve_tts()
        speech = self.candidate()
        self.output.add_tts(speech)
        self.output.read()
        self.prepared.request_original_pcm.assert_called_once_with('volume_requires_pcm')
        self.output.replace_music_source(self.prepared, volume=.55, on_music_end=None)
        self.assertEqual(self.prepared.request_original_pcm.call_count, 2)

    def test_tts_reservation_or_overlay_requests_original_once(self):
        for action in ('reserve', 'overlay'):
            with self.subTest(action=action):
                self.setUp()
                if action == 'reserve':
                    self.output.reserve_tts()
                    reason = 'tts_reservation'
                else:
                    self.output.add_tts(self.candidate())
                    reason = 'tts_overlay'
                self.prepared.request_original_pcm.assert_called_once_with(reason)

    def test_pause_does_not_request_but_later_processing_still_does(self):
        self.output.set_music_paused(True)
        self.prepared.request_original_pcm.assert_not_called()
        self.output.set_music_volume(.55)
        self.prepared.request_original_pcm.assert_called_once_with('volume_requires_pcm')

    def test_stop_disabled_policy_and_invalid_packet_are_not_processing_requests(self):
        self.output.set_output_policy({'bitrate_kbps': 96, 'opus_direct': 'off'})
        self.output.stop_music()
        self.prepared.request_original_pcm.assert_not_called()
        self.setUp()
        self.prepared.packet = b'\x18invalid-duration'
        self.output.read()
        self.prepared.request_original_pcm.assert_not_called()

    def test_unprepared_source_and_missing_hook_are_not_marked_as_requested(self):
        self.prepared._prepared_opus_source = False
        self.output.set_music_volume(.55)
        self.prepared.request_original_pcm.assert_not_called()
        self.prepared._prepared_opus_source = True
        callback = self.prepared.request_original_pcm
        self.prepared.request_original_pcm = None
        self.output.set_music_volume(.7)
        self.prepared.request_original_pcm = callback
        self.output.set_music_volume(.8)
        callback.assert_called_once_with('volume_requires_pcm')

    def test_failed_notification_does_not_escape_or_create_a_request_storm(self):
        self.prepared.request_original_pcm.side_effect = RuntimeError('callback failure')
        self.output.set_music_volume(.55)
        self.assertEqual(self.output.read(), helpers.ENCODED)
        self.output.set_bassboost_level(1)
        self.prepared.request_original_pcm.assert_called_once()

    def test_reset_allows_retry_only_on_a_later_processing_control(self):
        self.output.set_music_volume(.55)
        self.prepared.request_original_pcm.assert_called_once()
        self.assertFalse(self.output.reset_original_pcm_request(self.candidate()))
        self.assertTrue(self.output.reset_original_pcm_request(self.prepared))
        for _ in range(3):
            self.output.read()
        self.prepared.request_original_pcm.assert_called_once()
        self.output.set_bassboost_level(2)
        self.assertEqual(self.prepared.request_original_pcm.call_count, 2)
        self.assertEqual(self.prepared.request_original_pcm.call_args.args, ('effects_require_pcm',))
        self.output.stop_music()
        self.assertFalse(self.output.reset_original_pcm_request(self.prepared))

    def test_handoff_preserves_dsp_clock_callback_encoder_counters_and_tts(self):
        self.assertEqual(self.output.read(), helpers.RAW)
        self.output.set_music_volume(.55)
        self.output.set_bassboost_level(2)
        self.output.read()
        speech = self.candidate()
        future = self.output.add_tts(speech)
        future.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
        callback = self.output._on_music_end = mock.Mock()
        candidate = self.candidate()
        expected = {
            'gain': self.output._music_gain, 'mix_gain': self.output._mix_gain,
            'bass_filter': deepcopy(self.output._bass_filter), 'bass_mix': self.output._bass_mix,
            'bass_limiter': self.output._bass_limiter_gain, 'started': self.output._started_monotonic,
            'first': self.output.first_frame_monotonic, 'copied': self.output._opus_copied_packets,
            'encoded': self.output._opus_encoded_packets,
        }
        results = []
        def committed(success):
            results.append(success)
            self.assertEqual(self.output._music_gain, expected['gain'])
            self.assertEqual(self.output._mix_gain, expected['mix_gain'])
            self.assertEqual(self.output._bass_filter, expected['bass_filter'])
            self.assertEqual(self.output._bass_mix, expected['bass_mix'])
            self.assertEqual(self.output._bass_limiter_gain, expected['bass_limiter'])
            self.assertEqual(self.output._started_monotonic, expected['started'])
            self.assertEqual(self.output.first_frame_monotonic, expected['first'])
            self.assertEqual(self.output._opus_copied_packets, expected['copied'])
            self.assertEqual(self.output._opus_encoded_packets, expected['encoded'])
            self.assertFalse(self.output.last_read_had_music)
        self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, committed))
        self.assertIs(self.output.music_source, self.prepared)
        generation = self.output._opus_generation
        self.output.read()
        self.assertEqual(results, [True])
        self.assertIs(self.output.music_source, candidate)
        self.assertFalse(self.prepared.closed, 'the routing owner cleans the old source off-loop')
        self.assertIs(self.output.output_encoder, self.encoder)
        self.assertIs(self.output._on_music_end, callback)
        self.assertEqual(self.output._opus_generation, generation + 1)
        self.assertTrue(self.output.has_tts())
        self.assertFalse(future.done())
        self.assertTrue(self.output.opus_direct_status()['processed_active'])

    def test_dedicated_handoff_slot_does_not_replace_encoder_update(self):
        candidate, events = self.candidate(), []
        self.output.queue_encoder_update(lambda: events.append('ctl'))
        self.assertTrue(self.output.queue_music_source_handoff(
            self.prepared, candidate, lambda result: events.append(('handoff', result))))
        self.output.read()
        self.assertEqual(events, [('handoff', True), 'ctl'])
        self.assertEqual(self.encoder.frames[-1], helpers.PCM)

    def test_cancel_and_validation_rejection_leave_candidate_owned_by_caller(self):
        candidate, results = self.candidate(), []
        self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, results.append))
        self.assertTrue(self.output.cancel_music_source_handoff(self.prepared, candidate))
        self.assertFalse(self.output.cancel_music_source_handoff(self.prepared, candidate))
        self.assertEqual(results, [False])
        self.assertFalse(candidate.closed)
        self.assertEqual(self.output.read(), helpers.RAW)
        self.assertTrue(self.output.queue_music_source_handoff(
            self.prepared, candidate, results.append, validate=lambda: False))
        self.assertEqual(self.output.read(), helpers.RAW)
        self.assertEqual(results, [False, False])
        self.assertIs(self.output.music_source, self.prepared)

    def test_validation_and_completion_failures_are_isolated_from_audio(self):
        candidate = self.candidate()
        callback = mock.Mock(side_effect=RuntimeError('result failure'))
        validator = mock.Mock(side_effect=RuntimeError('validation failure'))
        self.assertTrue(self.output.queue_music_source_handoff(
            self.prepared, candidate, callback, validate=validator))
        self.assertEqual(self.output.read(), helpers.RAW)
        callback.assert_called_once_with(False)
        self.assertIs(self.output.music_source, self.prepared)
        self.assertFalse(candidate.closed)
        callback.reset_mock()
        self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, callback))
        self.assertEqual(self.output.read(), helpers.ENCODED)
        callback.assert_called_once_with(True)
        self.assertIs(self.output.music_source, candidate)

    def test_handoff_of_paused_music_keeps_pause_and_does_not_consume_candidate(self):
        self.output.set_music_paused(True)
        candidate, results = self.candidate(), []
        candidate.read = mock.Mock(wraps=candidate.read)
        self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, results.append))
        self.assertEqual(self.output.read(), helpers.ENCODED)
        self.assertEqual(results, [True])
        candidate.read.assert_not_called()
        self.assertTrue(self.output._music_paused)
        self.assertFalse(self.output.opus_direct_status()['processed_active'])
        self.output.set_music_paused(False)
        self.output.read()
        candidate.read.assert_called_once()

    def test_stop_replacement_and_cleanup_reject_pending_without_cleaning_candidate(self):
        for action in ('stop', 'replace', 'cleanup'):
            with self.subTest(action=action):
                self.setUp()
                candidate, results = self.candidate(), []
                self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, results.append))
                if action == 'stop':
                    self.output.stop_music()
                elif action == 'replace':
                    self.output.replace_music_source(self.candidate(), volume=.55, on_music_end=None)
                else:
                    self.output.cleanup()
                self.assertEqual(results, [False])
                self.assertFalse(candidate.closed)
                self.assertFalse(self.output.cancel_music_source_handoff(self.prepared, candidate))

    def test_queue_checks_source_ownership_and_has_only_one_pending_slot(self):
        candidate, other = self.candidate(), self.candidate()
        callback = mock.Mock()
        self.assertFalse(self.output.queue_music_source_handoff(other, candidate, callback))
        self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, callback))
        self.assertFalse(self.output.queue_music_source_handoff(self.prepared, other, callback))
        self.output.read()
        callback.assert_called_once_with(True)
        self.assertFalse(self.output.handoff_music_source(self.prepared, other))
        self.assertIs(self.output.music_source, candidate)

    def test_input_provenance_changes_only_when_handoff_commits_and_is_not_output_proof(self):
        status = self.output.opus_direct_status()
        self.assertEqual(status['pcm_origin'], 'prepared_opus')
        self.assertFalse(status['copy_active'])
        self.assertFalse(status['processed_active'])
        candidate = self.candidate()
        candidate._original_pcm_handoff = True
        self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, lambda result: None))
        self.assertEqual(self.output.opus_direct_status()['pcm_origin'], 'prepared_opus')
        self.output.read()
        status = self.output.opus_direct_status()
        self.assertEqual(status['pcm_origin'], 'original')
        self.assertTrue(status['processed_active'])
        self.assertFalse(status['copy_active'])
        self.output.replace_music_source(self.candidate(), volume=1, on_music_end=None)
        status = self.output.opus_direct_status()
        self.assertEqual(status['pcm_origin'], 'source')
        self.assertFalse(status['copy_active'])
        self.assertFalse(status['processed_active'])

    def test_inflight_old_read_finishes_before_queued_handoff_without_a_silence_tick(self):
        entered, release = threading.Event(), threading.Event()
        original_read = self.prepared.read
        def blocking_read():
            entered.set()
            if not release.wait(1):
                raise TimeoutError('test reader not released')
            return original_read()
        self.prepared.read = blocking_read
        observed, errors, results = [], [], []
        def read_once():
            try:
                observed.append(self.output.read())
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=read_once, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            candidate = self.candidate()
            self.assertTrue(self.output.queue_music_source_handoff(self.prepared, candidate, results.append))
            self.assertIs(self.output.music_source, self.prepared)
        finally:
            release.set()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, [helpers.RAW])
        self.assertEqual(results, [])
        self.assertEqual(self.output.read(), helpers.ENCODED)
        self.assertEqual(results, [True])
        self.assertEqual(self.encoder.frames[-1], helpers.PCM)


@unittest.skipUnless(ctypes.util.find_library('opus'), 'native libopus required')
class NativeOriginalPCMHandoffTests(unittest.TestCase):
    def test_aligned_handoff_matches_uninterrupted_pcm_with_one_encode_per_frame(self):
        mixer = helpers.load_mixer()
        loop = asyncio.new_event_loop()
        encoder = native.CaptureNativeEncoder()
        old, reference_source, new = native.TonePCM(), native.TonePCM(), native.TonePCM()
        output = mixer.AgentHybridOpusAudioSource(
            loop=loop, music_source=old, music_volume=.55, bassboost_level=2,
            persistent=True, telemetry_enabled=False, output_encoder=encoder,
            output_policy={'bitrate_kbps': 96, 'copy_bitrate_kbps': 256,
                           'channel_bitrate_kbps': 256, 'opus_direct': 'auto',
                           'fec': True, 'expected_packet_loss': .15})
        reference = mixer.AgentMixedAudioSource(loop=loop, music_source=reference_source,
            music_volume=.55, bassboost_level=2, persistent=True, telemetry_enabled=False)
        receiver = NativeDecoder()
        results = []
        try:
            for _ in range(4):
                self.assertEqual(buffer.opus_packet_samples(output.read()), 960)
                self.assertEqual(encoder.frames[-1], reference.read())
            new.position = old.position
            started = output._started_monotonic
            self.assertTrue(output.queue_music_source_handoff(old, new, results.append))
            for _ in range(4):
                packet = output.read()
                self.assertEqual(len(receiver.decode(packet, fec=False)), 3840)
                self.assertEqual(encoder.frames[-1], reference.read())
            self.assertEqual(results, [True])
            self.assertEqual(len(encoder.frames), 8)
            self.assertEqual(output._started_monotonic, started)
            self.assertIs(output.output_encoder, encoder)
            self.assertEqual(output.opus_direct_status()['encoded_packets'], 8)
        finally:
            old.cleanup()
            output.cleanup()
            reference.cleanup()
            encoder.close()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()


if __name__ == '__main__':
    unittest.main()
