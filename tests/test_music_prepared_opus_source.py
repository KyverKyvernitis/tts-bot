"""Native streaming preparation, cache commit and owned process lifecycle.

FFmpeg and libopus execute locally. Only Discord's codec Python boundary is
adapted; these tests do not make a network/perceptual-transparency claim.
"""
from __future__ import annotations

from array import array
import ctypes.util
import io
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.test_music_opus_packets import buffer, NativeDecoder, FakeDecoder, fixture


class CacheWriter:
    def __init__(self):
        self.raw = bytearray()
        self.committed = False
        self.aborted = threading.Event()

    def write(self, raw):
        self.raw.extend(raw)
        return len(raw)

    def commit(self):
        self.committed = True
        return True

    def abort(self):
        self.aborted.set()


class PCM:
    def __init__(self, count=40, waiting=0, partial=False, error=False):
        samples = array('h')
        for i in range(960):
            value = round(9000 * math.sin(2 * math.pi * 500 * i / 48000))
            samples.extend([value, value])
        self.frame = samples.tobytes()
        self.count = count
        self.waiting = waiting
        self.partial = partial
        self.error = error
        self.closed = False
        self.last_read_had_audio = False
        self.activated = False
        self.maximum = None

    def read(self):
        self.last_read_had_audio = False
        if self.closed:
            return b''
        if self.waiting:
            self.waiting -= 1
            return buffer.SILENCE
        if self.count:
            self.count -= 1
            self.last_read_had_audio = True
            return self.frame
        if self.partial:
            self.partial = False
            self.last_read_had_audio = True
            return self.frame[:320]
        if self.error:
            raise ValueError('upstream audio failed')
        return b''

    def activate_playback(self):
        self.activated = True

    def set_max_frames(self, value):
        self.maximum = value

    def cleanup(self):
        self.closed = True


