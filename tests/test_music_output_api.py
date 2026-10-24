"""Production API/event methods with local Discord/encoder boundaries.

The selected MusicAgent methods are compiled from the real server AST;
configuration and state are imported normally. No network or audible output
is simulated as proof of a real Discord quality improvement.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock

from cogs.musica.runtime_telefone.agente.configuracao import (
    audio_output_capabilities, audio_output_policy, opus_passthrough_eligibility,
    validate_audio_output_settings,
)
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'cogs/musica/runtime_telefone/agente/servidor.py'


def load_api():
    tree = ast.parse(SERVER.read_text())
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MusicAgent')
    wanted = {'_audio_output_policy', '_audio_output_runtime', '_audio_output_snapshot',
              'cmd_audio_output', '_dispatch_action', '_wire_discord_events'}
    methods = [n for n in original.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    assert {n.name for n in methods} == wanted
    node = ast.ClassDef(name='Agent', bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    discord = SimpleNamespace(__version__='test-boundary', opus=SimpleNamespace(is_loaded=lambda: False))
    namespace = dict(SimpleNamespace=SimpleNamespace, discord=discord, time=time,
                     safe_id=lambda x: int(x or 0), audio_output_capabilities=audio_output_capabilities,
                     audio_output_policy=audio_output_policy, opus_passthrough_eligibility=opus_passthrough_eligibility,
                     validate_audio_output_settings=validate_audio_output_settings,
                     AGENT_VERSION='0.3.89', truthy=lambda x, default=False: bool(x))
    exec(compile(module, str(SERVER), 'exec'), namespace)
    return namespace['Agent'], discord


class OutputAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Agent, cls.discord = load_api()

    def setUp(self):
        self.agent = self.Agent()
        self.agent.executor_mode = 'full'
        self.agent.states = {}
        self.agent.default_volume_percent = 55
        self.agent.audio_output_default_profile = 'balanced'
        self.agent.log = Mock()
        self.agent._sync_audio_output = AsyncMock(return_value={'state': 'queued'})
        self.agent._voice_client_is_connected = lambda player: player.is_connected()
        self.agent._audio_source_telemetry = lambda source: source.audio_telemetry()
        self.channels = {}
        self.client = SimpleNamespace(get_channel=self.channels.get)
        self.client.event = lambda fn: setattr(self.client, fn.__name__, fn) or fn
        self.agent.client = self.client
        self.agent.get_state = lambda gid: self.agent.states.setdefault(gid, GuildMusicState(gid))

    def command(self, **body):
        return asyncio.run(self.agent._dispatch_action({'guild_id': 1, **body}, 'audio_output'))

    def test_read_before_playback_does_not_create_state_or_sync(self):
        result = self.command()
        self.assertTrue(result['ok'])
        self.assertFalse(result['audio_output']['active'])
        self.assertEqual(result['audio_output']['requested']['profile'], 'balanced')
        self.assertEqual(self.agent.states, {})
        self.agent._sync_audio_output.assert_not_awaited()

    def test_profile_is_scoped_to_one_guild_and_does_not_change_volume(self):
        first = self.agent.get_state(1)
        second = self.agent.get_state(2)
        result = self.command(settings={'profile': 'quality_192'})
        self.assertEqual(first.audio_output_revision, 1)
        self.assertEqual(first.volume_percent, 55)
        self.assertEqual(second.audio_output_settings, {})
        self.assertEqual(result['audio_output']['requested']['bitrate_kbps'], 192)
        self.assertEqual(result['sync']['state'], 'queued')
        self.agent._sync_audio_output.assert_awaited_once_with(1, reason='audio_output_settings_changed')

    def test_same_settings_are_idempotent_and_reset_preserves_other_defaults(self):
        self.command(settings={'profile': 'quality_256', 'fec': False, 'expected_packet_loss': 0})
        state = self.agent.states[1]
        revision = state.audio_output_revision
        self.command(settings={'profile': 'quality_256'})
        self.assertEqual(state.audio_output_revision, revision)
        result = self.command(reset=True)
        self.assertEqual(state.audio_output_settings, {})
        self.assertEqual(result['audio_output']['requested']['profile'], 'balanced')
        self.assertTrue(result['audio_output']['requested']['fec'])
        self.assertEqual(result['audio_output']['requested']['expected_packet_loss'], 0.15)

    def test_invalid_settings_do_not_change_existing_session(self):
        self.command(settings={'profile': 'quality_192'})
        state = self.agent.states[1]
        before = (dict(state.audio_output_settings), state.audio_output_revision)
        for invalid in ({'stream_url': 'https://other'}, {'fec': 'false'}, {'expected_packet_loss': float('nan')}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.command(settings=invalid)
            self.assertEqual((state.audio_output_settings, state.audio_output_revision), before)

    def test_missing_guild_and_conflicting_reset_are_rejected(self):
        for body in ({'guild_id': 0}, {'reset': 'false'}, {'reset': True, 'settings': {}}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.command(**body)

    def test_archive_executor_can_read_but_cannot_mutate_output(self):
        self.agent.executor_mode = 'archive'
        self.assertTrue(self.command()['ok'])
        self.assertFalse(self.command(settings={'profile': 'quality_256'})['ok'])
        self.assertFalse(self.command(reset=True)['ok'])
        self.assertEqual(self.agent.states, {})

    def test_snapshot_uses_cached_channel_and_distinguishes_requested_from_applied(self):
        state = self.agent.get_state(1)
        state.current = AgentTrack(audio_abr=128)
        state.voice_channel_id = 19
        state.audio_output_settings = {'profile': 'quality_256'}
        state.audio_output_applied = {'profile': 'balanced', 'bitrate_kbps': 64, 'state': 'applied',
                                      'private_token': 'PRIVATE'}
        source = SimpleNamespace(audio_telemetry=lambda: {'mix_limited_frames': 2, 'stream_url': 'PRIVATE'})
        state.player = SimpleNamespace(channel=SimpleNamespace(id=19, bitrate=64000), source=source,
                                       is_connected=lambda: True, encoder=SimpleNamespace(set_bitrate=lambda value: value))
        self.channels[19] = SimpleNamespace(id=19, bitrate=256000)
        result = self.command()['audio_output']
        self.assertTrue(result['active'])
        self.assertEqual(result['requested']['bitrate_kbps'], 256)
        self.assertEqual(result['applied']['bitrate_kbps'], 64)
        self.assertEqual(result['metrics']['mix_limited_frames'], 2)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertTrue(result['runtime']['encoder_controls']['bitrate'])
        self.assertIsNone(result['runtime']['complexity'])
        self.assertIsNone(result['runtime']['vbr'])

    def test_inactive_encoder_is_not_reported_as_currently_applied(self):
        state = self.agent.get_state(1)
        state.audio_output_applied = {'bitrate_kbps': 256, 'state': 'applied'}
        self.assertEqual(self.command()['audio_output']['applied'], {})

    def test_channel_event_passes_fresh_channel_to_sync(self):
        self.agent.get_state(1)
        self.agent._wire_discord_events()
        before = SimpleNamespace(id=19, bitrate=64000)
        after = SimpleNamespace(id=19, bitrate=256000, guild=SimpleNamespace(id=1))
        asyncio.run(self.client.on_guild_channel_update(before, after))
        self.agent._sync_audio_output.assert_awaited_once_with(
            1, reason='channel_bitrate_changed', expected_channel_id=19, channel=after)

    def test_unrelated_events_do_not_queue_codec_controls(self):
        self.agent.get_state(1)
        self.agent._wire_discord_events()
        cases = ((19, 19, 64000, 64000, 1), (19, 20, 64000, 96000, 1), (19, 19, 64000, 96000, 2))
        for before_id, after_id, old, new, gid in cases:
            asyncio.run(self.client.on_guild_channel_update(
                SimpleNamespace(id=before_id, bitrate=old),
                SimpleNamespace(id=after_id, bitrate=new, guild=SimpleNamespace(id=gid))))
        self.agent._sync_audio_output.assert_not_awaited()

    def test_channel_handler_is_inert_for_archive_executor(self):
        self.agent.get_state(1)
        self.agent.executor_mode = 'archive'
        self.agent._wire_discord_events()
        asyncio.run(self.client.on_guild_channel_update(
            SimpleNamespace(id=19, bitrate=64000),
            SimpleNamespace(id=19, bitrate=96000, guild=SimpleNamespace(id=1))))
        self.agent._sync_audio_output.assert_not_awaited()

    def test_event_error_does_not_escape_or_log_exception_secrets(self):
        self.agent.get_state(1)
        self.agent._sync_audio_output.side_effect = RuntimeError('PRIVATE-TOKEN')
        self.agent._wire_discord_events()
        asyncio.run(self.client.on_guild_channel_update(
            SimpleNamespace(id=19, bitrate=64000),
            SimpleNamespace(id=19, bitrate=96000, guild=SimpleNamespace(id=1))))
        self.assertNotIn('PRIVATE-TOKEN', str(self.agent.log.call_args))
        self.assertEqual(self.agent.log.call_args.kwargs['error_type'], 'RuntimeError')


if __name__ == '__main__':
    unittest.main()
