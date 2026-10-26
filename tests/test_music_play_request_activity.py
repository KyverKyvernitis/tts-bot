"""Pending play requests through production command methods and memory helpers.

Discord, metadata and transport are explicit doubles. The command bodies execute
from their production AST; no live network, Discord or voice call is exercised.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest import mock
import uuid

from cogs.musica.reproducao.atividade import begin_music_request, end_music_request, has_music_activity

SOURCE = Path(__file__).resolve().parents[1] / 'cogs/musica/comandos/tocar.py'


class Router:
    def __init__(self):
        self.state = SimpleNamespace(music_operation_generation=3, current=None,
                                     current_status='idle', music_pending_requests=set(),
                                     music_request_serial=0)
        self.ensure_music_worker_available = mock.AsyncMock(return_value=SimpleNamespace(available=True))
        self.extractor = SimpleNamespace(extract=mock.AsyncMock())

    def get_state(self, guild_id):
        assert guild_id == 1
        return self.state

    def current_music_operation_generation(self, guild_id):
        return self.get_state(guild_id).music_operation_generation

    def music_worker_only_enabled(self):
        return True


def load_command():
    tree = ast.parse(SOURCE.read_text())
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'FluxoTocar')
    wanted = {'_run_play', '_run_play_discord_attachment', '_run_owned_discord_attachment'}
    methods = [node for node in klass.body if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted]
    assert {node.name for node in methods} == wanted
    klass = ast.ClassDef(name='Command', bases=[], keywords=[], body=methods, decorator_list=[])
    trace = SimpleNamespace(timings={}, context={}, trace_id='trace', finish=mock.Mock(),
                            dispatch=mock.Mock(), result=mock.Mock(), search_results=mock.Mock())
    loading = SimpleNamespace(start_background=mock.Mock(), finish=mock.AsyncMock())
    payload = {'title': 'Short', 'duration': 5, 'webpage_url': 'https://example.invalid/audio',
               'attachment_ref': {'attachment_id': 10}}
    track = SimpleNamespace(title='Short', duration=5, short_title='Short', duration_label='0:05')
    selected = SimpleNamespace(status='audio', anexo=SimpleNamespace(url='https://example.invalid/audio'))
    async def payload_thread(function, *args, **kwargs):
        # Payload serialization is an external scheduling boundary for these
        # ownership checks; no real thread is needed to serialize the double.
        return function(*args, **kwargs)
    command_asyncio = SimpleNamespace(CancelledError=asyncio.CancelledError,
                                     TimeoutError=asyncio.TimeoutError,
                                     create_task=asyncio.create_task, to_thread=payload_thread)
    namespace = dict(
        asyncio=command_asyncio, contextlib=contextlib, time=time, uuid=uuid, logger=mock.Mock(),
        begin_music_request=begin_music_request, end_music_request=end_music_request,
        MusicStartTrace=mock.Mock(return_value=trace), MusicLoadingReaction=mock.Mock(return_value=loading),
        config=SimpleNamespace(MUSIC_AGENT_ENABLED=True),
        discord=SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: None)),
        midia_respondida=mock.AsyncMock(return_value=selected),
        metadados_midia=mock.Mock(return_value=payload), faixa_do_payload=mock.Mock(return_value=track),
        music_agent_command=mock.AsyncMock(return_value={'ok': True, 'track': payload, 'state': {'status': 'idle'}}),
        prepare_music_channel=mock.AsyncMock(return_value={'state': 'ready'}),
        finish_music_channel_preparation=mock.AsyncMock(), channel_bitrate_hints=lambda _: {},
        capture_music_worker_validation=mock.Mock(return_value={}),
        schedule_deferred_learning=mock.Mock(), schedule_playlist_refill_from_result=mock.Mock(return_value=False),
        PlayInputKind=SimpleNamespace(SEARCH=object()),
        MusicExtractionError=type('MusicExtractionError', (Exception,), {}),
        agente_nao_pronto_transitorio=lambda _: False,
    )
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, klass], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    command = namespace['Command']()
    command.router = Router()
    command._voice_channel_from_ctx = mock.AsyncMock(return_value=SimpleNamespace(id=9))
    command._reply = mock.AsyncMock()
    command._sync_music_agent_panel = mock.AsyncMock()
    command._music_agent_play_message = mock.Mock(return_value='playing')
    command._music_agent_confirmed_playing = mock.Mock(return_value=True)
    command._music_agent_default_enabled = lambda: True
    command._music_error_message = lambda exc: type(exc).__name__
    command._query_profile = lambda _: SimpleNamespace(resource_type='track', is_url=False,
                                                       is_youtube=False, is_metadata_only=True, platform='spotify')
    command._play_input_kind = lambda _: object()
    command._is_youtube_text_search = lambda _: False
    command._music_agent_query_for_track = lambda *_: 'query'
    command._music_agent_tracks_payload = lambda *_args, **_kwargs: [payload]
    command.router.extractor.extract.return_value = SimpleNamespace(tracks=[track], is_playlist=False)
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=8),
                          author=SimpleNamespace(id=7, display_name='Person'),
                          message=SimpleNamespace(id=20, reference=None))
    return command, ctx, namespace, selected


class PlayRequestActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.command, self.ctx, self.ns, self.selected = load_command()
        self.router = self.command.router

    def assert_released(self):
        self.assertFalse(self.router.state.music_pending_requests)
        self.assertFalse(has_music_activity(self.router, 1))

    async def paused_request(self, coroutine, boundary):
        gate = asyncio.Event()
        async def waiting(*args, **kwargs):
            gate.set()
            await asyncio.Event().wait()
        boundary.side_effect = waiting
        task = asyncio.create_task(coroutine)
        await asyncio.wait_for(gate.wait(), 1)
        self.assertTrue(has_music_activity(self.router, 1))
        self.assertEqual(len(self.router.state.music_pending_requests), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()

    async def test_attachment_fetch_is_pending_before_metadata_await_and_cancel_releases(self):
        await self.paused_request(self.command._run_play_discord_attachment(self.ctx),
                                  self.ns['midia_respondida'])
        self.ns['music_agent_command'].assert_not_awaited()

    async def test_attachment_voice_admission_is_pending_and_cancel_releases(self):
        await self.paused_request(self.command._run_play_discord_attachment(self.ctx, self.selected),
                                  self.command._voice_channel_from_ctx)

    async def test_attachment_probe_is_pending_and_cancel_releases(self):
        await self.paused_request(self.command._run_play_discord_attachment(self.ctx, self.selected),
                                  self.ns['music_agent_command'])

    async def test_attachment_failure_releases_owner_without_panel(self):
        self.ns['music_agent_command'].side_effect = ValueError('probe rejected')
        await self.command._run_play_discord_attachment(self.ctx, self.selected)
        self.assert_released()
        self.command._sync_music_agent_panel.assert_not_awaited()

    async def test_attachment_metadata_failure_before_inner_try_releases_owner(self):
        self.ns['midia_respondida'].side_effect = RuntimeError('message unavailable')
        with self.assertRaises(RuntimeError):
            await self.command._run_play_discord_attachment(self.ctx)
        self.assert_released()

    async def test_attachment_authoritative_ack_releases_before_reply_and_panel(self):
        async def verify_release(*args, **kwargs):
            self.assert_released()
        self.command._reply.side_effect = verify_release
        self.command._sync_music_agent_panel.side_effect = verify_release
        await self.command._run_play_discord_attachment(self.ctx, self.selected)
        self.command._sync_music_agent_panel.assert_awaited_once()
        self.assert_released()

    async def test_attachment_stop_during_message_fetch_cannot_start_late(self):
        async def stopped(*args):
            self.assertTrue(has_music_activity(self.router, 1))
            self.router.state.music_operation_generation += 1
            return self.selected
        self.ns['midia_respondida'].side_effect = stopped
        await self.command._run_play_discord_attachment(self.ctx)
        self.ns['music_agent_command'].assert_not_awaited()
        self.command._sync_music_agent_panel.assert_not_awaited()
        self.assert_released()

    async def test_attachment_stop_during_ack_cannot_sync_stale_panel(self):
        result = self.ns['music_agent_command'].return_value
        async def stopped(*args, **kwargs):
            self.router.state.music_operation_generation += 1
            return result
        self.ns['music_agent_command'].side_effect = stopped
        await self.command._run_play_discord_attachment(self.ctx, self.selected)
        self.command._reply.assert_not_awaited()
        self.command._sync_music_agent_panel.assert_not_awaited()
        self.assert_released()

    async def test_attachment_stop_during_voice_admission_cannot_start_late(self):
        async def stopped(*args):
            self.assertTrue(has_music_activity(self.router, 1))
            self.router.state.music_operation_generation += 1
            return SimpleNamespace(id=9)
        self.command._voice_channel_from_ctx.side_effect = stopped
        await self.command._run_play_discord_attachment(self.ctx, self.selected)
        self.ns['prepare_music_channel'].assert_not_awaited()
        self.ns['music_agent_command'].assert_not_awaited()
        self.assert_released()

    async def test_main_metadata_window_is_activity_and_cancel_releases(self):
        await self.paused_request(self.command._run_play(self.ctx, 'a song'),
                                  self.router.extractor.extract)
        self.ns['music_agent_command'].assert_not_awaited()

    async def test_main_worker_admission_window_is_activity_and_cancel_releases(self):
        await self.paused_request(self.command._run_play(self.ctx, 'a song'),
                                  self.router.ensure_music_worker_available)

    async def test_main_stopped_metadata_cannot_dispatch_late(self):
        batch = self.router.extractor.extract.return_value
        async def stopped(*args, **kwargs):
            self.assertTrue(has_music_activity(self.router, 1))
            self.router.state.music_operation_generation += 1
            return batch
        self.router.extractor.extract.side_effect = stopped
        await self.command._run_play(self.ctx, 'a song')
        self.ns['music_agent_command'].assert_not_awaited()
        self.command._reply.assert_not_awaited()
        self.assert_released()

    async def test_main_ack_releases_owner_before_reply_and_panel(self):
        async def verify_release(*args, **kwargs):
            self.assert_released()
        self.command._reply.side_effect = verify_release
        self.command._sync_music_agent_panel.side_effect = verify_release
        await self.command._run_play(self.ctx, 'a song')
        self.ns['music_agent_command'].assert_awaited_once()
        self.command._sync_music_agent_panel.assert_awaited_once()
        self.assert_released()

    async def test_main_stop_during_ack_suppresses_stale_reply_and_panel(self):
        result = self.ns['music_agent_command'].return_value
        async def stopped(*args, **kwargs):
            self.router.state.music_operation_generation += 1
            return result
        self.ns['music_agent_command'].side_effect = stopped
        await self.command._run_play(self.ctx, 'a song')
        self.command._reply.assert_not_awaited()
        self.command._sync_music_agent_panel.assert_not_awaited()
        self.assert_released()


if __name__ == '__main__':
    unittest.main()
