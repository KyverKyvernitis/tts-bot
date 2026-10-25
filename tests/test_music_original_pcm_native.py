"""Offline AAC -> prepared Opus -> original PCM handoff with native codecs.

Production preparation routing, bounded buffers and both mixers execute here.
Only Discord's FFmpeg/voice bindings are adapted to local subprocesses and
libopus. This does not measure phone/network latency or perceptual continuity.
"""
from __future__ import annotations

import ast
import asyncio
import ctypes.util
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from tests.test_music_original_pcm_routing import load_routing, SOURCE
from tests.test_music_opus_native_pipeline import load_mixer
from tests.test_music_opus_processed_native import CaptureNativeEncoder
from tests.test_music_opus_packets import NativeDecoder, buffer


class LocalFFmpegPCM:
    """Discord FFmpeg boundary backed by the real installed decoder."""
    def __init__(self, source, *, executable, before_options, options):
        self.media = source
        self.before_options = before_options
        self.frames = []
        self.closed = False
        self.process = subprocess.Popen(
            [executable, '-v', 'error', *shlex.split(before_options), '-i', source,
             *shlex.split(options), '-f', 's16le', 'pipe:1'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self):
        frame = self.process.stdout.read(3840)
        if frame:
            self.frames.append(frame)
        return frame

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.process.stdout.close()


class FrameSource:
    def __init__(self, frame):
        self.frame = frame

    def read(self):
        return self.frame

    def cleanup(self):
        pass


async def owned_cleanup(source):
    # Explicit worker ownership avoids executor wakeup restrictions in the
    # test sandbox. Production cleanup uses a shielded executor with the same
    # cancellation/resource rule; its ownership races have separate tests.
    errors = []
    def run():
        try:
            source.cleanup()
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(.005)
    thread.join()
    if errors:
        raise errors[0]


@unittest.skipUnless(shutil.which('ffmpeg') and ctypes.util.find_library('opus'),
                     'FFmpeg and native libopus required')
class NativeOriginalPCMRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_aac_handoff_decodes_original_and_encodes_mixed_pcm_once(self):
        with tempfile.TemporaryDirectory(prefix='original-pcm-native-') as folder:
            media = Path(folder) / 'original.m4a'
            subprocess.run([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=430:sample_rate=48000:duration=3',
                '-ac', '2', '-c:a', 'aac', '-b:a', '128k', str(media)],
                check=True, capture_output=True, timeout=10)

            Agent = load_routing()
            # Execute the actual PCM creation method. Only its Discord FFmpeg
            # constructor is adapted; filters, seek and production buffer stay.
            tree = ast.parse(SOURCE.read_text())
            klass = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                         and node.name == 'PreparacaoAudioMixin')
            create = next(node for node in klass.body if isinstance(node, ast.FunctionDef)
                          and node.name == '_create_pcm_source')
            namespace = Agent._original_pcm_handoff.__globals__
            namespace.update(discord=SimpleNamespace(FFmpegPCMAudio=LocalFFmpegPCM),
                             BufferedPCMSource=buffer.BufferedPCMSource)
            module = ast.Module(body=[ast.ImportFrom(module='__future__',
                names=[ast.alias(name='annotations')], level=0), create], type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), namespace)
            Agent._create_pcm_source = namespace['_create_pcm_source']

            agent, loop = Agent(), asyncio.get_running_loop()
            agent._loop = loop
            agent.log = mock.Mock()
            agent._audio_prepare_stopping = False
            agent._original_pcm_handoff_tasks = {}
            agent._claim_audio_prepare_slot = mock.Mock(return_value=True)
            agent._release_audio_prepare_slot = mock.Mock()
            agent._track_stream_needs_refresh = mock.Mock(return_value=False)
            agent._arm_music_packet_trace = mock.Mock()
            agent._cleanup_audio_off_loop = owned_cleanup
            agent.ffmpeg_executable = 'ffmpeg'
            agent.pcm_buffer_max_frames = 25
            agent._ffmpeg_options_for_source = lambda *args, **kwargs: ('-vn -ac 2 -ar 48000', '')
            agent._ffmpeg_before_options_for_offset = lambda offset: f'-ss {offset:.3f}'
            track = AgentTrack(stream_url=str(media), audio_codec='aac',
                               audio_channels=2, audio_sample_rate=48000,
                               audio_abr=128, duration=3)
            raw = buffer.OpusPCMDecoderSource.from_prepared_ffmpeg(
                str(media), bitrate_kbps=96, source_codec='aac', decoder_factory=NativeDecoder)
            prepared = buffer.BufferedOpusPCMSource(raw, max_frames=25)
            prepared._prepared_opus_source = True
            agent._install_original_pcm_request(1, track, prepared)
            encoder = CaptureNativeEncoder()
            mixer_module = load_mixer()
            mixer = mixer_module.AgentHybridOpusAudioSource(
                loop=loop, music_source=prepared, music_volume=1,
                persistent=True, telemetry_enabled=False, output_encoder=encoder,
                output_policy={'bitrate_kbps': 96, 'copy_bitrate_kbps': 256,
                               'channel_bitrate_kbps': 256, 'opus_direct': 'auto',
                               'fec': True, 'expected_packet_loss': .15})
            player = SimpleNamespace(source=mixer)
            state = GuildMusicState(1, current=track, player=player, transport='direct',
                                    status='playing', playback_token=9,
                                    volume_percent=100, normal_volume_percent=100)
            agent.states = {1: state}
            reference = None
            try:
                await prepared.wait_ready(timeout=2, min_frames=5)
                for _ in range(5):
                    self.assertEqual(buffer.opus_packet_samples(mixer.read()), 960)
                self.assertEqual(encoder.frames, [], 'unchanged prepared packets are copied')
                position = prepared.original_pcm_position_seconds()
                self.assertAlmostEqual(position, (5 * 960 - 312) / 48000)
                encoder_identity, token = encoder.encoder, state.playback_token
                mixer.set_music_volume(.55)  # actual production processing hook
                state.volume_percent = state.normal_volume_percent = 55

                deadline = loop.time() + 3
                while mixer._pending_music_handoff is None:
                    self.assertLess(loop.time(), deadline, 'original PCM was not queued')
                    await asyncio.sleep(.005)
                task = agent._original_pcm_handoff_tasks[1]
                candidate = mixer._pending_music_handoff[1]
                self.assertIsInstance(candidate, buffer.BufferedPCMSource)
                original = candidate.source
                self.assertIsInstance(original, LocalFFmpegPCM)
                self.assertEqual(original.media, str(media), 'decoder must reopen original AAC')
                offset = float(shlex.split(original.before_options)[1])
                self.assertAlmostEqual(offset, position, delta=.001)
                self.assertIs(mixer.music_source, prepared, 'no source replacement before read boundary')

                packet = mixer.read()
                self.assertEqual(buffer.opus_packet_samples(packet), 960)
                self.assertEqual(len(encoder.frames), 1)
                self.assertIs(mixer.music_source, candidate)
                self.assertEqual(mixer.opus_direct_status()['pcm_origin'], 'original')
                self.assertTrue(mixer.opus_direct_status()['processed_active'])
                self.assertTrue(any(encoder.frames[0]), 'original input must contain decoded music')

                # The native encoder receives exactly the existing mixer's
                # volume transition over the original AAC decoder's first PCM.
                reference = mixer_module.AgentMixedAudioSource(
                    loop=loop, music_source=FrameSource(original.frames[0]), music_volume=1,
                    persistent=True, telemetry_enabled=False)
                reference.set_music_volume(.55)
                self.assertEqual(encoder.frames[0], reference.read())
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
                self.assertTrue(raw._closed)
                self.assertTrue(prepared._closed)
                self.assertFalse(candidate._closed)
                self.assertEqual(encoder.encoder, encoder_identity)
                self.assertEqual(state.playback_token, token)
                self.assertIs(state.player, player)
                self.assertIs(player.source, mixer)
                self.assertIs(state.current, track)
                agent._release_audio_prepare_slot.assert_called_once()
                self.assertEqual(agent.log.call_args.kwargs['outcome'], 'success')
                for _ in range(3):
                    self.assertEqual(buffer.opus_packet_samples(mixer.read()), 960)
                self.assertEqual(len(encoder.frames), 4, 'one native encode for each processed frame')
            finally:
                tasks = list(agent._original_pcm_handoff_tasks.values())
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await owned_cleanup(mixer)
                await owned_cleanup(prepared)
                if reference is not None:
                    reference.cleanup()
                encoder.close()


if __name__ == '__main__':
    unittest.main()
