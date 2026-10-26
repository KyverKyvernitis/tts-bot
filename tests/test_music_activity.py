"""Activity admission uses music state only, without Discord or worker I/O."""
from __future__ import annotations

import asyncio
from collections import deque
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.reproducao.atividade import (
    begin_music_request, claim_music_stop, end_music_request,
    has_music_activity, owns_music_stop, release_music_stop,
)


DEFERRED_MODULE = "cogs.musica.agente_telefone.comandos"


def empty_state(**changes):
    values = dict(
        current=None, current_status="idle", paused=False,
        queue=asyncio.Queue(), forward_queue=deque(),
        current_backend="local", current_resolve_task=None,
        music_operation_generation=3, music_pending_requests=set(),
        music_request_serial=0, music_stop_owner=None,
        agent_remote_queue_size=0, agent_queue_layout=[],
        agent_virtual_playlists=[], agent_virtual_playlist=None,
        agent_deferred_command_status="",
    )
    values.update(changes)
    return SimpleNamespace(**values)


class Router:
    def __init__(self):
        self.states = {1: empty_state(), 2: empty_state()}
        self.get_state = mock.Mock(side_effect=lambda guild_id: self.states[guild_id])
        # None of these boundaries may be invoked to answer an activity check.
        self.ensure_music_worker_available = mock.Mock(side_effect=AssertionError("worker I/O"))
        self.sync_music_agent_state = mock.Mock(side_effect=AssertionError("remote sync"))
        self.save_state = mock.Mock(side_effect=AssertionError("storage I/O"))


