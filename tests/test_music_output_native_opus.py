"""Offline integration of the production output update with real libopus.

Run with ``python -m unittest discover -s tests -p test_music_output_native_opus.py -v``.
Only the Discord AudioSource interface is adapted by the existing boundary
loader. Actual encoder CTLs, packet encoding and decoding use the installed
libopus. These checks prove codec compatibility, not audible quality or phone
CPU performance; listening and network-loss tests still require Discord.
"""
from __future__ import annotations

from array import array
import asyncio
import ctypes
import ctypes.util
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 48000
FRAME_SAMPLES = 960
CHANNELS = 2


def _production_boundaries():
    path = ROOT / "cogs/musica/testes/runtime_telefone/test_output_bitrate_sync.py"
    spec = importlib.util.spec_from_file_location("native_opus_production_boundaries", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_production_boundaries()


class NativeOpus:
    """Small libopus boundary matching the discord.py setters used by production."""

    def __init__(self, library: str):
        self.lib = ctypes.CDLL(library)
        self.lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int,
                                                 ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_encoder_destroy.restype = None
        self.lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_decoder_create.restype = ctypes.c_void_p
        self.lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_decoder_destroy.restype = None
        self.lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
        self.lib.opus_encode.restype = ctypes.c_int
        self.lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
                                         ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        self.lib.opus_decode.restype = ctypes.c_int
        self.lib.opus_packet_get_nb_samples.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
                                                        ctypes.c_int32]
        self.lib.opus_packet_get_nb_samples.restype = ctypes.c_int
        self.lib.opus_get_version_string.argtypes = []
        self.lib.opus_get_version_string.restype = ctypes.c_char_p

        # opus_encoder_ctl is variadic. Give its two used forms separate,
        # explicit signatures so neither a pointer nor the encoder address is
        # implicitly converted to a C int (which truncates pointers on 64 bit).
        address = ctypes.cast(self.lib.opus_encoder_ctl, ctypes.c_void_p).value
        self._set_ctl = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)(address)
        self._get_ctl = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int))(address)
        self.encoder = None
        self.decoder = None
        self.calls = []
        error = ctypes.c_int()
        # OPUS_APPLICATION_AUDIO; never OPUS_RESET_STATE during live updates.
        self.encoder = self.lib.opus_encoder_create(SAMPLE_RATE, CHANNELS, 2049, ctypes.byref(error))
        self._check(error.value)
        if not self.encoder:
            raise RuntimeError("libopus returned a null encoder")
        try:
            self.decoder = self.lib.opus_decoder_create(SAMPLE_RATE, CHANNELS, ctypes.byref(error))
            self._check(error.value)
            if not self.decoder:
                raise RuntimeError("libopus returned a null decoder")
        except Exception:
            self.close()
            raise

    @property
    def version(self):
        return self.lib.opus_get_version_string().decode("ascii")

    @staticmethod
    def _check(code):
        if code < 0:
            raise RuntimeError(f"libopus error {code}")
        return code

    def close(self):
        if self.decoder:
            self.lib.opus_decoder_destroy(self.decoder)
            self.decoder = None
        if self.encoder:
            self.lib.opus_encoder_destroy(self.encoder)
            self.encoder = None

    def _set(self, request, value):
        self._check(self._set_ctl(self.encoder, request, value))

    def get(self, request):
        result = ctypes.c_int()
        self._check(self._get_ctl(self.encoder, request, ctypes.byref(result)))
        return result.value

    def set_bitrate(self, value):
        self._set(4002, int(value) * 1000)
        self.calls.append(("bitrate", value))
        return self.get(4003) // 1000

    def set_fec(self, value):
        self._set(4012, int(bool(value)))
        self.calls.append(("fec", value))

    def set_expected_packet_loss_percent(self, value):
        # discord.py accepts a 0..1 fraction and passes integer percent to Opus.
        self._set(4014, int(float(value) * 100))
        self.calls.append(("loss", value))

    def set_signal_type(self, value):
        self._set(4024, {"music": 3002}[value])
        self.calls.append(("signal", value))

    def set_bandwidth(self, value):
        self._set(4008, {"full": 1105}[value])
        self.calls.append(("bandwidth", value))

    def encode_decode(self, pcm):
        if len(pcm) != FRAME_SAMPLES * CHANNELS * ctypes.sizeof(ctypes.c_int16):
            raise ValueError("expected exactly 20 ms of 48 kHz stereo PCM")
        samples = (ctypes.c_int16 * (FRAME_SAMPLES * CHANNELS)).from_buffer_copy(pcm)
        packet = (ctypes.c_ubyte * 4000)()
        size = self._check(self.lib.opus_encode(self.encoder, samples, FRAME_SAMPLES, packet, len(packet)))
        packet_samples = self._check(self.lib.opus_packet_get_nb_samples(packet, size, SAMPLE_RATE))
        decoded = (ctypes.c_int16 * (FRAME_SAMPLES * CHANNELS))()
        decoded_samples = self._check(self.lib.opus_decode(self.decoder, packet, size, decoded, FRAME_SAMPLES, 0))
        return size, packet_samples, decoded_samples, tuple(decoded)


class TonePCM:
    """Deterministic stereo fixture; never used as evidence of perceived quality."""

    def __init__(self):
        self.offset = 0
        self.last_frame = b""
        self.closed = False

    def read(self):
        samples = array("h")
        for index in range(self.offset, self.offset + FRAME_SAMPLES):
            samples.extend((round(8000 * math.sin(2 * math.pi * 440 * index / SAMPLE_RATE)),
                            round(6000 * math.sin(2 * math.pi * 660 * index / SAMPLE_RATE))))
        self.offset += FRAME_SAMPLES
        self.last_frame = samples.tobytes()
        return self.last_frame

    def cleanup(self):
        self.closed = True


class NativeOutputOpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = ctypes.util.find_library("opus")
        if library is None:
            raise unittest.SkipTest("native Opus checks require libopus")
        cls.library = library
        cls.Agent, cls.mixer = _production_boundaries()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.codec = NativeOpus(self.library)
        self.addCleanup(self.codec.close)
        self.agent = self.Agent()
        self.agent.log = lambda *args, **kwargs: None
        self.pcm = TonePCM()
        self.source = self.mixer.AgentMixedAudioSource(
            loop=self.loop, music_source=self.pcm, music_volume=1,
            persistent=True, telemetry_enabled=False)
        self.source.quality_context = {}
        self.addCleanup(self.source.cleanup)
        self.channel = SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1))
        self.voice = SimpleNamespace(source=self.source, channel=self.channel, encoder=self.codec)
        self.track = SimpleNamespace(audio_abr=160)
        self.state = SimpleNamespace(
            current=self.track, player=self.voice, transport="direct", voice_channel_id=20,
            playback_token=9, updated_at=0, audio_output_applied={})
        self.agent.states = {1: self.state}
        self.requested = {
            "bitrate_kbps": 64, "channel_bitrate_kbps": 256, "profile": "quality_256",
            "fec": True, "expected_packet_loss": 0.15, "application": "audio",
            "bandwidth": "full", "signal_type": "music", "revision": 0}
        self.agent._audio_output_policy = lambda *args: dict(self.requested)

    def _queue(self, bitrate, *, loss=0.15):
        self.channel.bitrate = bitrate * 1000
        self.requested.update(bitrate_kbps=bitrate, channel_bitrate_kbps=bitrate,
                              expected_packet_loss=loss, revision=self.requested["revision"] + 1)
        return self.loop.run_until_complete(self.agent._sync_audio_output(1, expected_channel_id=20))

    def _frame_and_packet(self):
        frame = self.source.read()
        self.assertEqual(frame, self.pcm.last_frame, "output settings must preserve mixer PCM bytes")
        size, packet_samples, decoded_samples, decoded = self.codec.encode_decode(frame)
        self.assertGreater(size, 0)
        self.assertEqual(packet_samples, FRAME_SAMPLES)
        self.assertEqual(decoded_samples, FRAME_SAMPLES)
        self.assertEqual(len(decoded), FRAME_SAMPLES * CHANNELS)
        self.assertTrue(any(decoded), "nonzero input must produce nonzero decoded PCM")
        self.assertFalse(self.pcm.closed)

    def test_native_live_bitrate_updates_preserve_fec_and_decode(self):
        encoder_identity = self.codec.encoder
        decoder_identity = self.codec.decoder
        self.assertTrue(self.codec.version.startswith("libopus"))
        for bitrate in (64, 96, 192, 256):
            with self.subTest(bitrate_kbps=bitrate):
                old_bitrate = self.codec.get(4003)
                calls_before = len(self.codec.calls)
                self.assertEqual(self._queue(bitrate)["state"], "queued")
                self.assertEqual(self.codec.get(4003), old_bitrate, "controls run at the next PCM read")
                self.assertEqual(len(self.codec.calls), calls_before)
                for _ in range(4):
                    self._frame_and_packet()
                self.assertEqual(self.codec.get(4003), bitrate * 1000)
                self.assertEqual(self.codec.get(4013), 1)  # OPUS_GET_INBAND_FEC
                self.assertEqual(self.codec.get(4015), 15)  # OPUS_GET_PACKET_LOSS_PERC
                self.assertEqual(self.codec.get(4025), 3002)  # OPUS_GET_SIGNAL / MUSIC
                self.assertEqual(self.codec.get(4009), 1105)  # OPUS_GET_BANDWIDTH / FULLBAND
                self.assertEqual(self.codec.get(4001), 2049)  # OPUS_GET_APPLICATION / AUDIO
                self.assertEqual(self.state.audio_output_applied["state"], "applied")
                self.assertEqual(self.state.audio_output_applied["bitrate_kbps"], bitrate)
                self.assertEqual(self.codec.encoder, encoder_identity)
                self.assertEqual(self.codec.decoder, decoder_identity)
        self.assertEqual([value for name, value in self.codec.calls if name == "bitrate"], [64, 96, 192, 256])

    def test_explicit_loss_change_is_applied_without_replacing_encoder(self):
        self._queue(192)
        self._frame_and_packet()
        encoder_identity = self.codec.encoder
        self.assertEqual(self.codec.get(4015), 15)
        self.assertEqual(self._queue(192, loss=0.05)["state"], "queued")
        self.assertEqual(self.codec.get(4015), 15)
        for _ in range(4):
            self._frame_and_packet()
        self.assertEqual(self.codec.get(4015), 5)
        self.assertEqual(self.codec.get(4013), 1)
        self.assertEqual(self.codec.get(4003), 192000)
        self.assertEqual(self.codec.encoder, encoder_identity)
        self.assertEqual(self.state.audio_output_applied["expected_packet_loss"], 0.05)
        self.assertEqual(self.source.quality_context["opus_expected_packet_loss"], 0.05)


if __name__ == "__main__":
    unittest.main()
