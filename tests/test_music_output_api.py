"""Production API/event methods with local Discord/encoder boundaries.

The selected MusicAgent methods are compiled from the real server AST;
configuration and state are imported normally. No network or audible output
is simulated as proof of a real Discord quality improvement.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

from cogs.musica.runtime_telefone.agente.configuracao import (
    OpusDirectPreferenceStore, audio_output_capabilities, audio_output_policy,
    env_float, opus_passthrough_eligibility, validate_audio_output_settings,
)
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from cogs.musica.runtime_telefone.agente.utilitarios import safe_id


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'cogs/musica/runtime_telefone/agente/servidor.py'


def load_api():
    tree = ast.parse(SERVER.read_text())
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MusicAgent')
    wanted = {'_audio_output_policy', '_audio_output_runtime', '_audio_output_snapshot',
              'cmd_audio_output', '_dispatch_action', '_wire_discord_events',
              'handle_command', 'dispatch', '_auth_ok', '_agent_response_headers',
              '_wait_channel_bitrate_hint'}
    methods = [n for n in original.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    assert {n.name for n in methods} == wanted
    node = ast.ClassDef(name='Agent', bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    discord = SimpleNamespace(__version__='test-boundary', opus=SimpleNamespace(is_loaded=lambda: False))
    agent_version = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == 'AGENT_VERSION' for t in n.targets))
    namespace = dict(SimpleNamespace=SimpleNamespace, discord=discord, time=time, web=web,
                     asyncio=asyncio, env_float=env_float,
                     safe_id=safe_id, audio_output_capabilities=audio_output_capabilities,
                     audio_output_policy=audio_output_policy, opus_passthrough_eligibility=opus_passthrough_eligibility,
                     validate_audio_output_settings=validate_audio_output_settings,
                     GuildMusicState=GuildMusicState, AGENT_VERSION=agent_version,
                     DiscordAttachmentError=type('DiscordAttachmentError', (Exception,), {}),
                     short_text=lambda x, limit: str(x)[:limit], truthy=lambda x, default=False: bool(x))
    exec(compile(module, str(SERVER), 'exec'), namespace)
    return namespace['Agent'], discord


class OutputAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Agent, cls.discord = load_api()
        helper_spec = importlib.util.spec_from_file_location(
            'output_api_request_helper', ROOT / 'scripts/testar-saida-musica.py')
        cls.helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(cls.helper)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='output-api-preferences-')
        self.addCleanup(temporary.cleanup)
        self.preferences_path = Path(temporary.name) / 'state/opus-preferences.json'
        self.agent = self.Agent()
        self.agent.opus_direct_preferences = OpusDirectPreferenceStore(self.preferences_path)
        self.agent.executor_mode = 'full'
        self.agent.states = {}
        self.agent.default_volume_percent = 55
        self.agent.audio_output_default_profile = 'balanced'
        self.agent.audio_output_default_opus_direct = 'off'
        self.agent.token = 'api-test-token'
        self.agent.log = Mock()
        self.agent._sync_audio_output = AsyncMock(return_value={'state': 'queued'})
        self.agent._voice_client_is_connected = lambda player: player.is_connected()
        self.agent._audio_source_telemetry = lambda source: source.audio_telemetry()
        self.channels = {}
        self.client = SimpleNamespace(get_channel=self.channels.get, is_ready=lambda: True)
        self.client.event = lambda fn: setattr(self.client, fn.__name__, fn) or fn
        self.agent.client = self.client
        self.agent._maybe_run_maintenance = Mock()
        self.agent.status_payload = lambda: {'version': self.agent._agent_response_headers()['X-Music-Agent-Version']}

    def restart_agent(self):
        """New process boundary data; reload the real store, never copy its map."""
        restarted = self.Agent()
        for name, value in vars(self.agent).items():
            if name not in {'states', 'opus_direct_preferences', '_sync_audio_output'}:
                setattr(restarted, name, value)
        restarted.states = {}
        restarted.opus_direct_preferences = OpusDirectPreferenceStore(self.preferences_path)
        restarted._sync_audio_output = AsyncMock(return_value={'state': 'queued'})
        self.agent = restarted
        return restarted

    def state(self, guild_id):
        """Test data only; do not inject a nonexistent production state method."""
        return self.agent.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))

    def command(self, **body):
        return asyncio.run(self.agent._dispatch_action({'guild_id': 1, **body}, 'audio_output'))

    def http_command(self, body, *, authorized=True):
        request = SimpleNamespace(
            headers={'Authorization': 'Bearer api-test-token' if authorized else 'Bearer other'},
            json=AsyncMock(return_value=body))
        response = asyncio.run(self.agent.handle_command(request))
        return response, request

    def helper_body(self, *arguments):
        args = self.helper.parser().parse_args(list(arguments))
        return self.helper.command_body(args)

    def test_http_helper_adjustments_use_real_state_registry_without_get_state_method(self):
        guild_id = 927002914449424404
        self.assertFalse(hasattr(self.agent, 'get_state'))
        for arguments, expected in (
            (('direct', '--guild', str(guild_id), '--mode', 'auto'), {'opus_direct': 'auto'}),
            (('profile', '--guild', str(guild_id), '--profile', 'quality_192'),
             {'opus_direct': 'auto', 'profile': 'quality_192'}),
            (('protection', '--guild', str(guild_id), '--fec', 'off', '--loss', '0.05'),
             {'opus_direct': 'auto', 'profile': 'quality_192', 'fec': False, 'expected_packet_loss': 0.05}),
            (('reset', '--guild', str(guild_id)), {}),
        ):
            with self.subTest(arguments=arguments):
                response, request = self.http_command(self.helper_body(*arguments))
                self.assertEqual(response.status, 200, response.text)
                request.json.assert_awaited_once()
                result = json.loads(response.text)
                self.assertTrue(result['ok'])
                self.assertEqual(result['version'], self.agent._agent_response_headers()['X-Music-Agent-Version'])
                self.assertEqual(result['guild_id'], guild_id)
                self.assertEqual(result['audio_output']['settings'], expected)
                self.assertEqual(self.agent.states[guild_id].audio_output_settings, expected)
                self.assertEqual(self.agent.states[guild_id].volume_percent, 55)
        revision = self.agent.states[guild_id].audio_output_revision
        response, _ = self.http_command(self.helper_body('status', '--guild', str(guild_id)))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.agent.states[guild_id].audio_output_revision, revision)
        self.agent._sync_audio_output.assert_awaited()
        self.assertEqual(self.agent._sync_audio_output.await_count, 4)

    def test_http_direct_setting_preserves_existing_active_session(self):
        state = self.state(1)
        state.current = AgentTrack(audio_codec='opus', audio_abr=131, audio_sample_rate=48000,
                                   audio_channels=2)
        state.voice_channel_id = 19
        state.player = SimpleNamespace(channel=SimpleNamespace(id=19, bitrate=96000),
                                       source=SimpleNamespace(audio_telemetry=lambda: {}),
                                       is_connected=lambda: True)
        current, player = state.current, state.player
        response, _ = self.http_command(self.helper_body('direct', '--guild', '1', '--mode', 'auto'))
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(state.audio_output_settings, {'opus_direct': 'auto'})
        self.assertIs(state.current, current)
        self.assertIs(state.player, player)
        result = json.loads(response.text)
        self.assertTrue(result['audio_output']['active'])
        self.assertEqual(result['audio_output']['requested']['opus_direct'], 'auto')
        self.assertIn('volume_requires_pcm', result['audio_output']['passthrough']['reasons'])
        self.assertIn('source_bitrate_limit', result['audio_output']['passthrough']['reasons'])

    def test_status_uses_copy_capacity_instead_of_encoder_target_for_source_candidate(self):
        state = self.state(1)
        state.current = AgentTrack(audio_codec='opus', audio_abr=220, audio_sample_rate=48000,
                                   audio_channels=2)
        state.voice_channel_id = 19
        state.volume_percent = 100
        state.audio_output_settings = {'opus_direct': 'auto', 'profile': 'quality_192'}
        state.player = SimpleNamespace(channel=SimpleNamespace(id=19, bitrate=256000),
                                       source=SimpleNamespace(audio_telemetry=lambda: {}),
                                       is_connected=lambda: True)
        result = self.command()['audio_output']
        self.assertEqual(result['requested']['bitrate_kbps'], 192)
        self.assertEqual(result['requested']['copy_bitrate_kbps'], 256)
        self.assertTrue(result['passthrough']['candidate'])
        self.assertNotIn('source_bitrate_limit', result['passthrough']['reasons'])
        self.assertFalse(result['passthrough'].get('copy_active', False))

    def test_http_invalid_settings_return_400_without_mutating_existing_session(self):
        state = self.state(1)
        state.audio_output_settings = {'opus_direct': 'auto', 'profile': 'quality_192'}
        before = (dict(state.audio_output_settings), state.audio_output_revision, state.volume_percent)
        for settings in ({'opus_direct': True}, {'opus_direct': 'on'}, {'fec': 'false'},
                         {'expected_packet_loss': 2}, {'private_setting': 'ignored'}):
            with self.subTest(settings=settings):
                response, _ = self.http_command({'action': 'audio_output', 'guild_id': 1, 'settings': settings})
                self.assertEqual(response.status, 400)
                self.assertFalse(json.loads(response.text)['ok'])
                self.assertEqual((state.audio_output_settings, state.audio_output_revision,
                                  state.volume_percent), before)
        self.agent._sync_audio_output.assert_not_awaited()

    def test_http_authentication_is_checked_before_dispatch_or_reading_body(self):
        response, request = self.http_command(
            self.helper_body('direct', '--guild', '1', '--mode', 'auto'), authorized=False)
        self.assertEqual(response.status, 401)
        request.json.assert_not_awaited()
        self.agent._maybe_run_maintenance.assert_not_called()
        self.agent._sync_audio_output.assert_not_awaited()
        self.assertEqual(self.agent.states, {})

    def test_http_archive_executor_still_rejects_adjustments_without_mutation(self):
        self.agent.executor_mode = 'archive'
        response, _ = self.http_command(self.helper_body('direct', '--guild', '1', '--mode', 'auto'))
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(response.text)['ok'])
        self.assertEqual(self.agent.states, {})
        self.agent._sync_audio_output.assert_not_awaited()

    def test_direct_preference_survives_new_agent_and_read_before_state_exists(self):
        self.agent.audio_output_default_opus_direct = 'auto'
        result = self.command(settings={'opus_direct': 'off'})
        self.assertTrue(result['audio_output']['persistence']['stored'])
        self.assertEqual(result['audio_output']['persistence']['mode'], 'off')
        self.restart_agent()
        result = self.command()
        self.assertEqual(result['audio_output']['requested']['opus_direct'], 'off')
        self.assertEqual(result['audio_output']['settings'], {'opus_direct': 'off'})
        self.assertEqual(self.agent.states, {})
        self.agent._sync_audio_output.assert_not_awaited()
        self.assertEqual(self.command(guild_id=2)['audio_output']['requested']['opus_direct'], 'auto')

    def test_preference_survives_pruned_idle_state_and_does_not_persist_profile_or_fec(self):
        self.command(settings={'opus_direct': 'auto', 'profile': 'quality_256', 'fec': False,
                               'expected_packet_loss': .05})
        self.assertEqual(json.loads(self.preferences_path.read_bytes())['guilds'], {'1': 'auto'})
        self.agent.states.clear()
        state = self.state(1)
        result = self.command()['audio_output']
        self.assertEqual(state.audio_output_settings, {})
        self.assertEqual(result['requested']['opus_direct'], 'auto')
        self.assertEqual(result['requested']['profile'], 'balanced')
        self.assertTrue(result['requested']['fec'])
        self.assertEqual(result['requested']['expected_packet_loss'], .15)

    def test_reset_removes_persisted_override_and_returns_configured_auto_default(self):
        self.agent.audio_output_default_opus_direct = 'auto'
        self.command(settings={'opus_direct': 'off', 'profile': 'quality_192'})
        state = self.state(1)
        state.volume_percent = 100
        result = self.command(reset=True)['audio_output']
        self.assertFalse(result['persistence']['stored'])
        self.assertEqual(result['requested']['opus_direct'], 'auto')
        self.assertEqual(state.audio_output_settings, {})
        self.assertEqual(state.volume_percent, 100)
        self.restart_agent()
        self.assertEqual(self.command()['audio_output']['requested']['opus_direct'], 'auto')

    def test_profile_and_protection_alone_do_not_create_preference_file(self):
        with patch.object(self.agent.opus_direct_preferences, 'set_mode', side_effect=AssertionError('no write')):
            self.command(settings={'profile': 'quality_192'})
            self.command(settings={'fec': False, 'expected_packet_loss': .05})
        self.assertFalse(self.preferences_path.exists())

    def test_idempotent_explicit_mode_is_persisted_before_sync_and_not_rewritten_later(self):
        state = self.state(1)
        state.audio_output_settings = {'opus_direct': 'auto'}
        def observe_saved(*args, **kwargs):
            self.assertEqual(json.loads(self.preferences_path.read_bytes())['guilds'], {'1': 'auto'})
            return {'state': 'queued'}
        self.agent._sync_audio_output.side_effect = observe_saved
        result = self.command(settings={'opus_direct': 'auto'})
        self.assertTrue(result['audio_output']['persistence']['stored'])
        self.agent._sync_audio_output.assert_awaited_once()
        revision = state.audio_output_revision
        with patch.object(self.agent.opus_direct_preferences, '_atomic_write', side_effect=AssertionError('must not rewrite')):
            self.command(settings={'opus_direct': 'auto'})
        self.assertEqual(state.audio_output_revision, revision)

    def test_failed_save_does_not_mutate_active_state_or_queue_encoder_controls(self):
        state = self.state(1)
        state.audio_output_settings = {'profile': 'quality_192'}
        before = (dict(state.audio_output_settings), state.audio_output_revision, state.updated_at)
        with patch.object(self.agent.opus_direct_preferences, 'set_mode', side_effect=OSError('PRIVATE-PATH')):
            response, _ = self.http_command(self.helper_body('direct', '--guild', '1', '--mode', 'auto'))
        self.assertEqual(response.status, 400)
        self.assertNotIn('PRIVATE-PATH', response.text)
        self.assertEqual((state.audio_output_settings, state.audio_output_revision, state.updated_at), before)
        self.agent._sync_audio_output.assert_not_awaited()
        self.assertFalse(self.preferences_path.exists())

    def test_corrupt_preference_store_is_reported_and_never_overwritten_by_command(self):
        self.preferences_path.parent.mkdir(parents=True)
        self.preferences_path.write_bytes(b'invalid')
        self.restart_agent()
        self.agent.audio_output_default_opus_direct = 'auto'
        result = self.command()['audio_output']
        self.assertFalse(result['persistence']['ready'])
        self.assertEqual(result['requested']['opus_direct'], 'off')
        response, _ = self.http_command(self.helper_body('direct', '--guild', '1', '--mode', 'auto'))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.preferences_path.read_bytes(), b'invalid')
        self.agent._sync_audio_output.assert_not_awaited()

    def test_capabilities_report_explicit_global_off_and_requested_global_auto(self):
        self.assertEqual(self.command()['capabilities']['opus_direct_default'], 'off')
        self.agent.audio_output_default_opus_direct = 'auto'
        result = self.command()
        self.assertEqual(result['capabilities']['opus_direct_default'], 'auto')
        self.assertTrue(result['capabilities']['opus_direct_persistent'])
        self.assertEqual(result['audio_output']['requested']['opus_direct'], 'auto')

    def test_read_before_playback_does_not_create_state_or_sync(self):
        result = self.command()
        self.assertTrue(result['ok'])
        self.assertFalse(result['audio_output']['active'])
        self.assertEqual(result['audio_output']['requested']['profile'], 'balanced')
        self.assertEqual(result['audio_output']['requested']['opus_direct'], 'off')
        self.assertEqual(self.agent.states, {})
        self.agent._sync_audio_output.assert_not_awaited()

    def test_profile_is_scoped_to_one_guild_and_does_not_change_volume(self):
        first = self.state(1)
        second = self.state(2)
        result = self.command(settings={'profile': 'quality_192'})
        self.assertEqual(first.audio_output_revision, 1)
        self.assertEqual(first.volume_percent, 55)
        self.assertEqual(second.audio_output_settings, {})
        self.assertEqual(result['audio_output']['requested']['bitrate_kbps'], 192)
        self.assertEqual(result['sync']['state'], 'queued')
        self.agent._sync_audio_output.assert_awaited_once_with(1, reason='audio_output_settings_changed')

    def test_direct_mode_is_scoped_and_does_not_raise_volume_or_change_protection(self):
        one = self.state(1)
        two = self.state(2)
        self.command(settings={'profile': 'quality_192', 'fec': True, 'expected_packet_loss': .15})
        result = self.command(settings={'opus_direct': 'auto'})
        self.assertEqual(result['version'], self.agent._agent_response_headers()['X-Music-Agent-Version'])
        self.assertEqual(result['audio_output']['settings']['opus_direct'], 'auto')
        self.assertEqual(result['audio_output']['requested']['opus_direct'], 'auto')
        self.assertEqual(result['audio_output']['requested']['profile'], 'quality_192')
        self.assertTrue(result['audio_output']['requested']['fec'])
        self.assertEqual(result['audio_output']['requested']['expected_packet_loss'], .15)
        self.assertEqual(one.volume_percent, 55)
        self.assertEqual(two.audio_output_settings, {})
        self.assertIn('volume_requires_pcm', result['audio_output']['passthrough']['reasons'])
        self.assertFalse(result['audio_output']['passthrough']['candidate'])

    def test_direct_reset_restores_off_but_preserves_volume(self):
        self.command(settings={'opus_direct': 'auto'})
        state = self.agent.states[1]
        state.volume_percent = 100
        result = self.command(reset=True)
        self.assertEqual(result['audio_output']['requested']['opus_direct'], 'off')
        self.assertEqual(result['audio_output']['settings'], {})
        self.assertEqual(state.volume_percent, 100)

    def test_copy_status_does_not_claim_fallback_encoder_controls_apply_to_original_packets(self):
        state = self.state(1)
        state.current = AgentTrack(audio_codec='opus', audio_abr=128, audio_sample_rate=48000,
                                   audio_channels=2)
        state.voice_channel_id = 19
        state.volume_percent = 100
        state.audio_output_settings = {'opus_direct': 'auto', 'profile': 'quality_256'}
        state.audio_output_applied = {'profile': 'quality_256', 'bitrate_kbps': 256, 'state': 'applied',
                                     'fec': True, 'expected_packet_loss': .15, 'encoder_kind': 'hybrid_fallback'}
        live = {'state': 'copy', 'copy_active': True, 'copied_packets': 12, 'encoded_packets': 0,
                'validated_packets': 12, 'source_bitrate_kbps': 128,
                'fallback_encoder': {'bitrate_kbps': 256, 'fec': True, 'expected_packet_loss': .15}}
        source = SimpleNamespace(audio_telemetry=lambda: {}, opus_direct_status=lambda: dict(live))
        state.player = SimpleNamespace(channel=SimpleNamespace(id=19, bitrate=256000), source=source,
                                       is_connected=lambda: True, encoder=SimpleNamespace())
        result = self.command()['audio_output']
        self.assertEqual(result['applied']['state'], 'copied')
        self.assertEqual(result['applied']['output_mode'], 'opus_direct')
        for key in ('bitrate_kbps', 'fec', 'expected_packet_loss'):
            self.assertIsNone(result['applied'][key])
        self.assertEqual(result['passthrough']['fallback_encoder']['bitrate_kbps'], 256)
        self.assertEqual(result['passthrough']['source_bitrate_kbps'], 128)
        live.update(state='pcm_fallback', copy_active=False, encoded_packets=3)
        fallback = self.command()['audio_output']
        self.assertEqual(fallback['applied']['state'], 'applied')
        self.assertEqual(fallback['applied']['output_mode'], 'pcm_encoded')
        self.assertTrue(fallback['applied']['fec'])
        self.assertEqual(fallback['applied']['bitrate_kbps'], 256)

    def test_enabling_direct_on_an_ordinary_pcm_source_waits_for_next_track(self):
        state = self.state(1)
        state.current = AgentTrack(audio_codec='opus', audio_abr=128, audio_sample_rate=48000,
                                   audio_channels=2)
        state.voice_channel_id = 19
        state.volume_percent = 100
        source = SimpleNamespace(audio_telemetry=lambda: {})
        state.player = SimpleNamespace(channel=SimpleNamespace(id=19, bitrate=256000), source=source,
                                       is_connected=lambda: True, encoder=SimpleNamespace())
        result = self.command(settings={'opus_direct': 'auto'})['audio_output']
        self.assertTrue(result['passthrough']['candidate'])
        self.assertEqual(result['passthrough']['reason'], 'next_track_required')
        self.assertIs(state.player.source, source)
        self.assertEqual(state.volume_percent, 100)

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
        for invalid in ({'stream_url': 'https://other'}, {'fec': 'false'}, {'expected_packet_loss': float('nan')},
                        {'opus_direct': True}, {'opus_direct': 'on'}):
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
        self.assertFalse(self.command(settings={'opus_direct': 'auto'})['ok'])
        self.assertFalse(self.command(reset=True)['ok'])
        self.assertEqual(self.agent.states, {})

    def test_snapshot_uses_cached_channel_and_distinguishes_requested_from_applied(self):
        state = self.state(1)
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
        state = self.state(1)
        state.audio_output_applied = {'bitrate_kbps': 256, 'state': 'applied'}
        self.assertEqual(self.command()['audio_output']['applied'], {})

    def test_channel_event_passes_fresh_channel_to_sync(self):
        self.state(1)
        self.agent._wire_discord_events()
        before = SimpleNamespace(id=19, bitrate=64000)
        after = SimpleNamespace(id=19, bitrate=256000, guild=SimpleNamespace(id=1))
        asyncio.run(self.client.on_guild_channel_update(before, after))
        self.agent._sync_audio_output.assert_awaited_once_with(
            1, reason='channel_bitrate_changed', expected_channel_id=19, channel=after)

    def test_unrelated_events_do_not_queue_codec_controls(self):
        self.state(1)
        self.agent._wire_discord_events()
        cases = ((19, 19, 64000, 64000, 1), (19, 20, 64000, 96000, 1), (19, 19, 64000, 96000, 2))
        for before_id, after_id, old, new, gid in cases:
            asyncio.run(self.client.on_guild_channel_update(
                SimpleNamespace(id=before_id, bitrate=old),
                SimpleNamespace(id=after_id, bitrate=new, guild=SimpleNamespace(id=gid))))
        self.agent._sync_audio_output.assert_not_awaited()

    def test_channel_handler_is_inert_for_archive_executor(self):
        self.state(1)
        self.agent.executor_mode = 'archive'
        self.agent._wire_discord_events()
        asyncio.run(self.client.on_guild_channel_update(
            SimpleNamespace(id=19, bitrate=64000),
            SimpleNamespace(id=19, bitrate=96000, guild=SimpleNamespace(id=1))))
        self.agent._sync_audio_output.assert_not_awaited()

    def test_event_error_does_not_escape_or_log_exception_secrets(self):
        self.state(1)
        self.agent._sync_audio_output.side_effect = RuntimeError('PRIVATE-TOKEN')
        self.agent._wire_discord_events()
        asyncio.run(self.client.on_guild_channel_update(
            SimpleNamespace(id=19, bitrate=64000),
            SimpleNamespace(id=19, bitrate=96000, guild=SimpleNamespace(id=1))))
        self.assertNotIn('PRIVATE-TOKEN', str(self.agent.log.call_args))
        self.assertEqual(self.agent.log.call_args.kwargs['error_type'], 'RuntimeError')


if __name__ == '__main__':
    unittest.main()
