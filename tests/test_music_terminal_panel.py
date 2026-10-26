"""Terminal player edits and idle legacy STOP through production methods.

The router and final LayoutView class execute from their real AST. Discord
transport and component bindings are explicit local doubles; these checks do
not replace the manual Discord rendering check described in the patch guide.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import time
import unittest
from unittest import mock

from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import MusicTrack


ROOT = Path(__file__).resolve().parents[1]
ROUTER_SOURCE = ROOT / 'cogs/musica/legado/roteador_audio.py'
UI_SOURCE = ROOT / 'cogs/musica/interface/componentes.py'
EXACT = '-# Parece que acabou as músicas <:emoji_86:1564113432905654353>'


class NotFound(Exception):
    pass


class TextDisplay:
    def __init__(self, content, **kwargs):
        self.content = content

    def to_component_dict(self):
        return {'type': 10, 'content': self.content}


class LayoutView:
    def __init__(self, *, timeout=None):
        self.timeout = timeout
        self.children = []

    def add_item(self, item):
        self.children.append(item)
        return self

    def to_components(self):
        return [item.to_component_dict() for item in self.children]


DISCORD = SimpleNamespace(
    NotFound=NotFound,
    ui=SimpleNamespace(LayoutView=LayoutView, TextDisplay=TextDisplay),
    Color=SimpleNamespace(dark_grey=lambda: 0),
)


def load_finished_view():
    tree = ast.parse(UI_SOURCE.read_text())
    klass = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == 'FinishedMusicMessageView')
    # Preserve any literal constant used by the real view without importing
    # the rest of Discord's UI and command module.
    constants = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                 and isinstance(getattr(node, 'value', None), ast.Constant)]
    namespace = {'discord': DISCORD}
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    code = ast.fix_missing_locations(ast.Module(body=[future, *constants, klass], type_ignores=[]))
    exec(compile(code, str(UI_SOURCE), 'exec'), namespace)
    return namespace['FinishedMusicMessageView']


def load_router():
    tree = ast.parse(ROUTER_SOURCE.read_text())
    wanted = {'update_panel', 'stop', 'request_stop'}
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and any(isinstance(method, ast.AsyncFunctionDef) and method.name == 'update_panel'
                            for method in node.body))
    methods = [node for node in original.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    assert {node.name for node in methods} == wanted
    namespace = {
        '__package__': 'cogs.musica.legado', 'discord': DISCORD,
        'asyncio': asyncio, 'contextlib': contextlib, 'time': time,
        'logger': logging.getLogger('tests.music.terminal_panel'),
        'config': SimpleNamespace(MUSIC_PANEL_REPOST_ON_TRACK_CHANGE=True),
    }
    activity = importlib.import_module('cogs.musica.reproducao.atividade')
    namespace.update({name: value for name, value in vars(activity).items()
                      if not name.startswith('__')})
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    klass = ast.ClassDef(name='Router', bases=[], keywords=[], body=methods, decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[future, klass], type_ignores=[]))
    exec(compile(code, str(ROUTER_SOURCE), 'exec'), namespace)
    return namespace['Router']


class Message:
    def __init__(self, identity=100):
        self.id = identity
        self.edits = []
        self.error = None
        self.on_edit = None
        self.delete = mock.AsyncMock()

    async def edit(self, **payload):
        self.edits.append(payload)
        if self.on_edit is not None:
            callback, self.on_edit = self.on_edit, None
            await callback()
        if self.error is not None:
            raise self.error
        return self


class PlayerView:
    def __init__(self, router, guild_id):
        self.current = router.get_state(guild_id).current
        self.kind = 'player'


class StaticView:
    def __init__(self, text, **kwargs):
        self.text = text


def track(identity='new'):
    return MusicTrack('Track ' + identity, 'https://example.invalid/' + identity,
                      1, queue_item_id=identity, duration=180)


class TerminalPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.Router = load_router()
        self.router = self.Router()
        self.state = MusicGuildState()
        self.state.last_text_channel_id = 9
        self.state.idle_reason = 'queue_finished'
        self.state.current_status = 'idle'
        self.state.current_backend = 'agent'
        self.state.panel_finished = False
        self.state.panel_finished_message_id = 0
        self.state.panel_finished_generation = -1
        self.message = self.state.now_message = Message()
        self.state.panel_track_key = 'old'
        self.channel = SimpleNamespace(send=mock.AsyncMock(return_value=Message(200)))
        self.guild = SimpleNamespace(get_channel=lambda identity: self.channel,
                                     voice_client=None)
        self.router.bot = SimpleNamespace(get_guild=lambda identity: self.guild,
                                          get_channel=lambda identity: self.channel)
        self.router.get_state = lambda gid: self.state
        self.router._has_pending_track = lambda state: bool(state.forward_queue or state.queue.qsize())
        self.router._reactivate_panel_controls_now = mock.Mock()
        self.router.pending_vote_summary = mock.Mock(return_value=[])
        self.router._panel_key_for_track = lambda value: value.queue_item_id if value else None
        self.router.current_music_operation_generation = lambda gid: self.state.music_operation_generation
        self.finished_view = load_finished_view()
        module = ModuleType('cogs.musica.interface.componentes')
        module.MusicPlayerView = PlayerView
        module.FinishedMusicMessageView = self.finished_view
        module.StaticMusicMessageView = StaticView
        module.build_player_embeds = lambda state: []
        self.boundary = mock.patch.dict(sys.modules, {'cogs.musica.interface.componentes': module})
        self.boundary.start()
        self.addCleanup(self.boundary.stop)

    async def update(self, **options):
        await self.router.update_panel(1, **options)

    def assert_no_new_message(self):
        self.channel.send.assert_not_awaited()
        self.message.delete.assert_not_awaited()

    async def test_natural_finish_edits_same_message_to_exact_top_level_text_only(self):
        await self.update(create=True, repost=True)
        self.assertIs(self.state.now_message, self.message)
        self.assertEqual(len(self.message.edits), 1)
        payload = self.message.edits[0]
        self.assertIsNone(payload['content'])
        self.assertEqual(payload['embeds'], [])
        self.assertEqual(payload['attachments'], [])
        self.assertEqual(payload['view'].to_components(), [{'type': 10, 'content': EXACT}])
        self.assertEqual(payload['view'].children[0].content, EXACT)
        self.assertTrue(self.state.panel_finished)
        self.assert_no_new_message()

    async def test_missing_message_marks_finish_without_publishing_a_replacement(self):
        self.state.now_message = None
        await self.update(create=True, repost=True)
        self.assertTrue(self.state.panel_finished)
        self.assertIsNone(self.state.now_message)
        self.assert_no_new_message()

    async def test_transient_edit_failure_keeps_reference_and_never_reposts(self):
        self.message.error = RuntimeError('temporary API failure')
        await self.update(create=True)
        self.assertIs(self.state.now_message, self.message)
        self.assertTrue(self.state.panel_finished)
        self.assert_no_new_message()
        self.message.error = None
        await self.update(create=True)
        self.assertEqual(self.message.edits[-1]['view'].to_components(), [{'type': 10, 'content': EXACT}])

    async def test_not_found_clears_only_the_old_reference_without_reposting(self):
        self.message.error = NotFound('gone')
        await self.update(create=True)
        self.assertIsNone(self.state.now_message)
        self.assertTrue(self.state.panel_finished)
        self.assert_no_new_message()
        await self.update(create=True)
        self.assert_no_new_message()

    async def test_later_idle_timer_or_afk_reason_cannot_restore_the_card(self):
        await self.update(create=True)
        for reason in ('music_idle_timeout', 'idle', 'manual_stop', 'voice_idle_empty'):
            self.state.idle_reason = reason
            await self.update(create=True, repost=True)
        self.assertTrue(self.message.edits)
        for payload in self.message.edits:
            self.assertEqual(payload['view'].to_components(), [{'type': 10, 'content': EXACT}])
        self.assertEqual(len(self.message.edits), 1, 'unchanged idle snapshots do not edit again')
        self.assert_no_new_message()

    async def test_plain_idle_without_a_panel_does_not_create_one(self):
        self.state.idle_reason = 'idle'
        self.state.now_message = None
        await self.update(create=True)
        self.assert_no_new_message()

    async def test_virtual_pending_queue_is_not_a_finished_session(self):
        self.state.agent_remote_queue_size = 2
        self.state.agent_queue_layout = [{'kind': 'virtual', 'virtual': {
            'active': True, 'remaining': 2, 'next_offset': 3, 'total_tracks': 5,
            'instance_id': 'playlist', 'provider': 'spotify_public',
            'source_url': 'https://example.invalid/playlist',
        }}]
        self.state.agent_virtual_playlists = [self.state.agent_queue_layout[0]['virtual']]
        await self.update(create=False)
        self.assertFalse(self.state.panel_finished)
        self.assertIsInstance(self.message.edits[-1]['view'], PlayerView)
        self.assert_no_new_message()

    async def test_new_play_while_waiting_for_panel_lock_wins_over_old_finish(self):
        await self.state.panel_lock.acquire()
        pending = asyncio.create_task(self.update(create=False))
        await asyncio.sleep(0)
        self.state.current = track()
        self.state.current_status = 'playing'
        self.state.idle_reason = 'idle'
        self.state.music_operation_generation += 1
        self.state.agent_playback_token += 1
        self.state.panel_lock.release()
        await pending
        self.assertFalse(self.state.panel_finished)
        self.assertIs(self.message.edits[-1]['view'].current, self.state.current)
        self.assert_no_new_message()

    async def test_old_edit_not_found_cannot_clear_a_new_message_reference(self):
        replacement = Message(300)
        async def newer_session():
            self.state.current = track('replacement')
            self.state.current_status = 'playing'
            self.state.music_operation_generation += 1
            self.state.agent_playback_token += 1
            self.state.now_message = replacement
            self.state.panel_finished = False
        self.message.on_edit = newer_session
        self.message.error = NotFound('old message disappeared')
        await self.update(create=True)
        self.assertIs(self.state.now_message, replacement)
        self.assertFalse(self.state.panel_finished)
        self.assert_no_new_message()

    async def test_new_music_restores_the_normal_view_after_the_terminal_text(self):
        await self.update(create=True)
        self.state.current = track()
        self.state.current_status = 'playing'
        self.state.idle_reason = 'idle'
        self.state.music_operation_generation += 1
        await self.update(create=False)
        self.assertFalse(self.state.panel_finished)
        self.assertIsInstance(self.message.edits[-1]['view'], PlayerView)
        self.assert_no_new_message()

    async def test_pending_request_arriving_during_old_edit_is_not_marked_finished(self):
        activity = importlib.import_module('cogs.musica.reproducao.atividade')
        owners = []
        async def start_request():
            owners.append(activity.begin_music_request(self.router, 1))
        self.message.on_edit = start_request
        try:
            await self.update(create=False)
            self.assertFalse(self.state.panel_finished)
            self.assertEqual(self.state.panel_finished_message_id, 0)
            self.assertEqual(self.state.panel_finished_generation, -1)
            self.assertIsInstance(self.message.edits[-1]['view'], PlayerView)
            self.assertIsNone(self.state.current, 'the real play has not published a current track yet')
            self.assert_no_new_message()
        finally:
            for owner in owners:
                activity.end_music_request(self.router, 1, owner)

    async def test_new_current_arriving_during_terminal_http_edit_gets_fresh_active_view(self):
        async def new_current():
            self.state.current = track('during-edit')
            self.state.current_status = 'playing'
            self.state.idle_reason = 'idle'
            self.state.music_operation_generation += 1
            self.state.agent_playback_token += 1
        self.message.on_edit = new_current
        await self.update(create=False)
        self.assertEqual(len(self.message.edits), 2)
        self.assertEqual(self.message.edits[0]['view'].to_components(),
                         [{'type': 10, 'content': EXACT}])
        self.assertIsInstance(self.message.edits[1]['view'], PlayerView)
        self.assertIs(self.message.edits[1]['view'].current, self.state.current)
        self.assertFalse(self.state.panel_finished)
        self.assertEqual(self.state.panel_finished_message_id, 0)
        self.assertEqual(self.state.panel_track_key, 'during-edit')
        self.assert_no_new_message()

    async def test_request_begins_and_ends_during_old_edit_invalidates_its_finish_stamp(self):
        activity = importlib.import_module('cogs.musica.reproducao.atividade')
        async def short_request():
            owner = activity.begin_music_request(self.router, 1)
            activity.end_music_request(self.router, 1, owner)
        self.message.on_edit = short_request
        await self.update(create=False)
        self.assertEqual(len(self.message.edits), 1)
        self.assertFalse(self.state.panel_finished)
        self.assertEqual(self.state.panel_finished_message_id, 0)
        self.assertEqual(self.state.panel_finished_generation, -1)
        self.assert_no_new_message()


class LegacyIdleStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.Router = load_router()
        self.router = self.Router()
        self.state = MusicGuildState()
        self.state.music_session_active = True  # Can remain true after natural end.
        self.state.history.append(track('old'))
        self.state.agent_voice_connected = True
        self.state.tts_session_active_until = time.monotonic() + 10
        self.router.get_state = lambda gid: self.state
        self.router.current_music_operation_generation = lambda gid: self.state.music_operation_generation
        self.router._control_or_vote = mock.AsyncMock(return_value=(True, '', False))
        self.router.cancel_pending_music_operations = mock.Mock()
        self.router._cancel_next_prefetch = mock.Mock()
        self.router._invalidate_panel_controls_now = mock.Mock()
        self.router._set_idle_reason = mock.Mock(side_effect=lambda state, reason: setattr(state, 'idle_reason', reason))
        self.router._restore_music_session_side_effects = mock.AsyncMock()
        self.router._set_current_status = mock.Mock(side_effect=lambda state, status: setattr(state, 'current_status', status))
        self.router._cancel_music_idle_disconnect = mock.Mock()
        self.router.update_panel = mock.AsyncMock()
        self.guild = SimpleNamespace(voice_client=None, get_channel=lambda identity: None)
        self.router.bot = SimpleNamespace(get_guild=mock.Mock(return_value=self.guild), get_channel=lambda identity: None)

    async def test_idle_request_stop_returns_no_message_without_votes_or_cancellation(self):
        self.assertEqual(await self.router.request_stop(1, object()), (False, ''))
        self.router._control_or_vote.assert_not_awaited()
        self.router.cancel_pending_music_operations.assert_not_called()
        self.router.update_panel.assert_not_awaited()
        self.assertEqual(len(self.state.history), 1)
        self.assertTrue(self.state.music_session_active)

    async def test_idle_low_level_stop_does_not_touch_voice_history_or_session(self):
        self.assertFalse(await self.router.stop(1))
        self.router.bot.get_guild.assert_not_called()
        self.router._cancel_next_prefetch.assert_not_called()
        self.router._invalidate_panel_controls_now.assert_not_called()
        self.router._restore_music_session_side_effects.assert_not_awaited()
        self.router.update_panel.assert_not_awaited()
        self.assertEqual(len(self.state.history), 1)
        self.assertFalse(self.state.stop_requested)
        self.assertTrue(self.state.agent_voice_connected)

    async def test_active_local_stop_retains_cleanup_but_cannot_create_a_panel(self):
        self.state.current = track()
        self.state.current_status = 'paused'
        self.state.paused = True
        self.assertTrue(await self.router.stop(1))
        self.router._restore_music_session_side_effects.assert_awaited_once()
        self.router.update_panel.assert_awaited_once_with(1, create=False)
        self.assertIsNone(self.state.current)
        self.assertFalse(self.state.music_session_active)

    async def test_failed_active_stop_releases_ownership_for_a_retry(self):
        self.state.current = track()
        self.state.current_status = 'playing'
        self.router._restore_music_session_side_effects.side_effect = RuntimeError('restore failed')
        with self.assertRaisesRegex(RuntimeError, 'restore failed'):
            await self.router.stop(1)
        self.assertIsNone(self.state.music_stop_owner)
        self.router._restore_music_session_side_effects.side_effect = None
        self.assertTrue(await self.router.stop(1))

    async def test_two_concurrent_legacy_stops_execute_cleanup_once(self):
        self.state.current = track()
        self.state.current_status = 'playing'
        entered, release = asyncio.Event(), asyncio.Event()
        async def restore(*args, **kwargs):
            entered.set()
            await release.wait()
        self.router._restore_music_session_side_effects.side_effect = restore
        first = asyncio.create_task(self.router.stop(1))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertFalse(await asyncio.wait_for(self.router.stop(1), timeout=.2))
        finally:
            release.set()
            result = await asyncio.wait_for(first, timeout=1)
        self.assertTrue(result)
        self.router._restore_music_session_side_effects.assert_awaited_once()
        self.router.update_panel.assert_awaited_once_with(1, create=False)

    async def test_pending_vote_preserves_its_response_and_releases_stop_ownership(self):
        self.state.current = track()
        self.state.current_status = 'playing'
        self.router._control_or_vote.return_value = (False, 'Votação pendente', False)
        self.assertEqual(await self.router.request_stop(1, object()), (False, 'Votação pendente'))
        self.assertIsNone(self.state.music_stop_owner)
        self.router.cancel_pending_music_operations.assert_not_called()
        self.router._restore_music_session_side_effects.assert_not_awaited()

    async def test_music_ending_during_stop_vote_does_not_cancel_or_reply(self):
        self.state.current = track()
        self.state.current_status = 'playing'
        async def vote(*args, **kwargs):
            self.state.current = None
            self.state.current_status = 'idle'
            return True, '', False
        self.router._control_or_vote.side_effect = vote
        self.assertEqual(await self.router.request_stop(1, object()), (False, ''))
        self.assertIsNone(self.state.music_stop_owner)
        self.router.cancel_pending_music_operations.assert_not_called()
        self.router._restore_music_session_side_effects.assert_not_awaited()
        self.router.update_panel.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
