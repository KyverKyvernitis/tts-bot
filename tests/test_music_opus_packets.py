"""Bounded real packet pipeline; only the Discord AudioSource boundary is adapted.

Pure container cases use a deterministic decoder double. Native cases generate
Opus with installed FFmpeg and decode through libopus, including 10/40 ms PCM
fallback. No Discord/network/perceptual-quality result is claimed here.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import importlib.util
import io
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_buffer():
    spec = importlib.util.spec_from_file_location("opus_packets_real_buffer", ROOT / "cogs/musica/runtime_telefone/agente/buffer_pcm.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"discord": SimpleNamespace(AudioSource=object)}):
        spec.loader.exec_module(module)
    return module


buffer = load_buffer()


def page(packets=(), *, serial=7, sequence=0, flags=0, granule=0, lacing=None, body=None):
    if lacing is None:
        table = []
        raw = bytearray()
        for packet in packets:
            table.extend([255] * (len(packet) // 255) + [len(packet) % 255])
            raw.extend(packet)
        lacing, body = bytes(table), bytes(raw)
    header = b"OggS" + bytes([0, flags]) + struct.pack("<QIII", granule, serial, sequence, 0) + bytes([len(lacing)])
    raw = bytearray(header + lacing + body)
    struct.pack_into("<I", raw, 22, buffer._ogg_crc(raw))
    return bytes(raw)


def head(*, channels=2, preskip=312, gain=0, mapping=0, version=1, rate=48000):
    return b"OpusHead" + bytes([version, channels]) + struct.pack("<HIhB", preskip, rate, gain, mapping)


def tags():
    return b"OpusTags" + struct.pack("<II", 0, 0)


def fixture(packets=(b"\xfc\x01\x02",), *, end_granule=None, **header):
    if end_granule is None:
        try:
            end_granule = sum(buffer.opus_packet_samples(packet) for packet in packets)
        except buffer.OpusPacketError:
            end_granule = 960 * len(packets)
    return (page([head(**header)], flags=2) + page([tags()], sequence=1)
            + page(packets, sequence=2, flags=4, granule=end_granule))


class FakeDecoder:
    def decode(self, packet, *, fec=False):
        return bytes([packet[-1]]) * (buffer.opus_packet_samples(packet) * 4)


def source(raw, decoder_factory=FakeDecoder):
    return buffer.OpusPCMDecoderSource(buffer.OggOpusPacketReader(io.BytesIO(raw)), decoder_factory=decoder_factory)


class OpusContainerTests(unittest.TestCase):
    def test_crc_matches_known_ogg_polynomial_vector(self):
        self.assertEqual(buffer._ogg_crc(b"123456789"), 0x89A1897F)

    def test_validated_packets_keep_exact_bytes_and_header_metadata(self):
        packet = b"\xfc\x01\x02"
        decoded = source(fixture([packet], rate=44100))
        self.assertEqual(decoded.read(), b"\x02" * 3840)
        self.assertEqual(decoded.last_opus_packet, packet)
        self.assertEqual(decoded.opus_metadata["sample_rate_hz"], 48000)
        self.assertEqual(decoded.opus_metadata["input_sample_rate_hz"], 44100)
        self.assertEqual(decoded.opus_metadata["pre_skip_samples"], 312)
        self.assertIn("no_sample_exact", decoded.opus_metadata["trim_mode"])
        self.assertEqual(decoded.read(), b"")
        self.assertIsNone(decoded.last_opus_packet)

    def test_invalid_head_is_rejected_before_decode(self):
        for kwargs in ({"channels": 1}, {"channels": 6}, {"mapping": 1}, {"gain": 1},
                       {"version": 2}, {"preskip": 961}):
            with self.subTest(kwargs=kwargs):
                decoder = mock.Mock()
                decoded = source(fixture(**kwargs), decoder_factory=lambda: decoder)
                with self.assertRaises(buffer.OpusPacketError):
                    decoded.read()
                decoder.decode.assert_not_called()
                self.assertIsNone(decoded.last_opus_packet)

    def test_corrupt_crc_is_rejected(self):
        raw = bytearray(fixture())
        raw[-1] ^= 1
        with self.assertRaisesRegex(buffer.OpusPacketError, "CRC"):
            source(raw).read()

    def test_truncated_page_is_rejected(self):
        raw = fixture()
        for length in (1, 25, 29, len(raw) - 1):
            with self.subTest(length=length), self.assertRaises(buffer.OpusPacketError):
                source(raw[:length]).read()

    def test_unknown_flags_and_nonzero_ogg_version_are_rejected(self):
        for offset, value in ((4, 1), (5, 10)):
            raw = bytearray(fixture())
            raw[offset] = value
            with self.assertRaises(buffer.OpusPacketError):
                source(raw).read()

    def test_page_sequence_serial_and_continuation_are_strict(self):
        prefix = page([head()], flags=2)
        for tail in (page([tags()], sequence=2), page([tags()], sequence=1, serial=8),
                     page([tags()], sequence=1, flags=1), page([tags()], sequence=1, flags=2)):
            with self.subTest(tail=tail[:27]), self.assertRaises(buffer.OpusPacketError):
                source(prefix + tail).read()

    def test_head_must_be_alone_in_first_page(self):
        with self.assertRaisesRegex(buffer.OpusPacketError, "primeira"):
            source(page([head(), tags()], flags=2)).read()

    def test_tags_length_and_comments_are_bounded(self):
        for invalid in (b"not-tags", b"OpusTags" + struct.pack("<II", 100, 0),
                        b"OpusTags" + struct.pack("<II", 0, 1025),
                        b"OpusTags" + struct.pack("<III", 0, 1, 100)):
            raw = page([head()], flags=2) + page([invalid], sequence=1)
            with self.subTest(invalid=invalid), self.assertRaises(buffer.OpusPacketError):
                source(raw).read()

    def test_oversized_packet_is_rejected_with_bounded_accumulator(self):
        prefix = page([head()], flags=2) + page([tags()], sequence=1)
        huge = (page(sequence=2, lacing=bytes([255]) * 255, body=b"x" * (255 * 255))
                + page(sequence=3, flags=1, lacing=bytes([255]) * 3, body=b"x" * 765))
        decoded = source(prefix + huge)
        with self.assertRaisesRegex(buffer.OpusPacketError, "limite"):
            decoded.read()
        self.assertLessEqual(len(decoded.reader._partial), 65536)

    def test_chained_stream_is_not_forwarded(self):
        decoded = source(fixture() + fixture())
        decoded.read()
        with self.assertRaisesRegex(buffer.OpusPacketError, "EOS"):
            decoded.read()
        self.assertIsNone(decoded.last_opus_packet)

    def test_incomplete_lacing_and_missing_eos_reject(self):
        prefix = page([head()], flags=2) + page([tags()], sequence=1)
        for tail in (page(sequence=2, flags=4, lacing=b"\xff", body=b"x" * 255),
                     page([b"\xfcab"], sequence=2)):
            decoded = source(prefix + tail)
            with self.assertRaises(buffer.OpusPacketError):
                decoded.read()
                decoded.read()

    def test_valid_continuation_reassembles_one_packet(self):
        packet = b"\xfc" + b"x" * 299
        raw = (page([head()], flags=2) + page([tags()], sequence=1)
               + page(sequence=2, lacing=b"\xff", body=packet[:255])
               + page(sequence=3, flags=5, granule=960, lacing=bytes([45]), body=packet[255:]))
        decoded = source(raw)
        decoded.read()
        self.assertEqual(decoded.last_opus_packet, packet)

    def test_duration_10_ms_reframes_without_copy_then_stays_pcm(self):
        decoded = source(fixture([b"\xf4\x01\x02", b"\xf4\x01\x03", b"\xfc\x01\x04"]))
        self.assertEqual(decoded.read(), b"\x02" * 1920 + b"\x03" * 1920)
        self.assertEqual(decoded.passthrough_reason, "packet_duration")
        self.assertIsNone(decoded.last_opus_packet)
        self.assertEqual(decoded.read(), b"\x04" * 3840)
        self.assertIsNone(decoded.last_opus_packet)

    def test_duration_40_ms_reframes_without_loss(self):
        # 2 CELT frames, each 20ms (TOC code 1).
        decoded = source(fixture([b"\xfd\x01\x05"]))
        self.assertEqual(decoded.read(), b"\x05" * 3840)
        self.assertEqual(decoded.read(), b"\x05" * 3840)
        self.assertEqual(decoded.read(), b"")
        self.assertEqual(decoded.passthrough_reason, "packet_duration")

    def test_eos_trim_is_checked_before_first_packet_from_final_page(self):
        packets = [b"\xfcab", b"\xfccd", b"\xfcef"]
        for invalid_granule in (400, 2881, 0x7FFFFFFFFFFFFFFF):
            decoder = mock.Mock()
            decoded = source(fixture(packets, end_granule=invalid_granule), decoder_factory=lambda: decoder)
            with self.subTest(granule=invalid_granule), self.assertRaises(buffer.OpusPacketError):
                decoded.read()
            decoder.decode.assert_not_called()
            self.assertIsNone(decoded.last_opus_packet)

    def test_empty_eos_page_cannot_bypass_tail_validation(self):
        raw = (page([head()], flags=2) + page([tags()], sequence=1)
               + page([b"\xfcab"], sequence=2, granule=960)
               + page([], sequence=3, flags=4, granule=400))
        decoded = source(raw)
        self.assertEqual(len(decoded.read()), 3840)
        with self.assertRaisesRegex(buffer.OpusPacketError, "EOS"):
            decoded.read()

    def test_normal_20ms_tail_padding_is_reported(self):
        decoded = source(fixture([b"\xfcab", b"\xfccd"], end_granule=1608))
        decoded.read()
        self.assertEqual(decoded.opus_metadata["raw_duration_samples"], 1920)
        self.assertEqual(decoded.opus_metadata["tail_padding_samples"], 312)
        decoded.read()
        self.assertEqual(decoded.read(), b"")

    def test_non20ms_valid_tail_padding_can_fill_the_last_long_packet(self):
        decoded = source(fixture([b"\xfdab"], end_granule=400))
        self.assertEqual(len(decoded.read()), 3840)
        self.assertEqual(len(decoded.read()), 3840)
        self.assertEqual(decoded.opus_metadata["tail_padding_samples"], 1520)
        self.assertIsNone(decoded.last_opus_packet)

    def test_copy_size_limit_keeps_valid_pcm(self):
        decoded = source(fixture([b"\xfc" + b"x" * 1275]))
        self.assertEqual(len(decoded.read()), 3840)
        self.assertIsNone(decoded.last_opus_packet)
        self.assertEqual(decoded.passthrough_reason, "packet_size")

    def test_zero_frames_and_over_120ms_are_rejected(self):
        for packet in (b"", b"\xff", b"\xff\x00", b"\xff\x07"):
            with self.subTest(packet=packet), self.assertRaises(buffer.OpusPacketError):
                source(fixture([packet])).read()

    def test_decoder_must_confirm_duration_before_copy(self):
        decoder = SimpleNamespace(decode=lambda *args, **kwargs: b"bad-pcm")
        decoded = source(fixture(), decoder_factory=lambda: decoder)
        with self.assertRaisesRegex(buffer.OpusPacketError, "decoder"):
            decoded.read()
        self.assertIsNone(decoded.last_opus_packet)

    def test_native_decoder_exception_never_exposes_packet(self):
        decoder = mock.Mock()
        decoder.decode.side_effect = ValueError("malformed opus")
        decoded = source(fixture(), decoder_factory=lambda: decoder)
        with self.assertRaises(ValueError):
            decoded.read()
        self.assertIsNone(decoded.last_opus_packet)

    def test_cleanup_is_idempotent(self):
        callback = mock.Mock()
        decoded = buffer.OpusPCMDecoderSource(buffer.OggOpusPacketReader(io.BytesIO(fixture())),
                                             decoder_factory=FakeDecoder, cleanup=callback)
        decoded.cleanup()
        decoded.cleanup()
        callback.assert_called_once()
        self.assertEqual(decoded.read(), b"")

    def test_ffmpeg_argv_maps_first_audio_and_never_uses_shell(self):
        process = mock.Mock(stdout=io.BytesIO(fixture()))
        process.poll.return_value = None
        url = "https://example.invalid/audio $(do-not-run)"
        with mock.patch.object(buffer.subprocess, "Popen", return_value=process) as spawn:
            decoded = buffer.OpusPCMDecoderSource.from_ffmpeg(
                url, before_options="-reconnect 1 -user_agent 'phone worker'", decoder_factory=FakeDecoder)
        argv = spawn.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[argv.index("-map") + 1], "0:a:0")
        self.assertEqual(argv[argv.index("-i") + 1], url)
        self.assertEqual(argv[argv.index("-user_agent") + 1], "phone worker")
        self.assertEqual(argv[argv.index("-c:a") + 1], "copy")
        self.assertNotIn("shell", spawn.call_args.kwargs)
        decoded.cleanup()
        decoded.cleanup()
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=0.2)
        self.assertTrue(process.stdout.closed)

    def test_ffmpeg_argv_maps_confirmed_attachment_stream(self):
        process = mock.Mock(stdout=io.BytesIO(fixture()))
        process.poll.return_value = 0
        with mock.patch.object(buffer.subprocess, "Popen", return_value=process) as spawn:
            decoded = buffer.OpusPCMDecoderSource.from_ffmpeg("attachment.ogg", audio_stream_index=2,
                                                            decoder_factory=FakeDecoder)
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[argv.index("-map") + 1], "0:2")
        decoded.cleanup()
        process.kill.assert_not_called()

    def test_ffmpeg_constructor_error_kills_child(self):
        process = mock.Mock(stdout=io.BytesIO(fixture()))
        process.poll.return_value = None
        def bad_decoder(): raise RuntimeError("decoder unavailable")
        with mock.patch.object(buffer.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "decoder unavailable"):
                buffer.OpusPCMDecoderSource.from_ffmpeg("attachment.ogg", decoder_factory=bad_decoder)
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(process.stdout.closed)


class OpusBufferTests(unittest.TestCase):
    def test_pcm_packet_pairing_is_atomic_after_read_ahead(self):
        packets = [bytes([252, 1, n]) for n in range(1, 8)]
        upstream = source(fixture(packets))
        buffered = buffer.BufferedOpusPCMSource(upstream, max_frames=3)
        self.addCleanup(buffered.cleanup)
        for n, packet in enumerate(packets, 1):
            asyncio.run(buffered.wait_ready(timeout=1))
            self.assertEqual(buffered.read(), bytes([n]) * 3840)
            self.assertEqual(buffered.last_opus_packet, packet)
        self.assertLessEqual(buffered._peak_frames, 3)

    def test_stall_drops_old_packet_and_does_not_fake_music(self):
        release = threading.Event()
        class Slow:
            last_opus_packet = b"packet"
            passthrough_reason = ""
            opus_metadata = {}
            def read(self):
                release.wait(1)
                return b""
            def cleanup(self): release.set()
        buffered = buffer.BufferedOpusPCMSource(Slow(), max_frames=1)
        self.addCleanup(buffered.cleanup)
        buffered.last_opus_packet = b"previous"
        self.assertEqual(buffered.read(), buffer.SILENCE)
        self.assertIsNone(buffered.last_opus_packet)
        self.assertFalse(buffered.last_read_had_audio)

    def test_ordinary_pcm_hook_keeps_original_padding_semantics(self):
        pcm = SimpleNamespace(read=mock.Mock(side_effect=[b"abc", b""]), cleanup=lambda: None)
        buffered = buffer.BufferedPCMSource(pcm)
        self.addCleanup(buffered.cleanup)
        asyncio.run(buffered.wait_ready(timeout=1))
        self.assertEqual(buffered.read(), b"abc".ljust(3840, b"\0"))


class NativeDecoder:
    def __init__(self):
        self.lib = ctypes.CDLL(ctypes.util.find_library("opus"))
        self.lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_decoder_create.restype = ctypes.c_void_p
        self.lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_decoder_destroy.restype = None
        self.lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
                                         ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        self.lib.opus_decode.restype = ctypes.c_int
        error = ctypes.c_int()
        self.state = self.lib.opus_decoder_create(48000, 2, ctypes.byref(error))
        if not self.state or error.value:
            raise RuntimeError("native Opus decoder unavailable")

    def decode(self, packet, *, fec=False):
        raw = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        pcm = (ctypes.c_int16 * (5760 * 2))()
        count = self.lib.opus_decode(self.state, raw, len(packet), pcm, 5760, int(fec))
        if count < 0:
            raise ValueError("native Opus rejected payload")
        return bytes(pcm)[:count * 4]

    def __del__(self):
        if getattr(self, "state", None):
            self.lib.opus_decoder_destroy(self.state)
            self.state = None


@unittest.skipUnless(shutil.which("ffmpeg") and ctypes.util.find_library("opus"), "FFmpeg and native libopus required")
class NativePacketTests(unittest.TestCase):
    def make_file(self, directory, duration_ms):
        target = Path(directory) / f"tone-{duration_ms}.ogg"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.12",
                        "-ac", "2", "-c:a", "libopus", "-b:a", "128k", "-frame_duration", str(duration_ms), str(target)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5)
        return target

    def test_native_20_ms_packet_copy_is_bit_identical_through_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self.make_file(directory, 20)
            reader = buffer.OggOpusPacketReader(io.BytesIO(target.read_bytes()))
            expected = []
            while packet := reader.read_packet():
                expected.append(packet)
            decoded = buffer.OpusPCMDecoderSource.from_ffmpeg(str(target), decoder_factory=NativeDecoder)
            self.addCleanup(decoded.cleanup)
            observed = []
            while frame := decoded.read():
                self.assertEqual(len(frame), 3840)
                self.assertTrue(any(frame))
                observed.append(decoded.last_opus_packet)
            self.assertEqual(observed, expected)
            self.assertGreater(len(observed), 1)
            self.assertEqual(decoded.opus_metadata["pre_skip_samples"], 312)

    def test_native_10_and_40_ms_are_decoded_and_reframed_without_copy(self):
        for duration_ms in (10, 40):
            with self.subTest(duration_ms=duration_ms), tempfile.TemporaryDirectory() as directory:
                target = self.make_file(directory, duration_ms)
                decoded = buffer.OpusPCMDecoderSource.from_ffmpeg(str(target), decoder_factory=NativeDecoder)
                self.addCleanup(decoded.cleanup)
                frames = []
                while frame := decoded.read():
                    frames.append(frame)
                    self.assertEqual(len(frame), 3840)
                    self.assertIsNone(decoded.last_opus_packet)
                self.assertGreater(len(frames), 3)
                self.assertEqual(decoded.passthrough_reason, "packet_duration")

    def test_ffmpeg_cleanup_reaps_process_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self.make_file(directory, 20)
            decoded = buffer.OpusPCMDecoderSource.from_ffmpeg(str(target), decoder_factory=NativeDecoder)
            self.assertEqual(len(decoded.read()), 3840)
            decoded.cleanup()
            decoded.cleanup()
            self.assertEqual(decoded.read(), b"")


if __name__ == "__main__":
    unittest.main()
