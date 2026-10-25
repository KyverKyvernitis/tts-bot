"""Continuous original Opus packets across archive parts, without Discord I/O.

Native checks generate real FFmpeg Opus, apply the production archive preroll,
and compare every packet and decoded PCM frame with the unsegmented original.
"""
from __future__ import annotations

import ctypes.util
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_music_opus_packets import buffer, FakeDecoder, NativeDecoder, fixture


ROOT = Path(__file__).resolve().parents[1]


def segmented_fixture():
    packets = [bytes([252, 1, index]) for index in range(1, 13)]
    parts = [fixture(packets[:5], end_granule=4800),
             fixture(packets[1:9], preskip=3840, end_granule=7680),
             fixture(packets[5:], preskip=3840, end_granule=6408)]
    counts = [4488, 3840, 2568]
    segments, offset = [], 0
    for order, count in enumerate(counts):
        segments.append({'order': order, 'offset_seconds': offset / 48000,
                         'duration': count / 48000, 'audio_codec': 'opus',
                         'audio_sample_rate': 48000, 'audio_channels': 2})
        offset += count
    return packets, parts, segments


def drain(decoded):
    pcm, packets = [], []
    while frame := decoded.read():
        pcm.append(frame)
        packets.append(decoded.last_opus_packet)
    return pcm, packets


