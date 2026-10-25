"""Controller pre-play bitrate policy with production functions and Discord boundaries.

Uses stdlib unittest. The actual temporary bitrate/restore methods and the
actual startup helpers execute here; Discord HTTP and persistent storage are
explicit local boundaries. No live Discord timing or audio result is claimed.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]


def load_production():
    path = ROOT / "cogs/musica/reproducao/inicio.py"
    tree = ast.parse(path.read_text())
    wanted = {"channel_bitrate_hints", "_music_channel_policy_lock", "prepare_music_channel", "finish_music_channel_preparation",
              "prepare_voice", "cancel_voice_preparation"}
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in wanted]
    assert {node.name for node in selected} == wanted
    namespace = {"asyncio": asyncio, "contextlib": contextlib, "time": time,
                 "logger": logging.getLogger("test.music.preplay"), "_CHANNEL_PREPARE_TIMEOUT": 0.1}
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])),
                 str(path), "exec"), namespace)

    legacy = ROOT / "cogs/musica/legado/roteador_audio.py"
    tree = ast.parse(legacy.read_text())
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and any(getattr(method, "name", "") == "_boost_auto_bitrate_for_music" for method in node.body))
    names = {"_target_auto_bitrate", "_boost_auto_bitrate_for_music", "_restore_auto_bitrate_for_state",
             "_schedule_agent_playback_started_effects", "_active_player_count"}
    methods = [node for node in klass.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in names]
    policy = ast.ClassDef(name="Policy", bases=[], keywords=[], body=methods, decorator_list=[])
    namespace.update(MUSIC_AUTO_BITRATE_ENABLED=True, MUSIC_AUTO_BITRATE_MIN_GAIN=16000,
                     MUSIC_AUTO_BITRATE_MAX=384000, MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS=1,
                     MUSIC_HEAVY_LOAD_MAX_ABR=128, MUSIC_MAX_AUDIO_BITRATE_STABLE=160,
                     discord=SimpleNamespace(Forbidden=PermissionError, HTTPException=RuntimeError),
                     __package__='cogs.musica.legado',
                     _consume_expected_music_exception=lambda task: None if task.cancelled() else task.exception())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, policy], type_ignores=[])),
                 str(legacy), "exec"), namespace)
    controls = ROOT / "cogs/musica/reproducao/controle_remoto.py"
    node = next(node for node in ast.parse(controls.read_text()).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "enviar_controle_remoto")
    namespace.update(uuid=uuid, config=SimpleNamespace(MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS=12))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[])),
                 str(controls), "exec"), namespace)
    return namespace


class PreplayBitrateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prod = load_production()
        self.events = []
        self.server_bitrate = 96000
        self.generation = 0
        self.manage_channels = True
        self.persistence_ok = True
        self.edit_gate = None
        self.edit_entered = asyncio.Event()
        self.edits = []
        self.saved = None
        self.fetched = 0
        self.state = SimpleNamespace(
            current=None, current_source=None, current_status='idle', last_voice_channel_id=2, auto_bitrate_channel_id=None,
            auto_bitrate_original=None, auto_bitrate_boosted=None)
        self.guild = SimpleNamespace(id=1, bitrate_limit=256000)

        async def edit(*, bitrate, reason):
            self.events.append("edit_started")
            self.edits.append(bitrate)
            self.edit_entered.set()
            if self.edit_gate is not None:
                await self.edit_gate.wait()
            self.server_bitrate = bitrate
            self.events.append("edit_done")
            return SimpleNamespace(id=2, guild=self.guild, bitrate=bitrate, edit=edit)
        self.channel = SimpleNamespace(id=2, guild=self.guild, bitrate=96000, edit=edit)

        async def fetch(channel_id):
            self.fetched += 1
            self.events.append("fetch")
            return SimpleNamespace(id=channel_id, guild=self.guild, bitrate=self.server_bitrate, edit=edit)
        self.guild.fetch_channel = fetch
        self.guild.get_channel = lambda channel_id: self.channel if channel_id == 2 else None
        self.router = self.prod["Policy"]()
        self.router.bot = SimpleNamespace(
            get_guild=lambda guild_id: self.guild if guild_id == 1 else None,
            get_channel=self.guild.get_channel)
        self.router.get_state = lambda guild_id: self.state
        self.router._states = {1: self.state}
        self.router.current_music_operation_generation = lambda guild_id: self.generation
        self.router._is_normal_voice_channel = lambda channel: True
        self.router._bot_can_manage_voice_channel = lambda guild, channel: self.manage_channels

        async def save(guild_id, record):
            if not self.persistence_ok:
                return False
            self.saved = dict(record)
            return True
        async def clear(guild_id):
            self.saved = None
        def load(guild_id, state):
            if self.saved:
                state.auto_bitrate_channel_id = self.saved['channel_id']
                state.auto_bitrate_original = self.saved['original_bitrate']
                state.auto_bitrate_boosted = self.saved['boosted_bitrate']
            return self.saved
        self.router._save_auto_bitrate_record = save
        self.router._clear_auto_bitrate_record = clear
        self.router._load_auto_bitrate_record_into_state = load

    async def prepare(self, owner="play", **kwargs):
        return await self.prod["prepare_music_channel"](
            self.router, guild_id=1, voice_channel_id=2,
            generation=kwargs.pop("generation", self.generation), operation_id=owner, **kwargs)

    async def finish(self, owner="play", committed=False):
        await self.prod["finish_music_channel_preparation"](
            self.router, guild_id=1, voice_channel_id=2, operation_id=owner, committed=committed)

    def test_preparing_new_guild_counts_together_with_existing_player(self):
        self.router._states[7] = SimpleNamespace(
            current=object(), current_source=None, current_status='playing')
        self.router._music_channel_preparations = {(1, 2): {'owners': {'play'}}}
        self.assertEqual(self.router._target_auto_bitrate(self.guild, self.channel), 160000)

    def test_two_idle_pending_guilds_use_stable_bitrate(self):
        self.router._music_channel_preparations = {
            (1, 2): {'owners': {'play'}}, (7, 8): {'owners': {'other'}}}
        self.assertEqual(self.router._active_player_count(), 0)
        self.assertEqual(self.router._target_auto_bitrate(self.guild, self.channel), 160000)

    def test_pending_channels_and_already_active_guild_are_not_double_counted(self):
        self.state.current_status = 'playing'
        self.router._music_channel_preparations = {
            (1, 2): {'owners': {'play'}}, (1, 3): {'owners': {'new-channel'}},
            (7, 8): {'owners': set()}}
        self.assertEqual(self.router._target_auto_bitrate(self.guild, self.channel), 256000)

    def test_three_pending_guilds_use_existing_heavy_load_limit(self):
        self.router._music_channel_preparations = {
            (1, 2): {'owners': {'play'}}, (7, 8): {'owners': {'other'}},
            (9, 10): {'owners': {'third'}}}
        self.assertEqual(self.router._target_auto_bitrate(self.guild, self.channel), 128000)

    async def test_confirmed_edit_produces_hint_even_before_gateway_cache_changes(self):
        result = await self.prepare()
        self.assertEqual(result, {"state": "ready", "channel_id": 2, "bitrate_bps": 256000, "changed": True})
        self.assertEqual(self.channel.bitrate, 96000)
        self.assertEqual(self.server_bitrate, 256000)
        self.assertEqual(self.prod['channel_bitrate_hints'](result),
                         {"channel_bitrate_hint_bps": 256000, "channel_bitrate_hint_channel_id": 2})
        await self.finish(committed=True)

    async def test_limit_96_no_permission_and_disabled_policy_do_not_claim_an_edit(self):
        for case in ("limit", "permission", "disabled", "persistence"):
            with self.subTest(case=case):
                self.setUp()
                if case == "limit": self.guild.bitrate_limit = 96000
                if case == "permission": self.manage_channels = False
                if case == "disabled": self.prod['MUSIC_AUTO_BITRATE_ENABLED'] = False
                if case == "persistence": self.persistence_ok = False
                result = await self.prepare()
                self.assertEqual(result['state'], 'skipped')
                self.assertEqual(self.prod['channel_bitrate_hints'](result), {})
                self.assertEqual(self.edits, [])
                await self.finish()
                self.assertEqual(self.fetched, 0)

    async def test_no_router_booster_or_wrong_generation_is_fast_noop(self):
        result = await self.prod['prepare_music_channel'](
            SimpleNamespace(), guild_id=1, voice_channel_id=2, generation=0)
        self.assertEqual(result['state'], 'skipped')
        self.generation = 1
        result = await self.prepare(generation=0)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(self.edits, [])

    async def test_already_boosted_verified_state_uses_no_extra_channel_edit(self):
        await self.prepare('first')
        await self.finish('first', committed=True)
        second = await self.prepare('second')
        self.assertEqual(second['bitrate_bps'], 256000)
        self.assertEqual(self.edits, [256000])
        await self.finish('second', committed=True)

    async def test_completed_cache_does_not_skip_new_boost_after_session_restore(self):
        await self.prepare('first')
        await self.finish('first', committed=True)
        fresh = await self.guild.fetch_channel(2)
        await self.router._restore_auto_bitrate_for_state(self.guild, self.state, channel_hint=fresh)
        self.assertEqual(self.server_bitrate, 96000)
        result = await self.prepare('next')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(self.edits, [256000, 96000, 256000])
        await self.finish('next', committed=True)

    async def test_noop_cache_does_not_delay_newly_available_permission(self):
        self.manage_channels = False
        self.assertEqual((await self.prepare('first'))['state'], 'skipped')
        self.manage_channels = True
        self.assertEqual((await self.prepare('next'))['state'], 'ready')
        self.assertEqual(self.edits, [256000])
        await self.finish('next', committed=True)

    async def test_shared_edit_has_independent_owners_and_failure_cannot_undo_success(self):
        self.edit_gate = asyncio.Event()
        first = asyncio.create_task(self.prepare('a'))
        await self.edit_entered.wait()
        second = asyncio.create_task(self.prepare('b'))
        await asyncio.sleep(0)
        self.edit_gate.set()
        results = await asyncio.gather(first, second)
        self.assertEqual([r['state'] for r in results], ['ready', 'ready'])
        self.assertEqual(self.edits, [256000])
        await self.finish('a', committed=False)
        self.assertEqual(self.fetched, 0)
        await self.finish('b', committed=True)
        self.assertEqual(self.server_bitrate, 256000)
        self.assertEqual(self.fetched, 0)

    async def test_cancelling_one_waiter_does_not_cancel_shared_edit(self):
        self.edit_gate = asyncio.Event()
        first = asyncio.create_task(self.prepare('a'))
        await self.edit_entered.wait()
        second = asyncio.create_task(self.prepare('b'))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        await self.finish('a')
        self.edit_gate.set()
        self.assertEqual((await second)['state'], 'ready')
        await self.finish('b', committed=True)
        self.assertEqual(self.edits, [256000])

    async def test_newer_generation_shares_pending_edit_but_old_play_is_cancelled(self):
        self.edit_gate = asyncio.Event()
        old = asyncio.create_task(self.prepare('old', generation=0))
        await self.edit_entered.wait()
        self.generation = 1
        new = asyncio.create_task(self.prepare('new', generation=1))
        await asyncio.sleep(0)
        self.edit_gate.set()
        self.assertEqual((await old)['state'], 'cancelled')
        self.assertEqual((await new)['state'], 'ready')
        await self.finish('old')
        await self.finish('new', committed=True)
        self.assertEqual(self.edits, [256000])

    async def test_failed_metadata_restores_idle_channel_from_fresh_observation(self):
        await self.prepare()
        await self.finish()
        self.assertEqual(self.fetched, 1)
        self.assertEqual(self.edits, [256000, 96000])
        self.assertEqual(self.server_bitrate, 96000)
        self.assertIsNone(self.saved)

    async def test_cleanup_preserves_staff_edit_and_active_or_newer_play(self):
        await self.prepare()
        self.server_bitrate = 128000
        await self.finish()
        self.assertEqual(self.edits, [256000])
        self.assertEqual(self.server_bitrate, 128000)
        self.assertIsNone(self.saved)
        self.setUp()
        await self.prepare()
        self.state.current = object()
        await self.finish()
        self.assertEqual(self.fetched, 0)
        self.assertEqual(self.server_bitrate, 256000)

    async def test_failed_fresh_fetch_leaves_persisted_record_for_reconcile(self):
        await self.prepare()
        async def unavailable(channel_id): raise RuntimeError('offline')
        self.guild.fetch_channel = unavailable
        await self.finish()
        self.assertIsNotNone(self.saved)
        self.assertEqual(self.edits, [256000])

    async def test_edit_timeout_is_bounded_and_does_not_send_unconfirmed_hint(self):
        self.prod['_CHANNEL_PREPARE_TIMEOUT'] = .01
        self.edit_gate = asyncio.Event()
        started = time.monotonic()
        result = await self.prepare()
        self.assertLess(time.monotonic() - started, .3)
        self.assertEqual(result['state'], 'timeout')
        self.assertEqual(self.prod['channel_bitrate_hints'](result), {})
        await self.finish()
        self.assertEqual(self.server_bitrate, 96000)
        self.assertIsNone(self.saved)

    async def test_prepare_voice_handshake_overlaps_channel_edit(self):
        self.edit_gate = asyncio.Event()
        voice_entered = asyncio.Event()
        voice_gate = asyncio.Event()
        async def command(action, **body):
            self.events.append(action)
            if action == 'prepare_voice':
                voice_entered.set()
                await voice_gate.wait()
            return {'ok': True}
        task = asyncio.create_task(self.prod['prepare_voice'](
            self.router, command, guild_id=1, voice_channel_id=2, generation=0,
            prepare_id='voice', trace_id='voice'))
        await self.edit_entered.wait()
        await voice_entered.wait()
        self.assertNotIn('edit_done', self.events)
        self.edit_gate.set()
        voice_gate.set()
        await task
        await self.prod['cancel_voice_preparation'](command, 1, 'voice', task, router=self.router)
        self.assertEqual(self.edits, [256000, 96000])

    async def test_late_speculative_task_is_released_after_metadata_failure(self):
        async def command(action, **body): return {'ok': True}
        task = asyncio.create_task(self.prod['prepare_voice'](
            self.router, command, guild_id=1, voice_channel_id=2, generation=0,
            prepare_id='late', trace_id='late'))
        await self.finish('late')  # metadata failure before background task ran
        await self.prod['cancel_voice_preparation'](command, 1, 'late', task, router=self.router)
        self.assertEqual(self.edits, [256000, 96000])

    async def test_actual_queue_control_sends_hint_only_after_edit(self):
        async def command(action, **body):
            self.events.append('command')
            self.assertEqual(self.server_bitrate, 256000)
            self.assertEqual(body['channel_bitrate_hint_bps'], 256000)
            self.assertEqual(body['channel_bitrate_hint_channel_id'], 2)
            self.assertEqual(body['voice_channel_id'], 2)
            return {'ok': True}
        self.prod['music_agent_command'] = command
        result = await self.prod['enviar_controle_remoto'](self.router, 'queue_play_now', guild_id=1)
        self.assertTrue(result['ok'])
        self.assertLess(self.events.index('edit_done'), self.events.index('command'))

    async def test_cancelled_generation_prevents_queue_control_dispatch(self):
        self.edit_gate = asyncio.Event()
        sent = []
        async def command(action, **body): sent.append(action); return {'ok': True}
        self.prod['music_agent_command'] = command
        task = asyncio.create_task(self.prod['enviar_controle_remoto'](self.router, 'skip', guild_id=1))
        await self.edit_entered.wait()
        self.generation = 1
        self.edit_gate.set()
        self.assertTrue((await task)['cancelled'])
        self.assertEqual(sent, [])

    async def test_virtual_queue_remove_and_move_do_not_edit_idle_channel(self):
        sent = []
        async def command(action, **body):
            sent.append((action, body))
            return {'ok': True}
        self.prod['music_agent_command'] = command
        for alias in ('queue_virtual_action', 'virtual_queue_action'):
            for operation in ('remove', 'move'):
                with self.subTest(action=alias, operation=operation):
                    result = await self.prod['enviar_controle_remoto'](
                        self.router, alias, guild_id=1, virtual_action=operation)
                    self.assertTrue(result['ok'])
                    self.assertNotIn('channel_bitrate_hint_bps', sent[-1][1])
        self.assertEqual(len(sent), 4)
        self.assertEqual(self.edits, [])
        self.assertIsNone(self.saved)
        self.assertFalse(getattr(self.router, '_music_channel_preparations', {}))

    async def test_virtual_queue_play_now_prepares_channel_for_both_action_aliases(self):
        sent = []
        async def command(action, **body):
            sent.append((action, body))
            self.assertEqual(self.server_bitrate, 256000)
            self.assertEqual(body['voice_channel_id'], 2)
            self.assertEqual(body['channel_bitrate_hint_bps'], 256000)
            return {'ok': True}
        self.prod['music_agent_command'] = command
        for alias in ('queue_virtual_action', 'virtual_queue_action'):
            with self.subTest(action=alias):
                result = await self.prod['enviar_controle_remoto'](
                    self.router, alias, guild_id=1, virtual_action='play_now')
                self.assertTrue(result['ok'])
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.edits, [256000])

    async def test_lost_control_ack_keeps_policy_until_authoritative_session_cleanup(self):
        async def command(action, **body): raise TimeoutError('ACK lost')
        self.prod['music_agent_command'] = command
        with self.assertRaises(TimeoutError):
            await self.prod['enviar_controle_remoto'](self.router, 'skip', guild_id=1)
        self.assertEqual(self.edits, [256000])
        self.assertEqual(self.server_bitrate, 256000)
        self.assertIsNotNone(self.saved)

    async def test_explicit_control_rejection_restores_unclaimed_idle_policy(self):
        async def command(action, **body): return {'ok': False, 'error': 'rejected'}
        self.prod['music_agent_command'] = command
        result = await self.prod['enviar_controle_remoto'](self.router, 'skip', guild_id=1)
        self.assertFalse(result['ok'])
        self.assertEqual(self.edits, [256000, 96000])
        self.assertIsNone(self.saved)

    async def _cross_channel_setup(self):
        channels, server, edits = {}, {2: 96000, 3: 64000}, []
        gate, entered = asyncio.Event(), asyncio.Event()
        active = {'count': 0, 'max': 0}
        def make_channel(channel_id, bitrate):
            async def edit(*, bitrate, reason):
                active['count'] += 1
                active['max'] = max(active['max'], active['count'])
                edits.append((channel_id, bitrate))
                try:
                    if channel_id == 2 and bitrate == 256000 and not gate.is_set():
                        entered.set()
                        await gate.wait()
                    server[channel_id] = bitrate
                    return make_channel(channel_id, bitrate)
                finally:
                    active['count'] -= 1
            return SimpleNamespace(id=channel_id, guild=self.guild, bitrate=bitrate, edit=edit)
        channels.update({cid: make_channel(cid, bitrate) for cid, bitrate in server.items()})
        self.guild.get_channel = channels.get
        self.router.bot.get_channel = channels.get
        async def fetch(cid): return make_channel(cid, server[cid])
        self.guild.fetch_channel = fetch
        return server, edits, gate, entered, active

    async def test_different_channels_serialize_restore_before_new_edit_and_keep_latest_record(self):
        server, edits, gate, entered, active = await self._cross_channel_setup()
        old = asyncio.create_task(self.prod['prepare_music_channel'](
            self.router, guild_id=1, voice_channel_id=2, generation=0, operation_id='old'))
        await entered.wait()
        self.generation = 1
        new = asyncio.create_task(self.prod['prepare_music_channel'](
            self.router, guild_id=1, voice_channel_id=3, generation=1, operation_id='new'))
        await asyncio.sleep(0)
        self.assertEqual(edits, [(2, 256000)])
        gate.set()
        self.assertEqual((await old)['state'], 'cancelled')
        self.assertEqual((await new)['state'], 'ready')
        self.assertEqual(edits, [(2, 256000), (2, 96000), (3, 256000)])
        self.assertEqual(active['max'], 1)
        self.assertEqual(server, {2: 96000, 3: 256000})
        self.assertEqual(self.saved['channel_id'], 3)
        self.assertEqual(self.state.auto_bitrate_channel_id, 3)
        await self.finish('old')
        self.assertEqual(self.saved['channel_id'], 3)
        await self.prod['finish_music_channel_preparation'](
            self.router, guild_id=1, voice_channel_id=3, operation_id='new', committed=True)

    async def test_new_channel_fetch_failure_preserves_previous_policy_and_skips_new_edit(self):
        server, edits, gate, entered, active = await self._cross_channel_setup()
        gate.set()
        await self.prepare('old')
        self.generation = 1
        async def unavailable(cid): raise RuntimeError('fresh channel unavailable')
        self.guild.fetch_channel = unavailable
        result = await self.prod['prepare_music_channel'](
            self.router, guild_id=1, voice_channel_id=3, generation=1, operation_id='new')
        self.assertEqual(result['state'], 'skipped')
        self.assertEqual(self.prod['channel_bitrate_hints'](result), {})
        self.assertEqual(edits, [(2, 256000)])
        self.assertEqual(self.saved['channel_id'], 2)
        await self.finish('old', committed=True)
        await self.prod['finish_music_channel_preparation'](
            self.router, guild_id=1, voice_channel_id=3, operation_id='new', committed=True)

    async def test_persisted_lost_response_is_restored_before_new_channel_policy(self):
        server, edits, gate, entered, active = await self._cross_channel_setup()
        gate.set()
        server[2] = 256000
        self.saved = {'channel_id': 2, 'original_bitrate': 96000, 'boosted_bitrate': 256000}
        self.assertIsNone(self.state.auto_bitrate_channel_id)
        result = await self.prod['prepare_music_channel'](
            self.router, guild_id=1, voice_channel_id=3, generation=0, operation_id='new')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(edits, [(2, 96000), (3, 256000)])
        self.assertEqual(self.saved['channel_id'], 3)
        await self.prod['finish_music_channel_preparation'](
            self.router, guild_id=1, voice_channel_id=3, operation_id='new', committed=True)

    async def test_late_started_effect_cannot_reboost_old_channel_after_explicit_new_target(self):
        server, edits, gate, entered, active = await self._cross_channel_setup()
        gate.set()
        await self.prod['prepare_music_channel'](
            self.router, guild_id=1, voice_channel_id=3, generation=0, operation_id='new')
        await self.prod['finish_music_channel_preparation'](
            self.router, guild_id=1, voice_channel_id=3, operation_id='new', committed=True)
        self.state.current = SimpleNamespace(queue_item_id='old')
        self.state.last_voice_channel_id = 2
        self.router._panel_key_for_track = lambda track: track.queue_item_id
        self.router._mark_voice_status_track_change = lambda state: self.events.append('mark_status')
        self.router._schedule_voice_status_track_sync = lambda *a, **k: self.events.append('schedule_status')
        with mock.patch.dict('sys.modules', {'cogs.musica.reproducao.inicio':
                SimpleNamespace(_music_channel_policy_lock=self.prod['_music_channel_policy_lock'])}):
            self.router._schedule_agent_playback_started_effects(1, 'old')
            await self.state.agent_side_effect_task
        self.assertEqual(edits, [(3, 256000)])
        self.assertEqual(self.saved['channel_id'], 3)
        self.assertIn('mark_status', self.events)


if __name__ == '__main__':
    unittest.main()
