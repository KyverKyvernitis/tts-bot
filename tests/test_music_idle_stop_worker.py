"""Authoritative music STOP lifecycle, with voice/network boundaries adapted.

The real worker methods execute over production state objects. No Discord
connection or audible result is inferred from these offline checks.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock

from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from cogs.musica.runtime_telefone.agente.utilitarios import safe_id


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'cogs/musica/runtime_telefone/agente'


def load_stop_agent():
    tree = ast.parse((RUNTIME / 'reproducao.py').read_text())
    wanted = {'_music_stop_has_activity', 'cmd_stop', '_run_music_start_request',
              'cmd_play', '_play_request', 'cmd_enqueue_discord_attachment', '_enqueue_discord_attachment'}
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ReproducaoMixin')
    methods = [node for node in original.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    assert {node.name for node in methods} == wanted
    server = ast.parse((RUNTIME / 'servidor.py').read_text())
    server_class = next(node for node in server.body if isinstance(node, ast.ClassDef) and node.name == 'MusicAgent')
    methods.append(next(node for node in server_class.body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_dispatch_action'))
    cls = ast.ClassDef(name='Agent', bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    namespace = dict(time=time, asyncio=asyncio, safe_id=safe_id, GuildMusicState=GuildMusicState)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, cls], type_ignores=[])),
                 str(RUNTIME / 'reproducao.py'), 'exec'), namespace)
    return namespace['Agent']


class WorkerIdleStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Agent = load_stop_agent()
        self.agent = self.Agent()
        self.agent.states = {}
        self.agent.executor_mode = 'full'
        self.agent._active_resolve_tasks = {}
        self.agent._starting_pcm = {}
        self.agent._initial_audio = {}
        self.agent._voice_prepare_leases = {}
        self.agent._cancel_voice_preparation = AsyncMock(return_value=False)
        self.agent._cancel_youtube_queue_metadata = Mock()
        self.agent._cancel_prefetch_tasks = Mock()
        self.agent._cancel_idle_disconnect = Mock()
        self.agent._cancel_voice_presence_disconnect = Mock()
        self.agent._record_voice_disconnect = Mock()
        self.agent._voice_human_count = Mock(return_value=1)
        self.agent._set_voice_session_mode = Mock()
        self.agent._stop_player_instance = AsyncMock()

        def bump(st, **options):
            st.playback_token += 1
        self.agent._bump_playback_generation = Mock(side_effect=bump)

        def status(st, value, **options):
            st.status = value
            st.last_event = options.get('event', '')
        self.agent._set_status = Mock(side_effect=status)

    def state(self, *, active=False):
        state = GuildMusicState(guild_id=1)
        state.playback_token, state.queue_reset_generation = 7, 9
        state.last_action, state.last_event = 'previous', 'queue_finished'
        state.history = [AgentTrack(title='History')]
        state.apply_effect_signature((2, 1, 0))
        state.effects_revision = 11
        state.player = SimpleNamespace(is_connected=lambda: True, is_playing=lambda: True,
                                       is_paused=lambda: False, source=SimpleNamespace(has_pending_tts=lambda: True))
        if active:
            state.current = AgentTrack(title='Current', queue_item_id='current', duration=180)
            state.status = 'playing'
        self.agent.states[1] = state
        return state

    def fingerprint(self, st):
        return (st.current, tuple(st.queue), tuple(st.history), st.effect_signature(), st.effects_revision,
                st.playback_token, st.queue_reset_generation, st.last_action, st.last_event,
                st.status, st.player, st.paused)

    def assert_no_stop_effects(self):
        for name in ('_cancel_youtube_queue_metadata', '_cancel_prefetch_tasks', '_cancel_idle_disconnect',
                     '_cancel_voice_presence_disconnect', '_record_voice_disconnect',
                     '_set_voice_session_mode', '_bump_playback_generation', '_set_status'):
            getattr(self.agent, name).assert_not_called()
        self.agent._stop_player_instance.assert_not_awaited()

    async def test_missing_guild_does_not_create_state_or_stop_resources(self):
        self.assertEqual(await self.agent.cmd_stop({'guild_id': 1}), {'ok': True, 'ignored': True})
        self.assertEqual(self.agent.states, {})
        self.assertFalse(hasattr(self.agent, '_music_stop_owners'))
        self.agent._cancel_voice_preparation.assert_not_awaited()
        self.assert_no_stop_effects()

    async def test_idle_history_effects_revision_and_tts_connection_remain_unchanged(self):
        st = self.state()
        before = self.fingerprint(st)
        self.assertEqual(await self.agent.cmd_stop({'guild_id': 1}), {'ok': True, 'ignored': True})
        self.assertEqual(self.fingerprint(st), before)
        self.agent._cancel_voice_preparation.assert_not_awaited()
        self.assert_no_stop_effects()

    async def test_tts_direct_is_not_musical_activity(self):
        st = self.state()
        st.status = 'tts_direct'
        before = self.fingerprint(st)
        self.assertTrue((await self.agent.cmd_stop({'guild_id': 1}))['ignored'])
        self.assertEqual(self.fingerprint(st), before)
        self.assert_no_stop_effects()

    async def test_playing_music_preserves_existing_stop_teardown(self):
        st = self.state(active=True)
        previous_player = st.player
        st.queue = [AgentTrack(title='Queued')]
        result = await self.agent.cmd_stop({'guild_id': 1})
        self.assertTrue(result['ok'])
        self.assertNotIn('ignored', result)
        self.assertIsNone(st.current)
        self.assertIsNone(st.player)
        self.assertEqual((st.queue, st.history, st.effect_signature()), ([], [], (0, 0, 0)))
        self.assertEqual((st.playback_token, st.queue_reset_generation, st.effects_revision), (8, 10, 12))
        self.agent._stop_player_instance.assert_awaited_once_with(previous_player, disconnect=True)
        self.assertEqual(self.agent._music_stop_owners, {})

    async def test_paused_music_is_stopped(self):
        st = self.state(active=True)
        st.paused, st.status = True, 'paused'
        self.assertNotIn('ignored', await self.agent.cmd_stop({'guild_id': 1}))
        self.assertFalse(st.paused)

    async def test_physical_and_virtual_pending_queue_are_both_real_work(self):
        for virtual in (False, True):
            with self.subTest(virtual=virtual):
                self.setUp()
                st = self.state()
                st.queue = [AgentTrack(title='Pending',
                    virtual_playlist_cursor={'provider': 'spotify_public', 'source_url': 'https://example.invalid/list',
                                             'next_offset': 1, 'total_tracks': 20} if virtual else {})]
                self.assertNotIn('ignored', await self.agent.cmd_stop({'guild_id': 1}))
                self.assertEqual(st.queue, [])

    async def test_resolve_task_is_activity_but_finished_task_is_not(self):
        st = self.state()
        self.agent._active_resolve_tasks[1] = Mock(done=lambda: False)
        self.assertTrue(self.agent._music_stop_has_activity(1))
        self.agent._active_resolve_tasks[1] = Mock(done=lambda: True)
        self.assertFalse(self.agent._music_stop_has_activity(1))
        self.assertTrue((await self.agent.cmd_stop({'guild_id': 1}))['ignored'])

    async def test_resolution_and_start_statuses_remain_cancellable(self):
        for value in ('resolving', 'preparing', 'starting', 'queued', 'skipping'):
            with self.subTest(status=value):
                self.setUp()
                st = self.state()
                st.status = value
                self.assertNotIn('ignored', await self.agent.cmd_stop({'guild_id': 1}))

    async def test_preparing_pcm_registries_count_only_for_the_requested_guild(self):
        self.state()
        for attribute in ('_starting_pcm', '_initial_audio'):
            registry = getattr(self.agent, attribute)
            registry[2] = object()
            self.assertFalse(self.agent._music_stop_has_activity(1))
            registry[1] = object()
            self.assertTrue(self.agent._music_stop_has_activity(1))
            registry.clear()

    async def test_valid_voice_lease_is_cancelled_without_resetting_tts_or_history(self):
        st = self.state()
        lease = SimpleNamespace(adopted=False, expires_monotonic=time.monotonic() + 5)
        self.agent._voice_prepare_leases[1] = lease
        before = self.fingerprint(st)
        async def cancel(*args, **kwargs):
            self.agent._voice_prepare_leases.pop(1)
            return True
        self.agent._cancel_voice_preparation.side_effect = cancel
        result = await self.agent.cmd_stop({'guild_id': 1})
        self.assertTrue(result['cancelled_preparation'])
        self.assertNotIn('ignored', result)
        self.assertEqual(self.fingerprint(st), before)
        self.assert_no_stop_effects()

    async def test_expired_or_adopted_leases_do_not_turn_tts_into_music(self):
        self.state()
        for lease in (SimpleNamespace(adopted=True, expires_monotonic=time.monotonic() + 5),
                      SimpleNamespace(adopted=False, expires_monotonic=time.monotonic() - 5)):
            with self.subTest(lease=lease):
                self.agent._voice_prepare_leases[1] = lease
                self.assertTrue((await self.agent.cmd_stop({'guild_id': 1}))['ignored'])
        self.agent._cancel_voice_preparation.assert_not_awaited()

    async def test_natural_finish_during_preparation_await_is_rechecked_before_mutation(self):
        st = self.state(active=True)
        async def finish(*args, **kwargs):
            st.current, st.status = None, 'idle'
            return False
        self.agent._cancel_voice_preparation.side_effect = finish
        before = (tuple(st.history), st.effect_signature(), st.effects_revision, st.player,
                  st.playback_token, st.queue_reset_generation)
        self.assertTrue((await self.agent.cmd_stop({'guild_id': 1}))['ignored'])
        self.assertEqual((tuple(st.history), st.effect_signature(), st.effects_revision, st.player,
                          st.playback_token, st.queue_reset_generation), before)
        self.assert_no_stop_effects()

    async def test_new_play_generation_during_await_is_not_torn_down(self):
        st = self.state(active=True)
        new_track = AgentTrack(title='New session')
        async def replace(*args, **kwargs):
            st.playback_token += 1
            st.current = new_track
            return False
        self.agent._cancel_voice_preparation.side_effect = replace
        self.assertTrue((await self.agent.cmd_stop({'guild_id': 1}))['ignored'])
        self.assertIs(st.current, new_track)
        self.assert_no_stop_effects()

    async def test_state_replacement_during_await_is_not_torn_down(self):
        st = self.state(active=True)
        new_state = GuildMusicState(guild_id=1, current=AgentTrack(title='New'))
        async def replace(*args, **kwargs):
            self.agent.states[1] = new_state
            return False
        self.agent._cancel_voice_preparation.side_effect = replace
        self.assertTrue((await self.agent.cmd_stop({'guild_id': 1}))['ignored'])
        self.assertIs(self.agent.states[1], new_state)
        self.assert_no_stop_effects()

    async def test_two_concurrent_stops_execute_the_teardown_once(self):
        self.state(active=True)
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()
            return False
        self.agent._cancel_voice_preparation.side_effect = wait
        first = asyncio.create_task(self.agent.cmd_stop({'guild_id': 1}))
        await entered.wait()
        self.assertTrue((await self.agent.cmd_stop({'guild_id': 1}))['ignored'])
        release.set()
        self.assertNotIn('ignored', await first)
        self.agent._cancel_voice_preparation.assert_awaited_once()
        self.agent._stop_player_instance.assert_awaited_once()
        self.assertEqual(self.agent._music_stop_owners, {})

    async def test_failed_preparation_releases_owner_and_allows_retry(self):
        self.state(active=True)
        self.agent._cancel_voice_preparation.side_effect = RuntimeError('boundary failure')
        with self.assertRaises(RuntimeError):
            await self.agent.cmd_stop({'guild_id': 1})
        self.assertEqual(self.agent._music_stop_owners, {})
        self.agent._cancel_voice_preparation.side_effect = None
        self.assertNotIn('ignored', await self.agent.cmd_stop({'guild_id': 1}))

    async def test_cancelled_stop_releases_owner(self):
        self.state(active=True)
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.agent._cancel_voice_preparation.side_effect = wait
        task = asyncio.create_task(self.agent.cmd_stop({'guild_id': 1}))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.agent._music_stop_owners, {})

    async def test_dispatch_does_not_accept_a_payload_force_override_for_stop(self):
        self.state()
        result = await self.agent._dispatch_action({'guild_id': 1, 'force_disconnect': True,
                                                   '_agent_action': 'disconnect'}, 'stop')
        self.assertTrue(result['ignored'])
        self.assert_no_stop_effects()

    async def test_explicit_disconnect_still_disconnects_idle_voice(self):
        st = self.state()
        player = st.player
        result = await self.agent._dispatch_action({'guild_id': 1}, 'disconnect')
        self.assertTrue(result['ok'])
        self.assertNotIn('ignored', result)
        self.agent._stop_player_instance.assert_awaited_once_with(player, disconnect=True)

    async def test_explicit_disconnect_can_create_cleanup_state_for_an_absent_guild(self):
        result = await self.agent._dispatch_action({'guild_id': 1}, 'disconnect')
        self.assertTrue(result['ok'])
        self.assertIn(1, self.agent.states)
        self.agent._stop_player_instance.assert_awaited_once_with(None, disconnect=True)

    async def test_music_request_owner_is_visible_before_a_handler_publishes_current(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(body):
            entered.set()
            await release.wait()
            return {'ok': True}
        task = asyncio.create_task(self.agent._run_music_start_request({'guild_id': 1}, handler))
        await entered.wait()
        self.assertTrue(self.agent._music_stop_has_activity(1))
        self.assertFalse(self.agent._music_stop_has_activity(2))
        release.set()
        self.assertEqual(await task, {'ok': True})
        self.assertEqual(self.agent._music_start_requests, {})

    async def test_nested_start_requests_keep_the_outer_owner_until_it_finishes(self):
        calls = []
        async def inner(body):
            self.assertEqual(len(self.agent._music_start_requests[1]), 2)
            return {'ok': True}
        async def outer(body):
            result = await self.agent._run_music_start_request(body, inner)
            calls.append(len(self.agent._music_start_requests[1]))
            return result
        self.assertEqual(await self.agent._run_music_start_request({'guild_id': 1}, outer), {'ok': True})
        self.assertEqual(calls, [1])
        self.assertEqual(self.agent._music_start_requests, {})

    async def test_failed_or_cancelled_start_request_cleans_only_its_own_registry_entry(self):
        async def fail(body):
            raise RuntimeError('boundary failure')
        with self.assertRaises(RuntimeError):
            await self.agent._run_music_start_request({'guild_id': 1}, fail)
        self.assertEqual(self.agent._music_start_requests, {})
        entered = asyncio.Event()
        async def wait(body):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.agent._run_music_start_request({'guild_id': 1}, wait))
        await entered.wait()
        self.agent._music_start_requests[2] = {object()}
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(1, self.agent._music_start_requests)
        self.assertIn(2, self.agent._music_start_requests)

    async def test_stop_during_voice_adoption_prevents_late_play_from_queuing(self):
        st = self.state()
        self.agent._update_auto_leave_from_body = Mock()
        self.agent._query_from_track_meta = Mock(return_value='Song')
        entered, release = asyncio.Event(), asyncio.Event()
        async def adopt(*args):
            entered.set()
            await release.wait()
            return None
        self.agent._adopt_voice_preparation = adopt
        task = asyncio.create_task(self.agent.cmd_play({'guild_id': 1, 'voice_channel_id': 2,
                                                        'query': 'Song'}))
        await entered.wait()
        self.assertTrue(self.agent._music_stop_has_activity(1))
        self.assertNotIn('ignored', await self.agent.cmd_stop({'guild_id': 1}))
        release.set()
        result = await task
        self.assertTrue(result['cancelled'])
        self.assertEqual(st.queue, [])
        self.assertIsNone(st.current)
        self.assertEqual(self.agent._music_start_requests, {})

    async def test_stop_during_attachment_probe_prevents_late_media_enqueue(self):
        st = self.state()
        self.agent.direct_audio_enabled = False
        self.agent.direct_pcm_volume_enabled = False
        self.agent._discord_probe_semaphore = asyncio.Semaphore(1)
        self.agent._cancel_initial_audio = Mock()
        self.agent._confirm_initial_audio = Mock()
        self.agent._discard_prepared_voice_task = AsyncMock()
        self.agent.log = Mock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def probe(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'duration': 5, 'audio_stream_index': 0, 'audio_sample_rate': 48000}
        namespace = self.agent._enqueue_discord_attachment.__func__.__globals__
        namespace.update(
            normalize_reference=lambda ref, gid: dict(ref),
            initial_discord_cdn_url=lambda hint, ref: hint,
            DiscordAttachmentError=ValueError, probe_discord_audio=probe,
        )
        task = asyncio.create_task(self.agent.cmd_enqueue_discord_attachment({
            'guild_id': 1, 'voice_channel_id': 2, 'command_id': 'discord-media:1:100:20',
            'attachment_url_hint': 'https://media.invalid/not-requested.ogg',
            'track': {'attachment_ref': {'guild_id': 1, 'channel_id': 10,
                                         'message_id': 100, 'attachment_id': 20}},
        }))
        await entered.wait()
        self.assertTrue(self.agent._music_stop_has_activity(1))
        self.assertNotIn('ignored', await self.agent.cmd_stop({'guild_id': 1}))
        release.set()
        result = await task
        self.assertTrue(result['cancelled'])
        self.assertEqual(st.queue, [])
        self.assertIsNone(st.current)
        self.assertEqual(self.agent._music_start_requests, {})

    async def test_stop_cancels_an_accepted_attachment_already_waiting_for_another_probe(self):
        st = self.state()
        lock = asyncio.Lock()
        await lock.acquire()
        self.agent._discord_enqueue_locks = {1: lock}
        self.agent._discord_seen_requests = {}
        namespace = self.agent._enqueue_discord_attachment.__func__.__globals__
        namespace.update(normalize_reference=lambda ref, gid: dict(ref), DiscordAttachmentError=ValueError)
        task = asyncio.create_task(self.agent.cmd_enqueue_discord_attachment({
            'guild_id': 1, 'voice_channel_id': 2, 'command_id': 'discord-media:1:101:21',
            'track': {'attachment_ref': {'guild_id': 1, 'channel_id': 10,
                                         'message_id': 101, 'attachment_id': 21}},
        }))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.agent._music_stop_has_activity(1))
        self.assertNotIn('ignored', await self.agent.cmd_stop({'guild_id': 1}))
        lock.release()
        result = await task
        self.assertTrue(result['cancelled'])
        self.assertEqual(st.queue, [])
        self.assertIsNone(st.current)
        self.assertEqual(self.agent._music_start_requests, {})


if __name__ == '__main__':
    unittest.main()