class ArchiveOpusPacketTests(unittest.TestCase):
    def setUp(self):
        self.packets, self.parts, self.segments = segmented_fixture()
        self.opened, self.streams = [], []

    def make_reader(self):
        def open_segment(index):
            self.opened.append(index)
            stream = io.BytesIO(self.parts[index])
            self.streams.append(stream)
            return stream
        reader = buffer.ArchiveOpusPacketReader(self.segments, open_segment)
        self.addCleanup(reader.cleanup)
        return reader

    def test_exact_packet_identity_and_one_decoder_across_three_parts(self):
        reader = self.make_reader()
        decoded = buffer.OpusPCMDecoderSource(reader, decoder_factory=FakeDecoder)
        self.addCleanup(decoded.cleanup)
        decoder = decoded.decoder
        pcm, observed = drain(decoded)
        self.assertEqual(observed, self.packets)
        self.assertIs(decoded.decoder, decoder)
        self.assertEqual(pcm, [bytes([index]) * 3840 for index in range(1, 13)])
        self.assertEqual(self.opened, [0, 1, 2])
        self.assertEqual(decoded.opus_metadata['archive_preroll_packets_skipped'], 8)
        self.assertEqual(decoded.opus_metadata['archive_preroll_samples_skipped'], 7680)
        self.assertEqual(decoded.opus_metadata['archive_segments_completed'], 3)
        self.assertEqual(decoded.opus_metadata['archive_segments_total'], 3)
        self.assertEqual(decoded.opus_metadata['raw_duration_samples'], 11520)
        self.assertEqual(decoded.opus_metadata['tail_padding_samples'], 312)
        self.assertTrue(all(stream.closed for stream in self.streams))

    def test_parts_are_opened_lazily_and_only_current_is_owned(self):
        reader = self.make_reader()
        self.assertEqual(self.opened, [])
        self.assertEqual(reader.read_packet(), self.packets[0])
        self.assertEqual(self.opened, [0])
        for packet in self.packets[1:5]:
            self.assertEqual(reader.read_packet(), packet)
        self.assertEqual(self.opened, [0])
        self.assertEqual(reader.read_packet(), self.packets[5])
        self.assertEqual(self.opened, [0, 1])
        self.assertTrue(self.streams[0].closed)
        self.assertFalse(self.streams[1].closed)

    def test_prefix_bytes_must_match_not_only_preskip_count(self):
        self.parts[1] = fixture([b'\xfc\x01\xff', *self.packets[2:9]], preskip=3840, end_granule=7680)
        reader = self.make_reader()
        decoded = buffer.OpusPCMDecoderSource(reader, decoder_factory=FakeDecoder)
        for packet in self.packets[:5]:
            self.assertTrue(decoded.read())
            self.assertEqual(decoded.last_opus_packet, packet)
        with self.assertRaisesRegex(buffer.OpusPacketError, 'preroll'):
            decoded.read()
        self.assertIsNone(decoded.last_opus_packet)
        self.assertEqual(decoded._decoded_samples, 4800)

    def test_standalone_large_preskip_remains_rejected(self):
        standalone = buffer.OpusPCMDecoderSource(
            buffer.OggOpusPacketReader(io.BytesIO(self.parts[1])), decoder_factory=FakeDecoder)
        with self.assertRaisesRegex(buffer.OpusPacketError, 'pre-skip'):
            standalone.read()

    def test_original_initial_preskip_is_still_bounded(self):
        self.parts[0] = fixture(self.packets[:5], preskip=1920, end_granule=4800)
        self.segments[0]['duration'] = 2880 / 48000
        self.segments[1]['offset_seconds'] = 2880 / 48000
        self.segments[2]['offset_seconds'] = (2880 + 3840) / 48000
        with self.assertRaisesRegex(buffer.OpusPacketError, 'pre-skip inicial'):
            self.make_reader().read_packet()

    def test_intermediate_tail_trim_is_rejected_before_forwarding_that_page(self):
        self.parts[0] = fixture(self.packets[:5], end_granule=4488)
        with self.assertRaisesRegex(buffer.OpusPacketError, 'corte final'):
            self.make_reader().read_packet()

    def test_duration_mismatch_is_rejected_before_forwarding_eos_page(self):
        self.parts[1] = fixture(self.packets[1:9], preskip=3840, end_granule=7679)
        reader = self.make_reader()
        for _ in range(5):
            reader.read_packet()
        with self.assertRaisesRegex(buffer.OpusPacketError, 'duração ou corte'):
            reader.read_packet()

    def test_corrupt_later_part_is_never_forwarded(self):
        raw = bytearray(self.parts[1])
        raw[-1] ^= 1
        self.parts[1] = bytes(raw)
        reader = self.make_reader()
        for _ in range(5):
            reader.read_packet()
        with self.assertRaisesRegex(buffer.OpusPacketError, 'CRC'):
            reader.read_packet()

    def test_legacy_repeated_partial_preskip_falls_back_safely(self):
        self.parts[1] = fixture(self.packets[5:9], preskip=312, end_granule=3840)
        self.segments[1]['duration'] = (3840 - 312) / 48000
        self.segments[2]['offset_seconds'] = (4488 + 3840 - 312) / 48000
        reader = self.make_reader()
        for _ in range(5):
            reader.read_packet()
        with self.assertRaisesRegex(buffer.OpusPacketError, 'preroll'):
            reader.read_packet()

    def test_manifest_order_offset_profile_and_nonfinite_values_are_rejected(self):
        for patch in ({'order': 9}, {'offset_seconds': .01}, {'duration': 0},
                      {'duration': True}, {'duration': float('nan')}, {'duration': 1e308},
                      {'audio_codec': 'vorbis'}, {'audio_channels': 1}, {'audio_sample_rate': 44100}):
            with self.subTest(patch=patch):
                segments = [dict(part) for part in self.segments]
                segments[0].update(patch)
                with self.assertRaises(buffer.OpusPacketError):
                    buffer.ArchiveOpusPacketReader(segments, lambda index: None)

    def test_final_trim_cannot_exceed_last_packet(self):
        self.parts[2] = fixture(self.packets[5:], preskip=3840, end_granule=5500)
        self.segments[2]['duration'] = (5500 - 3840) / 48000
        reader = self.make_reader()
        for _ in range(9):
            reader.read_packet()
        with self.assertRaisesRegex(buffer.OpusPacketError, 'corte final'):
            reader.read_packet()

    def test_cleanup_before_open_and_during_open_never_leaks_stream(self):
        reader = self.make_reader()
        reader.cleanup()
        reader.cleanup()
        self.assertEqual(reader.read_packet(), b'')
        self.assertEqual(self.opened, [])
        stream = io.BytesIO(self.parts[0])
        def open_and_close(index):
            second.cleanup()
            return stream
        second = buffer.ArchiveOpusPacketReader(self.segments, open_and_close)
        self.assertEqual(second.read_packet(), b'')
        self.assertTrue(stream.closed)

    def test_cleanup_between_open_and_publication_still_closes_new_stream(self):
        stream = io.BytesIO(self.parts[0])
        class RaceReader(buffer.ArchiveOpusPacketReader):
            def __setattr__(self, name, value):
                if name == '_current' and value is not None and not self._closed:
                    self.cleanup()  # deterministic interleaving before publish
                super().__setattr__(name, value)
        reader = RaceReader(self.segments, lambda index: stream)
        self.assertEqual(reader.read_packet(), b'')
        self.assertTrue(stream.closed)