class MusicActivityTests(unittest.TestCase):
    def setUp(self):
        self.router = Router()
        self.state = self.router.states[1]
        # Isolate the memory registry when this suite follows controller tests.
        self.deferred_patch = mock.patch.dict(sys.modules, {DEFERRED_MODULE: None})
        self.deferred_patch.start()
        self.addCleanup(self.deferred_patch.stop)

    def active(self):
        return has_music_activity(self.router, 1)

    def test_empty_state_does_not_touch_external_boundaries(self):
        self.assertFalse(self.active())
        self.router.get_state.assert_called_once_with(1)
        self.router.ensure_music_worker_available.assert_not_called()
        self.router.sync_music_agent_state.assert_not_called()
        self.router.save_state.assert_not_called()

    def test_history_voice_session_and_persistent_tts_mixer_are_idle(self):
        self.state.history = deque([object()])
        self.state.music_session_active = True
        self.state.music_owns_voice = True
        self.state.voice_client = SimpleNamespace(is_connected=lambda: True, is_playing=lambda: True)
        self.state.current_source = SimpleNamespace(tts_active=True)
        self.state.tts_session_active_until = float("inf")
        self.state.last_voice_channel_id = 99
        self.assertFalse(self.active())

    def test_playing_current_is_activity(self):
        self.state.current = object()
        self.state.current_status = "playing"
        self.assertTrue(self.active())

    def test_current_is_activity_during_recovery(self):
        self.state.current = object()
        self.state.current_status = "reconnecting"
        self.assertTrue(self.active())

    def test_paused_current_is_activity_even_if_status_is_stale(self):
        self.state.current = object()
        self.state.paused = True
        self.assertTrue(self.active())

    def test_stopped_stale_current_is_not_activity(self):
        self.state.current = object()
        for status in ("idle", "stopped", "error", "failed"):
            with self.subTest(status=status):
                self.state.current_status = status
                self.assertFalse(self.active())

    def test_player_playing_without_a_music_track_is_not_activity(self):
        self.state.current_status = "playing"
        self.assertFalse(self.active())

    def test_pending_music_statuses_count_without_current(self):
        for status in ("preparing", "resolving", "starting", "queued", "skipping"):
            with self.subTest(status=status):
                self.state.current_status = status
                self.assertTrue(self.active())

    def test_physical_queue_counts_without_current(self):
        self.state.queue.put_nowait(object())
        self.assertTrue(self.active())
        self.state.queue.get_nowait()
        self.assertFalse(self.active())

    def test_supported_list_and_tuple_queue_count(self):
        for queue in ([object()], (object(),)):
            with self.subTest(queue_type=type(queue)):
                self.state.queue = queue
                self.assertTrue(self.active())
        self.state.queue = []
        self.assertFalse(self.active())

    def test_forward_queue_counts(self):
        self.state.forward_queue.append(object())
        self.assertTrue(self.active())

    def test_running_resolve_counts_but_done_or_cancelling_does_not(self):
        for done, cancelled, cancelling, expected in (
            (False, False, 0, True), (True, False, 0, False),
            (True, True, 0, False), (False, False, 1, False),
            (False, True, 0, False),
        ):
            with self.subTest(done=done, cancelled=cancelled, cancelling=cancelling):
                self.state.current_resolve_task = SimpleNamespace(
                    done=lambda: done, cancelled=lambda: cancelled, cancelling=lambda: cancelling)
                self.assertEqual(self.active(), expected)

    def test_next_resolve_prefetch_alone_is_not_activity(self):
        self.state.next_resolve_task = SimpleNamespace(done=lambda: False)
        self.assertFalse(self.active())

    def test_remote_queue_size_requires_agent_backend(self):
        self.state.agent_remote_queue_size = 5
        self.assertFalse(self.active())
        self.state.current_backend = "agent"
        self.assertTrue(self.active())

    def test_invalid_remote_queue_sizes_do_not_invent_work(self):
        self.state.current_backend = "agent"
        for value in (0, -1, None, False, True, "invalid", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.state.agent_remote_queue_size = value
                self.assertFalse(self.active())

    def test_remote_physical_layout_counts(self):
        self.state.current_backend = "agent"
        self.state.agent_queue_layout = [{"kind": "track", "track": {"title": "Pending"}}]
        self.assertTrue(self.active())
        self.state.agent_queue_layout = [{"kind": "track", "track": None}]
        self.assertFalse(self.active())

    def test_virtual_layout_counts_when_preview_is_empty(self):
        self.state.current_backend = "agent"
        self.state.agent_queue_layout = [{"kind": "virtual", "virtual": {
            "cursor": {"next_offset": 10, "total_tracks": 30}}}]
        self.assertTrue(self.active())

    def test_virtual_queue_respects_bounds_remaining_and_exhaustion(self):
        self.state.current_backend = "agent"
        for info, expected in (
            ({"active": True, "remaining": 1}, True),
            ({"active": True, "remaining": 0}, False),
            ({"remaining": 2, "cursor": {"exhausted": True}}, False),
            ({"active": False, "remaining": 2}, False),
            ({"cursor": {"next_offset": 9, "total_tracks": 10}}, True),
            ({"cursor": {"next_offset": 10, "total_tracks": 10}}, False),
            ({"cursor": {"next_offset": 11, "total_tracks": 10}}, False),
            ({"cursor": {"next_offset": 5, "block_end_offset": 6, "total_tracks": 10}}, True),
            ({"cursor": {"next_offset": 6, "block_end_offset": 6, "total_tracks": 10}}, False),
            ({"cursor": {"next_offset": "invalid", "total_tracks": 10}}, False),
            ({"cursor": {"provider": "spotify", "source_url": "https://example.test/list"}}, True),
            ({"active": True}, True), ({}, False),
        ):
            with self.subTest(info=info):
                self.state.agent_virtual_playlists = [info]
                self.assertEqual(self.active(), expected)

    def test_legacy_virtual_cursor_is_supported(self):
        self.state.current_backend = "agent"
        self.state.agent_virtual_playlist = {"remaining": 4}
        self.assertTrue(self.active())
        self.state.agent_virtual_playlist = {"remaining": 0}
        self.assertFalse(self.active())

    def test_mirrored_deferred_pending_counts(self):
        self.state.agent_deferred_command_status = "pending"
        self.assertTrue(self.active())
        for status in ("done", "cancelled", "failed"):
            with self.subTest(status=status):
                self.state.agent_deferred_command_status = status
                self.assertFalse(self.active())

    def test_already_loaded_deferred_registry_is_read_in_memory(self):
        lookup = mock.Mock(side_effect=lambda guild_id: {"status": "pending"} if guild_id == 1 else {})
        sys.modules[DEFERRED_MODULE] = SimpleNamespace(estado_comando_diferido=lookup)
        self.assertTrue(self.active())
        self.assertFalse(has_music_activity(self.router, 2))
        self.assertEqual(lookup.call_args_list, [mock.call(1), mock.call(2)])

    def test_explicit_state_avoids_router_lookup(self):
        self.state.queue.put_nowait(object())
        self.assertTrue(has_music_activity(None, 1, state=self.state))
        self.router.get_state.assert_not_called()

    def test_nested_requests_stay_active_until_their_exact_owner_ends(self):
        first = begin_music_request(self.router, 1)
        second = begin_music_request(self.router, 1)
        self.assertIsNot(first, second)
        self.assertEqual(self.state.music_request_serial, 2)
        self.assertTrue(self.active())
        end_music_request(self.router, 1, object())
        end_music_request(self.router, 1, first)
        self.assertTrue(self.active())
        end_music_request(self.router, 1, first)  # finally cleanup is idempotent
        self.assertTrue(self.active())
        end_music_request(self.router, 1, second)
        self.assertFalse(self.active())

    def test_request_from_cancelled_generation_does_not_keep_session_alive(self):
        first = begin_music_request(self.router, 1)
        self.state.music_operation_generation += 1
        self.assertFalse(self.active())
        second = begin_music_request(self.router, 1)
        end_music_request(self.router, 1, first)
        self.assertTrue(self.active())
        end_music_request(self.router, 1, second)
        self.assertFalse(self.active())

    def test_pending_requests_are_isolated_per_guild(self):
        owner = begin_music_request(self.router, 1)
        self.assertFalse(has_music_activity(self.router, 2))
        end_music_request(self.router, 2, owner)
        self.assertTrue(self.active())
        end_music_request(self.router, 1, owner)
        self.assertFalse(self.active())

    def test_request_initializes_fields_on_legacy_state(self):
        self.router.states[1] = SimpleNamespace(music_operation_generation=0)
        owner = begin_music_request(self.router, 1)
        self.assertEqual(self.router.states[1].music_request_serial, 1)
        self.assertTrue(self.active())
        end_music_request(self.router, 1, owner)
        self.assertFalse(self.active())

    def test_stop_owner_is_exclusive_and_only_owner_can_release(self):
        owner = claim_music_stop(self.router, 1)
        self.assertTrue(owns_music_stop(self.router, 1, owner))
        self.assertIsNone(claim_music_stop(self.router, 1))
        self.assertFalse(owns_music_stop(self.router, 1, object()))
        self.assertFalse(owns_music_stop(self.router, 1, None))
        release_music_stop(self.router, 1, object())
        release_music_stop(self.router, 1, None)
        self.assertTrue(owns_music_stop(self.router, 1, owner))
        release_music_stop(self.router, 1, owner)
        self.assertFalse(owns_music_stop(self.router, 1, owner))
        self.assertIsNotNone(claim_music_stop(self.router, 1))

    def test_stop_owner_isolated_per_guild_and_does_not_imply_activity(self):
        first = claim_music_stop(self.router, 1)
        second = claim_music_stop(self.router, 2)
        self.assertIsNot(first, second)
        self.assertFalse(self.active())
        release_music_stop(self.router, 2, first)
        self.assertTrue(owns_music_stop(self.router, 2, second))
        release_music_stop(self.router, 1, first)
        self.assertTrue(owns_music_stop(self.router, 2, second))


if __name__ == "__main__":
    unittest.main()
