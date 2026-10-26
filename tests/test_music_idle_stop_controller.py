"""STOP admission and remote controls with explicit Discord/HTTP boundaries.

Production command and transport functions run against real domain state and
activity helpers. No voice connection, Discord request or worker is launched.
"""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.reproducao.atividade import (
    begin_music_request, claim_music_stop, end_music_request,
    has_music_activity, owns_music_stop, release_music_stop,
)


ROOT = Path(__file__).resolve().parents[1] / 'cogs/musica'


def load_controls():
    shared = dict(
        asyncio=asyncio, uuid=uuid, MusicTrack=MusicTrack,
        logger=logging.getLogger(__name__),
        config=SimpleNamespace(MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS=12),
        has_music_activity=has_music_activity, claim_music_stop=claim_music_stop,
        owns_music_stop=owns_music_stop, release_music_stop=release_music_stop,
        music_agent_command=mock.AsyncMock(),
        prepare_music_channel=mock.AsyncMock(),
        finish_music_channel_preparation=mock.AsyncMock(),
        channel_bitrate_hints=lambda value: {},
    )
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    source = ROOT / 'reproducao/controle_remoto.py'
    tree = ast.parse(source.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == 'enviar_controle_remoto')
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[])),
                 str(source), 'exec'), shared)

    classes = []
    for relative, original_name, class_name, wanted in (
        ('comandos/base.py', 'BaseComandosMusica', 'Base',
         {'_ensure_music_action_voice', '_send_music_agent_control'}),
        ('comandos/controle.py', 'FluxoControle', 'Control', {'_run_stop'}),
    ):
        source = ROOT / relative
        original = next(node for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.ClassDef) and node.name == original_name)
        methods = [node for node in original.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
        assert {node.name for node in methods} == wanted
        classes.append(ast.ClassDef(name=class_name, bases=[], keywords=[], body=methods, decorator_list=[]))
    code = ast.Module(body=[future, *classes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), 'controller_stop_boundary', 'exec'), shared)
    flow = type('Flow', (shared['Control'], shared['Base']), {})
    return flow, shared


class Router:
    def __init__(self):
        self.state = MusicGuildState()
        self.cancel_pending_music_operations = mock.Mock(side_effect=self.cancel)
        self.ensure_music_worker_available = mock.AsyncMock(return_value=SimpleNamespace(available=True))
        self.sync_music_agent_state = mock.AsyncMock(side_effect=self.sync)
        self.request_stop = mock.AsyncMock(return_value=(True, 'stopped locally'))

    def get_state(self, guild_id):
        return self.state

    def current_music_operation_generation(self, guild_id):
        return self.state.music_operation_generation

    def music_worker_only_enabled(self):
        return True

    def cancel(self, guild_id, **kwargs):
        self.state.music_operation_generation += 1

    async def sync(self, guild_id, track, remote, **kwargs):
        if remote.get('status') == 'idle':
            self.state.current = None
            self.state.current_status = 'idle'


class IdleStopControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Flow, self.namespace = load_controls()
        self.flow = Flow()
        self.router = self.flow.router = Router()
        self.ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, voice_client=None),
            channel=SimpleNamespace(id=5),
            author=SimpleNamespace(id=2, display_name='person', voice=SimpleNamespace(channel=SimpleNamespace(id=9))),
        )
        self.flow._voice_channel_from_ctx = mock.AsyncMock(return_value=SimpleNamespace(id=9))
        self.flow._music_agent_default_enabled = lambda: True
        self.flow._reply = mock.AsyncMock()
        self.flow._music_error_message = lambda error: 'control failed'
        self.worker = self.namespace['music_agent_command']
        self.worker.return_value = {'ok': True, 'state': {'status': 'idle'}}
        self.remote = self.namespace['enviar_controle_remoto']

    def active(self, *, paused=False):
        self.router.state.current = MusicTrack('Current', '', 1, duration=180, queue_item_id='current')
        self.router.state.current_status = 'paused' if paused else 'playing'
        self.router.state.paused = paused

    def assert_no_effects(self):
        self.worker.assert_not_called()
        self.router.cancel_pending_music_operations.assert_not_called()
        self.router.sync_music_agent_state.assert_not_called()
        self.router.request_stop.assert_not_called()
        self.flow._reply.assert_not_called()

    async def test_empty_stop_is_silent_before_voice_and_worker_admission(self):
        self.flow._voice_channel_from_ctx.return_value = None
        await self.flow._run_stop(self.ctx)
        self.flow._voice_channel_from_ctx.assert_not_called()
        self.router.ensure_music_worker_available.assert_not_called()
        self.assert_no_effects()

    async def test_history_session_and_voice_without_music_do_not_make_stop_active(self):
        self.router.state.music_session_active = True
        self.router.state.history.append(MusicTrack('Old', '', 1))
        self.ctx.guild.voice_client = SimpleNamespace(channel=SimpleNamespace(id=9), is_playing=lambda: True)
        self.router.state.current_source = None
        await self.flow._run_stop(self.ctx)
        self.assert_no_effects()
        self.assertEqual(len(self.router.state.history), 1)

    async def test_paused_music_stops_once_and_sync_cannot_create_a_panel(self):
        self.active(paused=True)
        await self.flow._run_stop(self.ctx)
        self.worker.assert_awaited_once()
        self.assertEqual(self.worker.call_args.args[0], 'stop')
        self.assertNotIn('_stop_owner', self.worker.call_args.kwargs)
        self.router.cancel_pending_music_operations.assert_called_once_with(1, reason='agent_stop')
        self.assertFalse(self.router.sync_music_agent_state.call_args.kwargs['create_panel'])
        self.flow._reply.assert_awaited_once()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_last_track_finishing_during_healthcheck_suppresses_offline_notice(self):
        self.active()
        async def finishes():
            self.router.state.current = None
            self.router.state.current_status = 'idle'
            return SimpleNamespace(available=False)
        self.router.ensure_music_worker_available.side_effect = finishes
        await self.flow._run_stop(self.ctx)
        self.assert_no_effects()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_last_track_finishing_during_voice_lookup_suppresses_voice_notice(self):
        self.active()
        async def finishes(_ctx):
            self.router.state.current = None
            self.router.state.current_status = 'idle'
            return None
        self.flow._voice_channel_from_ctx.side_effect = finishes
        await self.flow._run_stop(self.ctx)
        self.router.ensure_music_worker_available.assert_not_called()
        self.assert_no_effects()

    async def test_active_permission_failure_still_replies_and_releases_claim(self):
        self.active()
        self.flow._voice_channel_from_ctx.return_value = None
        await self.flow._run_stop(self.ctx)
        self.flow._reply.assert_awaited_once_with(self.ctx, 'Entre em um canal de voz primeiro.')
        self.worker.assert_not_called()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_active_worker_unavailable_preserves_notice_without_sending_stop(self):
        self.active()
        self.router.ensure_music_worker_available.return_value = SimpleNamespace(
            available=False, reason='offline', message='Worker indisponível',
        )
        await self.flow._run_stop(self.ctx)
        self.flow._reply.assert_awaited_once_with(self.ctx, 'Worker indisponível')
        self.worker.assert_not_called()
        self.router.cancel_pending_music_operations.assert_not_called()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_active_other_channel_preserves_permission_notice(self):
        self.active()
        self.ctx.guild.voice_client = SimpleNamespace(channel=SimpleNamespace(id=99))
        await self.flow._run_stop(self.ctx)
        self.flow._reply.assert_awaited_once_with(
            self.ctx, 'Entre no mesmo canal de voz do bot para usar isso.',
        )
        self.worker.assert_not_called()
        self.router.cancel_pending_music_operations.assert_not_called()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_music_finishing_during_successful_healthcheck_is_silent(self):
        self.active()
        self.ctx.guild.voice_client = SimpleNamespace(channel=SimpleNamespace(id=99))
        async def finishes():
            self.router.state.current = None
            self.router.state.current_status = 'idle'
            return SimpleNamespace(available=True)
        self.router.ensure_music_worker_available.side_effect = finishes
        await self.flow._run_stop(self.ctx)
        self.assert_no_effects()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_authoritative_ignored_result_does_not_sync_or_confirm(self):
        self.active()
        self.worker.return_value = {'ok': True, 'ignored': True, 'state': {'status': 'idle'}}
        await self.flow._run_stop(self.ctx)
        self.worker.assert_awaited_once()
        self.router.sync_music_agent_state.assert_not_called()
        self.flow._reply.assert_not_called()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_two_concurrent_commands_share_one_stop_admission(self):
        self.active()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_worker(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'ok': True, 'state': {'status': 'idle'}}
        self.worker.side_effect = slow_worker
        first = asyncio.create_task(self.flow._run_stop(self.ctx))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await self.flow._run_stop(self.ctx)
        release.set()
        await first
        self.worker.assert_awaited_once()
        self.router.ensure_music_worker_available.assert_awaited_once()
        self.flow._reply.assert_awaited_once()

    async def test_direct_remote_calls_also_share_one_stop_owner(self):
        self.active()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_worker(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'ok': True, 'state': {'status': 'idle'}}
        self.worker.side_effect = slow_worker
        first = asyncio.create_task(self.remote(self.router, 'stop', guild_id=1))
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = await self.remote(self.router, 'stop', guild_id=1)
        self.assertTrue(second['ignored'])
        release.set()
        await first
        self.worker.assert_awaited_once()
        self.router.cancel_pending_music_operations.assert_called_once()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_queue_virtual_playlist_and_starting_work_remain_stoppable(self):
        for kind in ('queue', 'virtual', 'starting'):
            with self.subTest(kind=kind):
                self.router.state = MusicGuildState()
                self.worker.reset_mock()
                if kind == 'queue':
                    self.router.state.queue.put_nowait(MusicTrack('Pending', '', 1))
                elif kind == 'virtual':
                    self.router.state.current_backend = 'agent'
                    self.router.state.agent_virtual_playlist = {
                        'active': True, 'provider': 'test', 'source_url': 'https://example.invalid/list',
                        'next_offset': 0, 'remaining': 10,
                    }
                else:
                    self.router.state.current_status = 'starting'
                await self.flow._run_stop(self.ctx)
                self.worker.assert_awaited_once()
                self.assertIsNone(self.router.state.music_stop_owner)

    async def test_cancelling_transport_wait_releases_guard_without_a_reply(self):
        self.active()
        entered = asyncio.Event()
        async def slow_worker(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.worker.side_effect = slow_worker
        task = asyncio.create_task(self.flow._run_stop(self.ctx))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.router.state.music_stop_owner)
        self.flow._reply.assert_not_called()
        self.worker.side_effect = None
        await self.flow._run_stop(self.ctx)
        self.assertEqual(self.worker.await_count, 2)

    async def test_failed_transport_releases_guard_for_retry(self):
        self.active()
        self.worker.side_effect = RuntimeError('private transport detail')
        await self.flow._run_stop(self.ctx)
        self.assertIsNone(self.router.state.music_stop_owner)
        self.worker.side_effect = None
        await self.flow._run_stop(self.ctx)
        self.assertEqual(self.worker.await_count, 2)
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_direct_idle_remote_control_is_noop_without_cancellation_or_rpc(self):
        result = await self.remote(self.router, 'stop', guild_id=1)
        self.assertEqual(result, {'ok': True, 'ignored': True})
        self.assert_no_effects()

    async def test_invalid_internal_owner_cannot_borrow_another_stop(self):
        self.active()
        owner = claim_music_stop(self.router, 1)
        result = await self.remote(self.router, 'stop', guild_id=1, _stop_owner=object())
        self.assertTrue(result['ignored'])
        self.assertTrue(owns_music_stop(self.router, 1, owner))
        self.assert_no_effects()
        release_music_stop(self.router, 1, owner)

    async def test_direct_call_claims_and_releases_its_own_stop(self):
        self.active()
        await self.remote(self.router, 'stop', guild_id=1, create_panel=True)
        self.assertFalse(self.router.sync_music_agent_state.call_args.kwargs['create_panel'])
        self.assertIsNone(self.router.state.music_stop_owner)
        self.assertNotIn('_stop_owner', self.worker.call_args.kwargs)

    async def test_pre_dispatch_local_request_can_be_cancelled_without_worker_music(self):
        owner = begin_music_request(self.router, 1)
        try:
            self.worker.return_value = {'ok': True, 'ignored': True}
            await self.flow._run_stop(self.ctx)
            self.router.cancel_pending_music_operations.assert_called_once()
            self.worker.assert_awaited_once()
            self.flow._reply.assert_not_called()
        finally:
            end_music_request(self.router, 1, owner)

    async def test_legacy_fallback_ignored_result_has_no_empty_reply(self):
        self.active()
        self.flow._music_agent_default_enabled = lambda: False
        self.router.request_stop.return_value = (False, '')
        await self.flow._run_stop(self.ctx)
        self.router.request_stop.assert_awaited_once()
        self.assertIsNotNone(self.router.request_stop.call_args.kwargs['_stop_owner'])
        self.flow._reply.assert_not_called()
        self.worker.assert_not_called()
        self.assertIsNone(self.router.state.music_stop_owner)

    async def test_explicit_disconnect_keeps_its_existing_semantics_when_idle(self):
        result = await self.remote(self.router, 'disconnect', guild_id=1)
        self.assertTrue(result['ok'])
        self.worker.assert_awaited_once()
        self.router.cancel_pending_music_operations.assert_not_called()


if __name__ == '__main__':
    unittest.main()
