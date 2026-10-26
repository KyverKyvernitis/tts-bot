"""Queue estimates in actual UI formatters with Discord boundaries adapted.

The estimator, domain objects and production formatter AST execute here. No
Discord API, provider lookup or audio decoder is needed to render these views.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from collections import deque
from copy import deepcopy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.reproducao import estimativa_fila as eta


SOURCE = Path(__file__).resolve().parents[1] / 'cogs/musica/interface/componentes.py'


def load_ui(**overrides):
    tree = ast.parse(SOURCE.read_text())
    wanted = {'_agent_play_message', '_agent_confirmed_playing', '_agent_guild_state',
              '_public_audio_quality_label', '_player_track_text', '_queue_items',
              '_queue_total_count', '_virtual_playlists_info', '_virtual_playlist_info',
              '_queue_duration_for_panel', '_queue_preview_text', '_watch_agent_message'}
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in wanted]
    assert {node.name for node in functions} == wanted
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'QueueView')
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
               and node.name in {'_page_items', '_max_page', '_queue_text'}]
    assert len(methods) == 3
    queue_class = ast.ClassDef(name='QueueView', bases=[], keywords=[], body=methods, decorator_list=[])
    namespace = {
        'asyncio': asyncio, 'contextlib': contextlib, 'math': math,
        'MusicTrack': MusicTrack, 'QUEUE_PAGE_SIZE': 8,
        'queue_wait_line': eta.queue_wait_line,
        'estimate_queue_waits': lambda state, targets: eta.estimate_queue_waits(state, targets, now=1000),
        '_escape': lambda value, limit: str(value or '')[:limit],
        '_track_link_v2': lambda track, title_limit=84, **kwargs:
            f'[{track.short_title[:title_limit]}]({track.webpage_url})',
        '_source_badge_for_track': lambda track: ('▶️', 'YouTube'),
        **overrides,
    }
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *functions, queue_class], type_ignores=[]))
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return SimpleNamespace(**namespace)


def song(key, title='Labyrinth', seconds=153):
    return MusicTrack(title=title, webpage_url=f'https://example.invalid/{key}',
                      requester_id=1, queue_item_id=key, duration=seconds)


def raw_song(key, title='Labyrinth', seconds=153):
    return dict(queue_item_id=key, title=title, duration=seconds, is_live=False)


def response():
    previous = raw_song('before', 'Anterior', 190)
    target = raw_song('new')
    return {'queued': True, 'track': target, 'state': {
        'status': 'playing', 'confirmed_playing': True, 'paused': False,
        'loop_mode': 'off', 'speed_multiplier': 1, 'position_ms': 130000,
        'current': raw_song('current', 'Atual', 200), 'queue_size': 2,
        'queue': [previous, target],
        'queue_layout': [{'kind': 'track', 'track': item} for item in [previous, target]],
    }}


def local_state():
    current, previous, target = song('current', 'Atual', 200), song('before', 'Anterior', 190), song('new')
    state = MusicGuildState(current=current, current_backend='agent', current_status='playing',
                            current_started_at_monotonic=1000, current_start_offset_seconds=130)
    state.agent_voice_connected = True
    state.forward_queue = deque([previous, target])
    state.agent_remote_queue_size = 2
    state.agent_queue_layout = [{'kind': 'track', 'track': track} for track in [previous, target]]
    return state, previous, target


class QueueConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.ui = load_ui()
        self.track = song('new')

    def test_single_confirmation_uses_queued_target_and_excludes_its_own_duration(self):
        result = response()
        original = deepcopy(result)
        content = self.ui._agent_play_message(self.track, result)
        self.assertEqual(content, '`🎶` **Adicionada à fila:** Labyrinth • `2:33`\n'
                                 '⏳ Começa em aproximadamente **4:20**')
        self.assertNotIn('Atual', content)
        self.assertEqual(result, original)

    def test_title_enrichment_can_preserve_fixed_enqueue_estimate(self):
        result = response()
        first = self.ui._agent_play_message(self.track, result)
        wait_line = first.split('\n', 1)[1]
        result['state']['position_ms'] = 195000
        result['track']['title'] = 'Labyrinth — título completo'
        edited = self.ui._agent_play_message(self.track, result, wait_line=wait_line)
        self.assertIn('título completo', edited)
        self.assertTrue(edited.endswith('⏳ Começa em aproximadamente **4:20**'))

    def test_provisional_title_also_contains_initial_wait_estimate(self):
        result = response()
        result['track']['title'] = self.track.title = 'YouTube'
        content = self.ui._agent_play_message(self.track, result)
        self.assertIn('Carregando detalhes...', content)
        self.assertTrue(content.endswith('⏳ Começa em aproximadamente **4:20**'))

    def test_pause_is_conditional_and_unknown_duration_is_not_a_false_number(self):
        result = response()
        result['state'].update(status='paused', paused=True)
        self.assertIn('Após retomar: aproximadamente **4:20**', self.ui._agent_play_message(self.track, result))
        result['state']['current']['duration'] = None
        self.assertTrue(self.ui._agent_play_message(self.track, result).endswith('⏳ Previsão indisponível'))

    def test_truncated_preview_last_item_cannot_supply_eta_for_another_target(self):
        result = response()
        result.pop('track')
        result['state']['queue_layout'] = [{'kind': 'track', 'track': raw_song('unrelated', seconds=20)}]
        result['state']['queue'] = [raw_song('unrelated', seconds=20)]
        result['state']['queue_size'] = 20
        content = self.ui._agent_play_message(self.track, result)
        self.assertIn('Adicionada à fila:** Labyrinth', content)
        self.assertNotIn('unrelated', content)
        self.assertTrue(content.endswith('⏳ Previsão indisponível'))

    def test_starting_playing_and_failed_confirmation_do_not_gain_a_queue_estimate(self):
        for state, expected in [
            ({'status': 'starting'}, 'Preparando para tocar'),
            ({'status': 'playing', 'confirmed_playing': True, 'current': raw_song('new')}, 'Tocando'),
            ({'status': 'failed', 'last_error': 'sem áudio'}, 'Não consegui iniciar'),
        ]:
            with self.subTest(status=state['status']):
                content = self.ui._agent_play_message(self.track, {'state': state})
                self.assertIn(expected, content)
                self.assertNotIn('⏳', content)

    def test_virtual_duration_cache_is_forwarded_without_overriding_remote_position(self):
        calls = []
        def fixed(state, target, **options):
            calls.append((state, target, options))
            return '⏳ Previsão indisponível'
        ui = load_ui(queue_wait_line=fixed)
        result, cache = response(), object()
        ui._agent_play_message(self.track, result, duration_state=cache)
        self.assertEqual(calls, [(result['state'], result['track'], {'duration_state': cache})])


class QueuePageTests(unittest.TestCase):
    def setUp(self):
        self.ui = load_ui()
        self.state, self.previous, self.target = local_state()
        self.view = self.ui.QueueView()
        self.view.page, self.view.selected_position = 0, None

    def render(self):
        return self.view._queue_text(self.state, list(self.state.forward_queue))

    def test_page_displays_title_then_small_duration_and_current_wait(self):
        content = self.render()
        self.assertIn('**02**  [Labyrinth](https://example.invalid/new)\n-# 2:33 · ⏳ aproximadamente **4:20**', content)
        self.assertIn('**01**  [Anterior]', content)
        self.assertIn('-# 3:10 · ⏳ aproximadamente **1:10**', content)

    def test_reopening_recomputes_time_and_reordering_changes_predecessors(self):
        self.assertIn('⏳ aproximadamente **4:20**', self.render())
        self.state.current_start_offset_seconds = 160
        self.assertIn('⏳ aproximadamente **3:50**', self.render())
        self.state.agent_queue_layout = [{'kind': 'track', 'track': item} for item in [self.target, self.previous]]
        self.state.forward_queue = deque([self.target, self.previous])
        self.assertIn('**01**  [Labyrinth](https://example.invalid/new)\n-# 2:33 · ⏳ aproximadamente **0:40**', self.render())

    def test_pause_and_recovery_show_conditional_or_unavailable_estimates(self):
        self.state.paused = True
        self.state.current_status = 'paused'
        self.state.voice_status_pause_position_seconds = 130
        self.assertIn('após retomar', self.render())
        self.state.paused = False
        self.state.current_status = 'reconnecting'
        self.assertIn('previsão indisponível', self.render())

    def test_empty_page_stays_empty_and_does_not_render_an_eta(self):
        self.state.current = None
        self.state.forward_queue.clear()
        self.state.agent_queue_layout = []
        self.state.agent_remote_queue_size = 0
        content = self.render()
        self.assertIn('A fila está vazia', content)
        self.assertNotIn('⏳', content)

    def test_single_prefix_calculation_serves_all_tracks_on_the_page(self):
        original = self.view._queue_text.__globals__['estimate_queue_waits']
        spy = mock.Mock(wraps=original)
        self.view._queue_text.__globals__['estimate_queue_waits'] = spy
        self.render()
        spy.assert_called_once_with(self.state, [self.previous, self.target])

    def test_long_urls_keep_all_eight_rows_and_text_under_discord_limit(self):
        tracks = [song(str(index), title='Long title ' * 10, seconds=3600) for index in range(8)]
        for track in tracks:
            track.webpage_url += '?value=' + 'x' * 2000
        self.state.current.webpage_url += '?value=' + 'x' * 2000
        self.state.forward_queue = deque(tracks)
        self.state.agent_queue_layout = [{'kind': 'track', 'track': track} for track in tracks]
        self.state.agent_remote_queue_size = 8
        self.view.selected_position = 3
        content = self.render()
        self.assertLessEqual(len(content), 4000)
        self.assertEqual(content.count('⏳ aproximadamente'), 8)
        self.assertIn('**▶**', content)
        self.assertNotIn('?value=', content)

    def test_main_player_queue_preview_remains_compact_without_per_track_estimates(self):
        content = self.ui._queue_preview_text(self.state)
        self.assertIn('**Fila** · 2 músicas', content)
        self.assertNotIn('⏳', content)
        self.assertNotIn('aproximadamente', content)


class CompactAudioLineTests(unittest.TestCase):
    def test_processed_track_text_keeps_output_bitrate_and_original_media_metadata(self):
        ui = load_ui()
        track = song('current', 'Mili - Hero')
        track.uploader = 'Mili'
        track.resolved_audio_codec, track.resolved_audio_abr = 'opus', 128
        track.opus_processed_active, track.opus_processed_bitrate_kbps = True, 96
        state = SimpleNamespace(current=track, current_backend='agent', current_status='playing', paused=False)
        content = ui._player_track_text(state, track)
        self.assertIn('YouTube · Opus processado · 96 kbps', content)
        self.assertNotIn('fonte Opus', content)
        self.assertNotIn('128 kbps', content)
        self.assertEqual((track.resolved_audio_codec, track.resolved_audio_abr), ('opus', 128))


class StartingMessageWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_watcher_uses_actual_ui_formatter_for_confirmed_playback(self):
        state = {'status': 'playing', 'confirmed_playing': True, 'current': raw_song('new')}
        sleep = mock.AsyncMock()
        ui = load_ui(
            asyncio=SimpleNamespace(get_running_loop=asyncio.get_running_loop, sleep=sleep),
            config=SimpleNamespace(), discord=SimpleNamespace(NotFound=type('NotFound', (Exception,), {})),
            monitor_music_agent_ativo=lambda *args: True,
            estado_local_music_agent=lambda *args: state,
        )
        message = SimpleNamespace(edit=mock.AsyncMock())
        await asyncio.wait_for(ui._watch_agent_message(message, 1, song('new'), router=object()), timeout=.5)
        message.edit.assert_awaited_once()
        self.assertIn('**Tocando:** Labyrinth', message.edit.call_args.kwargs['content'])


if __name__ == '__main__':
    unittest.main()