@unittest.skipUnless(shutil.which('ffmpeg') and ctypes.util.find_library('opus'),
                     'FFmpeg and native libopus required')
class NativeArchiveOpusPacketTests(unittest.TestCase):
    def make_archive(self, directory, frame_ms=20):
        path = Path(directory)
        original = path / 'original.ogg'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=437:sample_rate=48000:duration=2.17', '-ac', '2',
                        '-c:a', 'libopus', '-b:a', '128k', '-frame_duration', str(frame_ms), str(original)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(original), '-map', '0:a:0',
                        '-c:a', 'copy', '-f', 'segment', '-segment_format', 'ogg',
                        '-segment_time', '0.7', '-reset_timestamps', '1', str(path / 'part-%03d.ogg')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        parts = sorted(path.glob('part-*.ogg'))
        spec = importlib.util.spec_from_file_location('native_archive_preroll',
            ROOT / 'cogs/musica/runtime_telefone/agente/archive_ogg.py')
        archive = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(archive)
        durations = archive.add_opus_preroll(original, parts)
        segments, offset = [], 0.0
        for order, seconds in enumerate(durations):
            segments.append({'order': order, 'duration': seconds, 'offset_seconds': offset})
            offset += seconds
        return original, parts, segments

    def test_real_archive_copy_is_packet_and_pcm_identical_to_original(self):
        with tempfile.TemporaryDirectory() as directory:
            original, parts, segments = self.make_archive(directory)
            self.assertGreaterEqual(len(parts), 3)
            original_source = buffer.OpusPCMDecoderSource(
                buffer.OggOpusPacketReader(io.BytesIO(original.read_bytes())), decoder_factory=NativeDecoder)
            self.addCleanup(original_source.cleanup)
            expected_pcm, expected_packets = drain(original_source)
            reader = buffer.ArchiveOpusPacketReader(segments, lambda index: parts[index].open('rb'))
            source = buffer.OpusPCMDecoderSource(reader, decoder_factory=NativeDecoder)
            self.addCleanup(source.cleanup)
            actual_pcm, actual_packets = drain(source)
            self.assertEqual(actual_packets, expected_packets)
            self.assertEqual(actual_pcm, expected_pcm)
            self.assertEqual(source.opus_metadata['pre_skip_samples'],
                             original_source.opus_metadata['pre_skip_samples'])
            self.assertEqual(source.opus_metadata['tail_padding_samples'],
                             original_source.opus_metadata['tail_padding_samples'])
            self.assertEqual(source.opus_metadata['archive_segments_completed'], len(parts))

    def test_real_40ms_parts_keep_continuous_pcm_without_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            original, parts, segments = self.make_archive(directory, frame_ms=40)
            expected = buffer.OpusPCMDecoderSource(
                buffer.OggOpusPacketReader(io.BytesIO(original.read_bytes())), decoder_factory=NativeDecoder)
            self.addCleanup(expected.cleanup)
            expected_pcm, _ = drain(expected)
            reader = buffer.ArchiveOpusPacketReader(segments, lambda index: parts[index].open('rb'))
            source = buffer.OpusPCMDecoderSource(reader, decoder_factory=NativeDecoder)
            self.addCleanup(source.cleanup)
            actual_pcm, packets = drain(source)
            self.assertEqual(actual_pcm, expected_pcm)
            self.assertTrue(all(packet is None for packet in packets))
            self.assertEqual(source.passthrough_reason, 'packet_duration')


if __name__ == '__main__':
    unittest.main()
