"""Terminal text and idle STOP through production UI methods.

Discord component/interaction boundaries are explicit local doubles. The actual
renderer methods and request ownership call sites execute from production AST;
this does not claim validation against a live Discord API.
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


SOURCE = Path(__file__).resolve().parents[1] / 'cogs/musica/interface/componentes.py'
FINISHED = '-# Parece que acabou as músicas <:emoji_86:1564113432905654353>'


class Layout:
    def __init__(self, *, timeout=None, **kwargs):
        self.children = []
        self.timeout = timeout

    def add_item(self, item):
        self.children.append(item)
        return self

    def to_components(self):
        return [item.to_component_dict() for item in self.children]


class TextDisplay:
    def __init__(self, content):
        self.content = content

    def to_component_dict(self):
        return {'type': 10, 'content': self.content}


def load_ui(**overrides):
    tree = ast.parse(SOURCE.read_text())
    wanted = {
        'FinishedMusicMessageView': None,
        'MusicPlayerView': {'interaction_check', '_ignore_empty_stop',
                            '_music_agent_default_enabled', '_send_agent_control', 'stop'},
        'SearchSelect': {'callback'},
        'AddSongModal': {'on_submit'},
    }
    classes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in wanted:
            methods = wanted[node.name]
            if methods is not None:
                node.body = [item for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and item.name in methods]
                assert {item.name for item in node.body} == methods
            classes.append(node)
    assert {node.name for node in classes} == set(wanted)
    trace = SimpleNamespace(finish=mock.Mock(), timings={}, trace_id='request')
    loading = SimpleNamespace(start_background=mock.Mock(), finish=mock.AsyncMock())
    namespace = dict(
        asyncio=asyncio, contextlib=contextlib, time=time, logger=mock.Mock(),
        discord=SimpleNamespace(ui=SimpleNamespace(LayoutView=Layout, Select=Layout, Modal=Layout, TextDisplay=TextDisplay),
                                NotFound=type('NotFound', (Exception,), {})),
        config=SimpleNamespace(MUSIC_AGENT_ENABLED=True),
        has_music_activity=mock.Mock(return_value=False),
        begin_music_request=mock.Mock(return_value=object()), end_music_request=mock.Mock(),
        enviar_controle_remoto=mock.AsyncMock(return_value={'ok': True}),
        _panel_controls_invalid=mock.Mock(return_value=True),
        _send_interaction_notice=mock.AsyncMock(), _require_music_voice_interaction=mock.AsyncMock(return_value=False),
        _interaction_user_voice_channel=mock.Mock(return_value=SimpleNamespace(id=9)),
        MusicStartTrace=mock.Mock(return_value=trace), MusicLoadingReaction=mock.Mock(return_value=loading),
        finish_music_channel_preparation=mock.AsyncMock(),
        describe_url=lambda query: SimpleNamespace(resource_type='track', is_url=False),
        MusicExtractionError=type('MusicExtractionError', (Exception,), {}),
        **overrides,
    )
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *classes], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    return SimpleNamespace(**namespace), namespace


def interaction(*, done=False, custom_id='music:stop'):
    response = SimpleNamespace(send_message=mock.AsyncMock())
    response.done = done
    response.is_done = lambda: response.done

    async def defer(**kwargs):
        response.done = True
    response.defer = mock.AsyncMock(side_effect=defer)
    return SimpleNamespace(
        data={'custom_id': custom_id}, response=response,
        user=SimpleNamespace(id=7, display_name='Pessoa'),
        followup=SimpleNamespace(send=mock.AsyncMock()),
        guild=SimpleNamespace(id=1, get_channel=mock.Mock(return_value=SimpleNamespace(id=8))),
        channel=SimpleNamespace(id=8), client=SimpleNamespace(get_channel=mock.Mock()),
        message=SimpleNamespace(id=10), edit_original_response=mock.AsyncMock(),
    )


class FinishedViewTests(unittest.TestCase):
    def test_only_exact_top_level_text_display_and_no_container_or_controls(self):
        ui, _ = load_ui()
        view = ui.FinishedMusicMessageView()
        self.assertEqual(view.to_components(), [{'type': 10, 'content': FINISHED}])
        self.assertEqual(len(view.children), 1)
        self.assertIsNone(view.timeout)


class IdleStopInteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ui, self.ns = load_ui()
        self.view = self.ui.MusicPlayerView()
        self.view.router = SimpleNamespace(
            get_state=mock.Mock(return_value=SimpleNamespace(history=[])),
            request_stop=mock.AsyncMock(return_value=(False, '')),
            music_worker_only_enabled=lambda: True)
        self.view.guild_id = 1
        self.view._ack = mock.AsyncMock()

    async def test_old_idle_stop_click_is_acknowledged_before_expired_or_voice_checks(self):
        item = interaction()
        self.assertFalse(await self.view.interaction_check(item))
        item.response.defer.assert_awaited_once_with(thinking=False)
        self.ns['_panel_controls_invalid'].assert_not_called()
        self.ns['_require_music_voice_interaction'].assert_not_called()
        self.ns['_send_interaction_notice'].assert_not_called()
        item.followup.send.assert_not_awaited()

    async def test_idle_callback_does_not_send_remote_stop_or_reply(self):
        item = interaction()
        await self.view.stop(item)
        item.response.defer.assert_awaited_once_with(thinking=False)
        self.ns['enviar_controle_remoto'].assert_not_awaited()
        self.view.router.request_stop.assert_not_awaited()
        self.view._ack.assert_not_awaited()

    async def test_already_acknowledged_idle_click_has_no_extra_response(self):
        item = interaction(done=True)
        await self.view.stop(item)
        item.response.defer.assert_not_awaited()
        item.response.send_message.assert_not_awaited()
        item.followup.send.assert_not_awaited()

    async def test_music_can_end_during_defer_without_creating_a_thinking_message(self):
        self.ns['has_music_activity'].side_effect = [True, False]
        item = interaction()
        await self.view.stop(item)
        item.response.defer.assert_awaited_once_with(thinking=False)
        self.ns['enviar_controle_remoto'].assert_not_awaited()
        self.view._ack.assert_not_awaited()

    async def test_worker_ignored_stop_does_not_produce_success_followup(self):
        self.ns['has_music_activity'].return_value = True
        self.ns['enviar_controle_remoto'].return_value = {'ok': True, 'ignored': True}
        item = interaction()
        self.assertTrue(await self.view._send_agent_control(item, 'stop', 'encerrado'))
        item.response.defer.assert_awaited_once_with(thinking=False)
        self.assertFalse(self.ns['enviar_controle_remoto'].call_args.kwargs['create_panel'])
        self.view._ack.assert_not_awaited()

    async def test_active_stop_preserves_success_confirmation_and_cannot_create_panel(self):
        self.ns['has_music_activity'].return_value = True
        item = interaction()
        await self.view.stop(item)
        self.ns['enviar_controle_remoto'].assert_awaited_once()
        self.assertFalse(self.ns['enviar_controle_remoto'].call_args.kwargs['create_panel'])
        self.view._ack.assert_awaited_once_with(item, '`⏹️` Player encerrado e desconectado.')

    async def test_active_button_still_requires_existing_voice_authorization(self):
        self.ns['has_music_activity'].return_value = True
        self.ns['_panel_controls_invalid'].return_value = False
        self.ns['_require_music_voice_interaction'].return_value = True
        item = interaction()
        self.assertTrue(await self.view.interaction_check(item))
        self.ns['_require_music_voice_interaction'].assert_awaited_once_with(
            item, self.view.router, 1, check_worker=False)

    async def test_legacy_empty_result_is_not_acknowledged_after_a_race(self):
        self.ns['has_music_activity'].return_value = True
        self.view.router.music_worker_only_enabled = lambda: False
        await self.view.stop(interaction())
        self.view.router.request_stop.assert_awaited_once()
        self.view._ack.assert_not_awaited()


class PendingUIRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ui, self.ns = load_ui()
        self.router = SimpleNamespace(
            current_music_operation_generation=mock.Mock(return_value=3),
            get_state=mock.Mock(return_value=SimpleNamespace(last_text_channel_id=8)),
            music_worker_only_enabled=lambda: True,
            ensure_music_worker_available=mock.AsyncMock(),
        )

    def select(self):
        view = self.ui.SearchSelect()
        view.router, view.guild_id, view.requester_id = self.router, 1, 7
        view.search_timing_ms = {}
        view.voice_channel_id, view.text_channel_id = 9, 8
        return view

    async def wait_and_cancel(self, coroutine, gate):
        task = asyncio.create_task(coroutine)
        await asyncio.wait_for(gate.wait(), timeout=1)
        self.ns['begin_music_request'].assert_called_once_with(self.router, 1)
        self.ns['end_music_request'].assert_not_called()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.ns['end_music_request'].assert_called_once_with(
            self.router, 1, self.ns['begin_music_request'].return_value)

    async def test_selection_owns_pending_work_during_worker_validation_and_releases_on_cancel(self):
        gate = asyncio.Event()
        async def validate(*args, **kwargs):
            gate.set()
            await asyncio.Future()
        self.ns['_require_music_voice_interaction'].side_effect = validate
        await self.wait_and_cancel(self.select().callback(interaction()), gate)

    async def test_selection_defer_cancellation_also_releases_ownership(self):
        gate, item = asyncio.Event(), interaction()
        async def defer(**kwargs):
            gate.set()
            await asyncio.Future()
        item.response.defer.side_effect = defer
        await self.wait_and_cancel(self.select().callback(item), gate)
        self.ns['_require_music_voice_interaction'].assert_not_awaited()

    async def test_selection_without_voice_does_not_claim_a_pending_play(self):
        self.ns['_interaction_user_voice_channel'].return_value = None
        await self.select().callback(interaction())
        self.ns['begin_music_request'].assert_not_called()
        self.ns['end_music_request'].assert_not_called()

    def modal(self):
        modal = self.ui.AddSongModal()
        modal.router, modal.guild_id = self.router, 1
        modal.voice_channel_id, modal.text_channel_id = 9, 8
        modal.query = SimpleNamespace(value='song query')
        self.ns['_require_music_voice_interaction'].return_value = True
        return modal

    async def test_modal_owns_pending_work_while_worker_prepares_and_releases_on_cancel(self):
        gate = asyncio.Event()
        async def worker():
            gate.set()
            await asyncio.Future()
        self.router.ensure_music_worker_available.side_effect = worker
        await self.wait_and_cancel(self.modal().on_submit(interaction()), gate)

    async def test_modal_defer_cancellation_releases_ownership_before_metadata(self):
        gate, item = asyncio.Event(), interaction()
        async def defer(**kwargs):
            gate.set()
            await asyncio.Future()
        item.response.defer.side_effect = defer
        await self.wait_and_cancel(self.modal().on_submit(item), gate)
        self.router.ensure_music_worker_available.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