class PreparedOpusValidationTests(unittest.TestCase):
    def test_invalid_rates_fail_before_starting_a_process_and_release_owned_input(self):
        for value in (True, None, 0, 15, 513, 256.0, float('nan'), '256'):
            writer, pcm = CacheWriter(), PCM()
            with self.subTest(value=value), mock.patch.object(buffer.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    buffer.OpusPCMDecoderSource.from_prepared_pcm(
                        pcm, bitrate_kbps=value, decoder_factory=FakeDecoder, cache_writer=writer)
                spawn.assert_not_called()
                self.assertTrue(writer.aborted.is_set())
                self.assertTrue(pcm.closed)

    def test_cache_file_error_hook_is_once_and_never_triggered_by_normal_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prepared.ogg'
            path.write_bytes(b'invalid Ogg')
            hook = mock.Mock()
            source = buffer.OpusPCMDecoderSource.from_prepared_file(
                path, bitrate_kbps=96, source_codec='aac', decoder_factory=FakeDecoder, on_error=hook)
            with self.assertRaises(buffer.OpusPacketError):
                source.read()
            source.cleanup()
            self.assertEqual(source.read(), b'')
            hook.assert_called_once_with()
            self.assertTrue(source.reader.stream.closed)
            path.write_bytes(fixture())
            hook.reset_mock()
            source = buffer.OpusPCMDecoderSource.from_prepared_file(
                path, bitrate_kbps=96, source_codec='aac', decoder_factory=FakeDecoder, on_error=hook)
            source.cleanup()
            hook.assert_not_called()

    def test_cache_packets_must_match_the_prepared_duration_and_cbr_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prepared.ogg'
            for packet in (b'\xf0' + b'a' * 639, b'\xfc' + b'a' * 99,
                           b'\xf8' + b'a' * 999, b'\xf9' + b'a' * 639):
                with self.subTest(length=len(packet), toc=packet[0]):
                    path.write_bytes(fixture([packet]))
                    hook = mock.Mock()
                    source = buffer.OpusPCMDecoderSource.from_prepared_file(
                        path, bitrate_kbps=256, source_codec='aac', decoder_factory=FakeDecoder, on_error=hook)
                    try:
                        with self.assertRaises(buffer.OpusPacketError):
                            source.read()
                        hook.assert_called_once_with()
                        self.assertIsNone(source.last_opus_packet)
                    finally:
                        source.cleanup()


@unittest.skipUnless(shutil.which('ffmpeg') and ctypes.util.find_library('opus'),
                     'FFmpeg and native libopus required')
class NativePreparedOpusSourceTests(unittest.TestCase):
    def test_high_quality_resampling_and_protection_options_keep_valid_bounded_packets(self):
        from types import SimpleNamespace
        quality_filter = 'aresample=48000:resampler=swr:filter_size=64:phase_shift=10:linear_interp=1:exact_rational=1:filter_type=kaiser'
        media = self.media()
        spawn = mock.Mock(side_effect=subprocess.Popen)
        proxy = SimpleNamespace(Popen=spawn, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL)
        with mock.patch.object(buffer, 'subprocess', proxy):
            source = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
                str(media), bitrate_kbps=96, source_codec='aac', decoder_factory=NativeDecoder,
                resample_filter=quality_filter, fec=False, expected_packet_loss=.05)
            try:
                packets, _pcm = self.read_all(source, cap=96)
                self.assertTrue(packets)
                command = spawn.call_args.args[0]
                self.assertEqual(command[command.index('-af') + 1], quality_filter)
                self.assertEqual(command[command.index('-fec') + 1], '0')
                self.assertEqual(command[command.index('-packet_loss') + 1], '5')
                self.assertFalse(source.opus_metadata['prepared_requested_fec'])
                self.assertEqual(source.opus_metadata['prepared_expected_packet_loss'], .05)
            finally:
                source.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='prepared-opus-source-')
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def media(self, codec='aac', *, rate=44100, channels=1, duration=.45):
        ext = {'aac': 'm4a', 'libmp3lame': 'mp3', 'libvorbis': 'ogg', 'libopus': 'ogg'}[codec]
        path = self.folder / f'{codec}-{rate}-{channels}.{ext}'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        f'sine=frequency=440:sample_rate={rate}:duration={duration}',
                        '-ac', str(channels), '-c:a', codec, '-b:a', '128k', str(path)],
                       check=True, capture_output=True, timeout=10)
        return path

    def read_all(self, source, *, cap):
        packets, pcm = [], bytearray()
        while frame := source.read():
            pcm.extend(frame)
            self.assertEqual(len(frame), 3840)
            self.assertTrue(source.last_read_had_audio)
            packet = source.last_opus_packet
            self.assertIsNotNone(packet)
            self.assertEqual(buffer.opus_packet_samples(packet), 960)
            self.assertLessEqual(len(packet), min(1275, int(cap * 2.5)))
            packets.append(packet)
        self.assertTrue(packets)
        self.assertTrue(any(pcm))
        self.assertEqual(source.passthrough_reason, '')
        return packets, bytes(pcm)

    def test_aac_mp3_vorbis_mono_44100_prepare_valid_stereo_20ms_cbr(self):
        for codec in ('aac', 'libmp3lame', 'libvorbis'):
            with self.subTest(codec=codec):
                original = self.media(codec)
                source = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
                    str(original), bitrate_kbps=256, source_codec=codec, decoder_factory=NativeDecoder)
                try:
                    self.read_all(source, cap=256)
                    metadata = source.opus_metadata
                    self.assertTrue(metadata['prepared_opus'])
                    self.assertEqual(metadata['prepared_bitrate_kbps'], 256)
                    self.assertEqual(metadata['prepared_source_codec'], codec)
                    self.assertFalse(metadata['prepared_cache_hit'])
                    self.assertEqual(metadata['sample_rate_hz'], 48000)
                    self.assertEqual(metadata['channels'], 2)
                    self.assertEqual(source._prepared_process.poll(), 0)
                finally:
                    source.cleanup()

    def test_cbr_odd_and_physical_limit_rates_never_exceed_copy_packet_budget(self):
        original = self.media()
        for rate in (16, 96, 131, 256, 512):
            with self.subTest(rate=rate):
                source = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
                    str(original), bitrate_kbps=rate, decoder_factory=NativeDecoder)
                try:
                    self.read_all(source, cap=rate)
                    self.assertEqual(source.opus_metadata['prepared_bitrate_kbps'], min(rate, 510))
                finally:
                    source.cleanup()

    def test_valid_eof_commits_the_same_packets_and_cache_hit_needs_no_ffmpeg(self):
        writer = CacheWriter()
        original = self.media()
        source = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
            str(original), bitrate_kbps=192, source_codec='aac', cache_writer=writer,
            decoder_factory=NativeDecoder)
        try:
            expected, _ = self.read_all(source, cap=192)
            self.assertTrue(writer.committed)
            self.assertFalse(writer.aborted.is_set())
        finally:
            source.cleanup()
        cached = self.folder / 'cached.ogg'
        cached.write_bytes(writer.raw)
        decoder = NativeDecoder()
        with mock.patch.object(buffer.subprocess, 'Popen', side_effect=AssertionError('cache hit must not encode')):
            hit = buffer.OpusPCMDecoderSource.from_prepared_file(
                cached, bitrate_kbps=192, source_codec='aac', decoder_factory=lambda: decoder)
            try:
                actual, _ = self.read_all(hit, cap=192)
                self.assertEqual(actual, expected)
                self.assertTrue(hit.opus_metadata['prepared_cache_hit'])
            finally:
                hit.cleanup()

    def test_cache_write_failure_aborts_cache_but_audio_still_finishes(self):
        writer = CacheWriter()
        writer.write = mock.Mock(side_effect=OSError('disk full'))
        original = self.media()
        source = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
            str(original), bitrate_kbps=96, cache_writer=writer, decoder_factory=NativeDecoder)
        try:
            self.read_all(source, cap=96)
            self.assertTrue(writer.aborted.is_set())
            self.assertFalse(writer.committed)
            writer.write.assert_called_once()
        finally:
            source.cleanup()

    def test_cleanup_before_eof_aborts_cache_and_kills_owned_process(self):
        writer = CacheWriter()
        original = self.media(duration=10)
        source = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
            str(original), bitrate_kbps=256, cache_writer=writer, decoder_factory=NativeDecoder)
        self.assertTrue(source.read())
        process = source._prepared_process
        source.cleanup()
        self.assertTrue(writer.aborted.wait(1))
        self.assertFalse(writer.committed)
        self.assertIsNotNone(process.wait(timeout=1))
        self.assertTrue(process.stdout.closed)
        self.assertEqual(source.read(), b'')

    def test_continuous_pcm_skips_synthetic_wait_and_preserves_short_final_chunk(self):
        pcm = PCM(waiting=4, partial=True)
        writer = CacheWriter()
        source = buffer.OpusPCMDecoderSource.from_prepared_pcm(
            pcm, bitrate_kbps=128, source_codec='aac', cache_writer=writer, decoder_factory=NativeDecoder)
        source.activate_playback()
        source.set_max_frames(17)
        try:
            packets, _ = self.read_all(source, cap=128)
            self.assertGreaterEqual(len(packets), 41)
            self.assertLessEqual(len(packets), 42)  # four synthetic waits were not encoded
            self.assertTrue(writer.committed)
            self.assertTrue(pcm.activated)
            self.assertEqual(pcm.maximum, 17)
        finally:
            source.cleanup()
        deadline = time.monotonic() + 1
        while not pcm.closed and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(pcm.closed)

    def test_continuous_pcm_emits_first_audio_before_the_input_reaches_eof(self):
        class GatedPCM(PCM):
            def __init__(self):
                super().__init__(count=10)
                self.release = threading.Event()
            def read(self):
                if self.count:
                    return super().read()
                self.release.wait(2)
                return b''
            def cleanup(self):
                super().cleanup()
                self.release.set()
        pcm = GatedPCM()
        source = buffer.OpusPCMDecoderSource.from_prepared_pcm(
            pcm, bitrate_kbps=128, decoder_factory=NativeDecoder)
        ready, results = threading.Event(), []
        def read_first():
            try:
                results.append(source.read())
            except Exception as exc:
                results.append(exc)
            finally:
                ready.set()
        thread = threading.Thread(target=read_first, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(1), 'first frame must not wait for complete input')
            self.assertIsInstance(results[0], bytes)
            self.assertEqual(len(results[0]), 3840)
            self.assertFalse(pcm.release.is_set(), 'source is still open while audio starts')
        finally:
            source.cleanup()
            pcm.release.set()
            thread.join(timeout=1)

    def test_cleanup_does_not_wait_for_a_cache_commit_or_writer_join(self):
        writer = CacheWriter()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def commit():
            entered.set()
            release.wait(2)
            writer.committed = True
            return True
        writer.commit = commit
        original = self.media()
        source = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
            str(original), bitrate_kbps=128, cache_writer=writer, decoder_factory=NativeDecoder)
        def consume():
            try:
                while source.read():
                    pass
            except Exception:
                pass
            finally:
                finished.set()
        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            source.cleanup()
            self.assertLess(time.monotonic() - started, .1)
        finally:
            release.set()
            source.cleanup()
            thread.join(timeout=2)
        self.assertTrue(finished.is_set())

    def test_upstream_pcm_failure_is_not_committed_as_a_successful_short_track(self):
        writer, pcm = CacheWriter(), PCM(count=4, error=True)
        source = buffer.OpusPCMDecoderSource.from_prepared_pcm(
            pcm, bitrate_kbps=128, cache_writer=writer, decoder_factory=NativeDecoder)
        try:
            with self.assertRaisesRegex(ValueError, 'upstream audio'):
                self.read_all(source, cap=128)
            self.assertTrue(writer.aborted.wait(1))
            self.assertFalse(writer.committed)
        finally:
            source.cleanup()

    def test_original_opus_copy_does_not_encode_or_claim_prepared_provenance(self):
        original = self.media('libopus', rate=48000, channels=2)
        source = buffer.OpusPCMDecoderSource.from_ffmpeg(str(original), decoder_factory=NativeDecoder)
        expected_reader = buffer.OggOpusPacketReader(io.BytesIO(original.read_bytes()))
        expected = []
        while packet := expected_reader.read_packet():
            expected.append(packet)
        try:
            actual, _ = self.read_all(source, cap=512)
            self.assertEqual(actual, expected)
            self.assertNotIn('prepared_opus', source.opus_metadata)
        finally:
            source.cleanup()


if __name__ == '__main__':
    unittest.main()
