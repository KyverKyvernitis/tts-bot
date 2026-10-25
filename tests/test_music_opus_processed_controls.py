"""Production volume/effect commands with the real mixer and explicit I/O boundaries.

The command AST and real DSP execute here. Async source preparation, Discord
and the ordinary encoder double are boundaries; a separate optional native
case verifies actual Opus packets. This is not a live/audible Discord test.
"""
from __future__ import annotations

import ast
import asyncio
from array import array
import contextlib
import ctypes.util
from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest import mock

from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from tests.test_music_opus_hybrid import Encoder, load_mixer
from tests.test_music_opus_native_pipeline import NativeOutputEncoder
from tests.test_music_opus_packets import NativeDecoder


ROOT = Path(__file__).resolve().parents[1]


class ReadyPCM:
    def __init__(self, *, value=4000, gate=None):
        self.frame = array('h', [value, -value] * 960).tobytes()
        self.last_read_had_audio = False
        self.closed = False
        self.reads = 0
        self.gate = gate
        self.entered = asyncio.Event()
        self.ready_error = None

    async def wait_ready(self, **kwargs):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.ready_error is not None:
            raise self.ready_error

    def read(self):
        self.reads += 1
        self.last_read_had_audio = not self.closed
        return self.frame if not self.closed else b''

    def cleanup(self):
        self.closed = True


def load_controls(mixer):
    path = ROOT / 'cogs/musica/runtime_telefone/agente/reproducao.py'
    original = next(node for node in ast.parse(path.read_text()).body
                    if isinstance(node, ast.ClassDef) and node.name == 'ReproducaoMixin')
    wanted = {'_apply_player_volume', 'cmd_volume', 'cmd_duck', 'cmd_unduck',
              'cmd_audio_effect', '_build_ffmpeg_source', '_play_music_source',
              '_record_first_music_packet'}
    methods = [node for node in original.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    assert {node.name for node in methods} == wanted
    klass = ast.ClassDef(name='Agent', bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    namespace = dict(asyncio=asyncio, contextlib=contextlib, math=math, time=time, replace=replace,
                     GuildMusicState=GuildMusicState, AgentTrack=AgentTrack,
                     AgentMixedAudioSource=mixer.AgentMixedAudioSource,
                     AgentHybridOpusAudioSource=mixer.AgentHybridOpusAudioSource,
                     BufferedPCMSource=ReadyPCM, ArchiveSegmentedPCMSource=ReadyPCM,
                     ContinuousArchiveDSPSource=ReadyPCM, PCM_FRAME_BYTES=3840,
                     safe_id=lambda value: int(value or 0), short_text=lambda value, n: str(value)[:n])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, klass], type_ignores=[])),
                 str(path), 'exec'), namespace)
    return namespace['Agent']


class ProcessedOpusControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mixer_module = load_mixer()
        self.Agent = load_controls(self.mixer_module)
        self.agent = self.Agent()
        self.loop = asyncio.get_running_loop()
        self.agent._loop = self.loop
        self.agent.default_volume_percent = 55
        self.agent.duck_volume_percent = 8
        self.agent.direct_pcm_volume_enabled = True
        self.agent.ffmpeg_options = '-vn -sn -dn'
        self.agent.prepare_timeout = 30
        self.agent.pcm_buffer_stall_seconds = 12
        self.agent.audio_telemetry_enabled = False
        self.agent._ffmpeg_has_custom_audio_filter = lambda options: False
        self.agent._ffmpeg_before_options_for_offset = lambda offset: ''
        self.agent._ffmpeg_options_for_source = lambda *args, **kwargs: ('', 'native_48k')
        self.agent._track_stream_needs_refresh = lambda track: False
        self.agent._cancel_audio_preparation = mock.Mock()
        self.agent._schedule_audio_prepare = mock.Mock()
        self.agent._schedule_next_queue_prefetch = mock.Mock()
        self.agent._arm_music_packet_trace = mock.Mock()
        self.agent.log = mock.Mock()
        self.agent._set_status = lambda st, status, **kwargs: setattr(st, 'status', status)
        self.agent._direct_after = mock.AsyncMock()
        self.track = AgentTrack(title='original', stream_url='https://media.invalid/not-fetched',
                                audio_codec='aac', audio_abr=128, duration=180)
        self.queue_track = AgentTrack(title='queue remains')
        self.pcm = ReadyPCM()
        self.encoder = Encoder()
        self.policy = dict(bitrate_kbps=160, channel_bitrate_kbps=256, copy_bitrate_kbps=256,
                           opus_direct='auto', fec=True, expected_packet_loss=.15)
        self.source = self.mixer_module.AgentHybridOpusAudioSource(
            loop=self.loop, music_source=self.pcm, music_volume=.55, persistent=True,
            on_music_end=lambda *args: None, output_policy=self.policy,
            output_encoder=self.encoder, telemetry_enabled=False)
        self.voice = SimpleNamespace(source=self.source, encoder=self.encoder,
                                     channel=SimpleNamespace(id=19), is_playing=lambda: True,
                                     is_paused=lambda: False, is_connected=lambda: True,
                                     stop=mock.Mock(), play=mock.Mock())
        self.state = GuildMusicState(1, current=self.track, player=self.voice,
                                     voice_channel_id=19, transport='direct', status='playing',
                                     playback_token=7, volume_percent=55, normal_volume_percent=55)
        self.state.queue.append(self.queue_track)
        self.state.source_position_seconds = lambda: 12.5
        self.agent.states = {1: self.state}
        def bump(st, **kwargs):
            st.playback_token += 1
            return st.playback_token
        self.agent._bump_playback_generation = bump
        self.candidate = ReadyPCM(value=2000)
        self.agent._create_pcm_source = mock.Mock(return_value=self.candidate)

    async def asyncTearDown(self):
        self.source.cleanup()
        await asyncio.sleep(0)

    async def test_volume_zero_55_and_150_apply_before_one_encode_without_restarting(self):
        identities = self.voice, self.source, self.encoder, self.pcm, self.state.playback_token
        for value in (0, 55, 150):
            with self.subTest(value=value):
                result = await self.agent.cmd_volume({'guild_id': 1, 'volume': value})
                self.assertTrue(result['ok'])
                self.assertEqual(result['volume'], value)
                self.assertEqual(self.source.normal_music_volume, value / 100)
                # Keep the existing short gain ramp that avoids clicks. Each
                # output tick still performs exactly one final Opus encode.
                for _ in range(55):
                    before = len(self.encoder.frames)
                    self.source.read()
                    self.assertEqual(len(self.encoder.frames), before + 1)
                actual = array('h', self.encoder.frames[-1])
                if value == 0:
                    self.assertEqual(max(map(abs, actual)), 0)
                elif value == 55:
                    self.assertEqual(actual[0], 2200)
                else:
                    self.assertEqual(actual[0], 6000)
                self.assertEqual(identities, (self.voice, self.source, self.encoder, self.pcm,
                                              self.state.playback_token))
                self.assertTrue(self.source.is_opus())
        self.voice.stop.assert_not_called()
        self.voice.play.assert_not_called()
        self.agent._create_pcm_source.assert_not_called()

    async def test_volume_percent_alias_accepts_zero_and_default_stays_55(self):
        self.assertEqual((await self.agent.cmd_volume({'guild_id': 1, 'volume_percent': 0}))['volume'], 0)
        self.assertEqual((await self.agent.cmd_volume({'guild_id': 1}))['volume'], 55)

    async def test_nonfinite_or_boolean_volume_is_rejected_without_mutating_audio(self):
        before = self.state.volume_percent, self.state.normal_volume_percent, self.source.normal_music_volume
        for invalid in (True, False, float('nan'), float('inf'), 'invalid'):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, TypeError)):
                await self.agent.cmd_volume({'guild_id': 1, 'volume': invalid})
            self.assertEqual(before, (self.state.volume_percent, self.state.normal_volume_percent,
                                      self.source.normal_music_volume))
        self.assertEqual(self.encoder.frames, [])

    async def test_auto_pcm_start_uses_native_opus_transport_and_off_keeps_legacy_pcm(self):
        self.agent.duck_volume_percent = 8
        native_api = SimpleNamespace(AudioSource=object,
                                     opus=SimpleNamespace(Encoder=lambda **kwargs: Encoder()))
        with mock.patch.object(self.mixer_module, 'discord', native_api):
            source = self.agent._build_ffmpeg_source(
                'unused', volume_percent=55, pcm_source=ReadyPCM(),
                output_policy=self.policy, on_music_end=lambda *args: None)
            try:
                self.assertIsInstance(source, self.mixer_module.AgentHybridOpusAudioSource)
                self.assertTrue(source.is_opus())
                self.agent._play_music_source(self.voice, source, after=None, opus_bitrate_kbps=160,
                                               output_policy=self.policy)
                self.assertIs(self.voice.encoder, source.output_encoder)
                self.assertEqual(self.voice.play.call_args.kwargs, {'after': None})
            finally:
                source.cleanup()
            legacy = self.agent._build_ffmpeg_source(
                'unused', volume_percent=55, pcm_source=ReadyPCM(),
                output_policy={**self.policy, 'opus_direct': 'off'}, on_music_end=lambda *args: None)
            try:
                self.assertFalse(legacy.is_opus())
                self.assertNotIsInstance(legacy, self.mixer_module.AgentHybridOpusAudioSource)
            finally:
                legacy.cleanup()

    @unittest.skipUnless(ctypes.util.find_library('opus'), 'native libopus required')
    async def test_volume_controls_emit_valid_native_20ms_opus_without_player_restart(self):
        self.source.cleanup()
        native = NativeOutputEncoder(ctypes.util.find_library('opus'))
        self.addCleanup(native.close)
        self.encoder = native
        self.source = self.mixer_module.AgentHybridOpusAudioSource(
            loop=self.loop, music_source=self.pcm, music_volume=.55, persistent=True,
            on_music_end=lambda *args: None, output_policy=self.policy,
            output_encoder=native, telemetry_enabled=False)
        self.voice.source = self.source
        self.voice.encoder = native
        receiver = NativeDecoder()
        for volume in (55, 0, 150, 100):
            await self.agent.cmd_volume({'guild_id': 1, 'volume': volume})
            before = sum(name == 'encode' for name, _value in native.calls)
            packet = self.source.read()
            self.assertTrue(self.source._opus_packet_is_20ms(packet))
            self.assertEqual(len(receiver.decode(packet, fec=False)), 3840)
            self.assertEqual(sum(name == 'encode' for name, _value in native.calls), before + 1)
            self.assertIs(self.voice.encoder, native)
        self.voice.stop.assert_not_called()
        self.voice.play.assert_not_called()

    async def test_muted_music_stays_muted_after_duck_and_unduck(self):
        await self.agent.cmd_volume({'guild_id': 1, 'volume': 0})
        await self.agent.cmd_duck({'guild_id': 1, 'volume': 8})
        restored = await self.agent.cmd_unduck({'guild_id': 1})
        self.assertEqual(restored['volume'], 0)
        self.assertEqual(restored['normal_volume'], 0)
        for _ in range(15):
            self.source.read()
        self.assertFalse(any(self.encoder.frames[-1]))

    async def test_bassboost_updates_existing_mixer_and_preserves_session(self):
        result = await self.agent.cmd_audio_effect({'guild_id': 1, 'effect': 'bassboost', 'level': 6})
        self.assertTrue(result['ok'])
        self.assertEqual(self.source.bassboost_level, 6)
        self.assertEqual(self.state.effect_signature(), (6, 0, 0))
        self.assertEqual(self.state.playback_token, 7)
        self.assertIs(self.source.music_source, self.pcm)
        self.assertIs(self.source.output_encoder, self.encoder)
        self.agent._create_pcm_source.assert_not_called()
        self.voice.stop.assert_not_called()
        self.source.read()
        self.assertEqual(len(self.encoder.frames[-1]), 3840)

    async def test_nightcore_warms_then_swaps_only_music_source_with_exact_effects(self):
        gate = asyncio.Event()
        self.candidate.gate = gate
        task = asyncio.create_task(self.agent.cmd_audio_effect(
            {'guild_id': 1, 'effect': 'nightcore', 'level': 2}))
        await self.candidate.entered.wait()
        self.assertIs(self.source.music_source, self.pcm)
        self.source.read()
        gate.set()
        result = await task
        self.assertTrue(result['ok'])
        selected = self.agent._create_pcm_source.call_args.args[0]
        self.assertEqual(selected.start_offset_seconds, 12.5)
        self.assertEqual(self.agent._create_pcm_source.call_args.kwargs['effects'], (0, 2, 0))
        self.assertIs(self.source.music_source, self.candidate)
        self.assertIs(self.voice.source, self.source)
        self.assertIs(self.source.output_encoder, self.encoder)
        self.assertTrue(self.pcm.closed)
        self.assertEqual(self.state.queue, [self.queue_track])
        self.assertEqual(self.state.playback_token, 8)
        self.voice.stop.assert_not_called()
        self.voice.play.assert_not_called()
        self.agent._arm_music_packet_trace.assert_called_once()
        trace = self.agent._arm_music_packet_trace.call_args
        self.assertEqual(trace.kwargs['token'], 8)
        self.assertIs(trace.kwargs['pcm_source'], self.candidate)
        self.assertTrue(trace.kwargs['output_refresh'])

    async def test_slower_reverb_replaces_conflicting_nightcore_without_changing_voice(self):
        self.state.apply_effect_signature((1, 2, 0))
        result = await self.agent.cmd_audio_effect({'guild_id': 1, 'effect': 'slowed_reverb', 'level': 3})
        self.assertTrue(result['ok'])
        self.assertEqual(self.agent._create_pcm_source.call_args.kwargs['effects'], (1, 0, 3))
        self.assertEqual(self.state.effect_signature(), (1, 0, 3))
        self.assertIs(self.source.output_encoder, self.encoder)
        self.assertIs(self.voice.source, self.source)

    async def test_effect_replacement_keeps_queue_and_uses_new_token_for_completion(self):
        result = await self.agent.cmd_audio_effect({'guild_id': 1, 'effect': 'nightcore', 'level': 1})
        self.assertTrue(result['ok'])
        self.candidate.cleanup()  # natural source EOF boundary, not player.stop()
        self.source.read()
        for _ in range(10):
            if self.agent._direct_after.await_count:
                break
            await asyncio.sleep(.001)
        self.agent._direct_after.assert_awaited_once()
        args = self.agent._direct_after.await_args.args
        self.assertEqual(args, (1, None, 8))
        self.assertEqual(self.state.queue, [self.queue_track])
        self.voice.stop.assert_not_called()

    async def test_cancelled_effect_candidate_is_closed_and_old_music_keeps_playing(self):
        self.candidate.gate = asyncio.Event()
        task = asyncio.create_task(self.agent.cmd_audio_effect(
            {'guild_id': 1, 'effect': 'nightcore', 'level': 1}))
        await self.candidate.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.candidate.closed)
        self.assertFalse(self.pcm.closed)
        self.assertIs(self.source.music_source, self.pcm)
        self.assertEqual(self.state.playback_token, 7)
        self.assertEqual(self.state.effect_signature(), (0, 0, 0))

    async def test_changed_track_during_effect_preparation_does_not_replace_new_music(self):
        gate = asyncio.Event()
        self.candidate.gate = gate
        task = asyncio.create_task(self.agent.cmd_audio_effect(
            {'guild_id': 1, 'effect': 'nightcore', 'level': 1}))
        await self.candidate.entered.wait()
        newer = AgentTrack(title='newer')
        self.state.current = newer
        self.state.playback_token += 1
        gate.set()
        result = await task
        self.assertFalse(result['ok'])
        self.assertTrue(self.candidate.closed)
        self.assertIs(self.state.current, newer)
        self.assertIs(self.source.music_source, self.pcm)
        self.assertEqual(self.state.queue, [self.queue_track])
        self.agent._arm_music_packet_trace.assert_not_called()

    async def test_failed_effect_does_not_change_session_or_settings(self):
        self.candidate.ready_error = ValueError('decoder failure')
        result = await self.agent.cmd_audio_effect({'guild_id': 1, 'effect': 'nightcore', 'level': 1})
        self.assertFalse(result['ok'])
        self.assertTrue(self.candidate.closed)
        self.assertIs(self.source.music_source, self.pcm)
        self.assertEqual(self.state.effect_signature(), (0, 0, 0))
        self.assertEqual(self.state.playback_token, 7)

    async def test_effect_refresh_packet_proof_does_not_overwrite_first_audio_measurement(self):
        trace = {'source': self.source, 'expected_music_source': self.pcm, 'guild_id': 1,
                 'track': self.track, 'token': 7, 'first_packet_monotonic': None, 'output_refresh': True}
        self.voice._music_packet_trace = trace
        self.state.first_audio_sent_monotonic = 100
        self.track.agent_received_monotonic = 50
        self.track.agent_timing_ms = {'agent_to_first_packet_ms': 50}
        self.agent._archive_foreground_until = {}
        self.agent._log_start_trace = mock.Mock()
        self.agent._finish_start_attempt = mock.Mock()
        self.agent._record_first_music_packet(self.voice, trace)
        self.assertIsNotNone(trace['first_packet_monotonic'])
        self.assertEqual(self.state.first_audio_sent_monotonic, 100)
        self.assertEqual(self.track.agent_timing_ms, {'agent_to_first_packet_ms': 50})
        self.agent._log_start_trace.assert_not_called()
        self.agent._finish_start_attempt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
