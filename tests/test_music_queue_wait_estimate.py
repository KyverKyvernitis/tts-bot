"""Read-only queue estimates over production snapshots, clocks and cache data.

These stdlib checks need neither Discord, network access nor a media resolver.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest

from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import LoopMode, MusicTrack
from cogs.musica.reproducao.estimativa_fila import (
    QueueWaitEstimate, estimate_queue_wait, estimate_queue_waits, queue_wait_line,
)


def track(identity: str, duration: float | None = 180, **values):
    return {"queue_item_id": identity, "title": "Same title", "duration": duration,
            "is_live": False, **values}


def raw(*prior, target=None, current=None, position=110000, **values):
    target = track("target", 153) if target is None else target
    queue = [*prior, target]
    return {"status": "playing", "paused": False, "confirmed_playing": True,
            "voice_connected": True, "loop_mode": "off", "speed_multiplier": 1,
            "current": track("current", 180) if current is None else current,
            "position_ms": position, "queue": queue[:50], "queue_size": len(queue),
            "logical_queue_size": len(queue),
            "queue_layout": [{"kind": "track", "track": item} for item in queue], **values}


def virtual(start=0, end=3, *, instance="instance", **values):
    return {"active": True, "instance_id": instance, "provider": "spotify_public",
            "source_url": "https://example.invalid/playlist", "next_offset": start,
            "total_tracks": end, "block_end_offset": end, "remaining": end - start,
            "shuffle_seed": 0, **values}


def cached(info, durations):
    key = tuple(info[name] for name in ("instance_id", "provider", "source_url"))
    return SimpleNamespace(agent_virtual_duration_cache={key: dict(durations)},
                           agent_virtual_duration_ends={})


class QueueWaitLabelTests(unittest.TestCase):
    def test_chosen_public_formats_and_unknown(self):
        self.assertEqual(QueueWaitEstimate(260).label(), "-# ⏳ Vai começar em: 4 minutos e 20 segundos")
        self.assertEqual(QueueWaitEstimate(260, True).label(), "-# ⏸️ Após retomar: 4 minutos e 20 segundos")
        self.assertEqual(QueueWaitEstimate(None).label(), "-# ⏳ Previsão indisponível")
        self.assertEqual(QueueWaitEstimate(260).metadata_label(), "⏳ aproximadamente **4:20**")

    def test_seconds_round_up_and_hours_keep_the_full_wait(self):
        self.assertIn("0 minutos e 1 segundo", QueueWaitEstimate(.001).label())
        self.assertIn("60 minutos e 1 segundo", QueueWaitEstimate(3600.001).label())
        self.assertIn("0 minutos e 0 segundos", QueueWaitEstimate(0).label())

    def test_acknowledgement_uses_small_text_words_and_correct_singular_plural(self):
        for seconds, expected in (
            (1, "0 minutos e 1 segundo"),
            (60, "1 minuto e 0 segundos"),
            (61, "1 minuto e 1 segundo"),
            (187, "3 minutos e 7 segundos"),
            (3900, "65 minutos e 0 segundos"),
        ):
            with self.subTest(seconds=seconds):
                line = QueueWaitEstimate(seconds).label()
                self.assertEqual(line, "-# ⏳ Vai começar em: " + expected)
                self.assertNotIn("**", line)
                self.assertNotIn("aproximadamente", line)

    def test_invalid_diagnostic_values_cannot_break_the_formatter(self):
        for value in (float("inf"), float("nan"), -1, True, "invalid"):
            with self.subTest(value=value):
                self.assertEqual(QueueWaitEstimate(value).label(), "-# ⏳ Previsão indisponível")

    def test_estimate_is_a_frozen_snapshot(self):
        estimate = QueueWaitEstimate(260)
        with self.assertRaises(FrozenInstanceError):
            estimate.seconds = 10


class RawQueueWaitTests(unittest.TestCase):
    def test_current_remainder_plus_prior_tracks_excludes_target_duration(self):
        state = raw(track("prior", 190))
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 260)
        state["queue_layout"][-1]["track"]["duration"] = 999999
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 260)

    def test_batch_estimates_follow_the_full_prefix_and_only_original_id(self):
        one, two, three = track("one", 30), track("two", 60), track("three", 90)
        state = raw(one, two, target=three)
        self.assertEqual([value.seconds for value in estimate_queue_waits(state, [three, "one", two])],
                         [160, 70, 100])

    def test_speed_converts_media_duration_to_wall_time(self):
        for speed in (.4, .8, 1.25, 1.5, 1.75):
            with self.subTest(speed=speed):
                state = raw(track("prior", 190), speed_multiplier=speed)
                self.assertAlmostEqual(estimate_queue_wait(state, "target").seconds, 260 / speed)

    def test_seek_position_is_subtracted_once_and_clamped_after_the_end(self):
        state = raw(position=175000)
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 5)
        state["position_ms"] = 181000
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 0)

    def test_full_layout_beyond_the_fifty_track_preview(self):
        state = raw(*(track(str(index), 10) for index in range(61)))
        self.assertEqual(len(state["queue"]), 50)
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 680)

    def test_repeated_titles_do_not_match_a_different_entry(self):
        state = raw(track("same-song-earlier", 190))
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 260)
        self.assertIsNone(estimate_queue_wait(state, track("absent", 190)).seconds)

    def test_repeated_identity_or_current_queue_alias_is_not_a_numeric_wait(self):
        state = raw(track("target", 190))
        self.assertIsNone(estimate_queue_wait(state, "target").seconds)
        state = raw(track("current", 190))
        self.assertIsNone(estimate_queue_wait(state, "target").seconds)
        self.assertIsNone(estimate_queue_wait(raw(), "current").seconds)

    def test_paused_snapshot_zero_position_is_valid_and_conditional(self):
        state = raw(status="paused", paused=True, confirmed_playing=False, position=0)
        value = estimate_queue_wait(state, "target")
        self.assertEqual(value.seconds, 180)
        self.assertTrue(value.paused)
        self.assertIn("Após retomar", value.label())

    def test_single_repeat_is_indeterminate_and_queue_repeat_uses_first_pass(self):
        self.assertIsNone(estimate_queue_wait(raw(loop_mode="one"), "target").seconds)
        self.assertEqual(estimate_queue_wait(raw(loop_mode="all"), "target").seconds, 70)

    def test_recovery_disconnection_missing_clock_and_unconfirmed_start_are_unknown(self):
        patches = [dict(status="reconnecting"), dict(status="preparing"),
                   dict(voice_runtime_recovery_pending=True), dict(voice_connected=False),
                   dict(confirmed_playing=False), dict(position_ms=None)]
        for patch in patches:
            with self.subTest(patch=patch):
                self.assertIsNone(estimate_queue_wait(raw(**patch), "target").seconds)

    def test_unknown_or_live_prior_duration_does_not_poison_an_earlier_target(self):
        one, unknown, target = track("one", 10), track("unknown", None), track("target")
        state = raw(one, unknown, target=target)
        self.assertEqual(estimate_queue_wait(state, "one").seconds, 70)
        self.assertEqual(estimate_queue_wait(state, "unknown").seconds, 80)
        self.assertIsNone(estimate_queue_wait(state, "target").seconds)
        state["queue_layout"][1]["track"] = track("live", 180, is_live=True)
        self.assertIsNone(estimate_queue_wait(state, "target").seconds)

    def test_live_or_unknown_current_has_no_numeric_wait(self):
        for current in (track("current", None), track("current", 180, is_live=True)):
            self.assertIsNone(estimate_queue_wait(raw(current=current), "target").seconds)

    def test_invalid_finite_nonfinite_and_bool_metadata_never_raise(self):
        for value in (None, True, -1, "no", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(estimate_queue_wait(raw(current=track("current", value)), "target").seconds)
                self.assertIsNone(estimate_queue_wait(raw(speed_multiplier=value), "target").seconds)
        estimate = estimate_queue_wait(raw(current=track("current", 1e308), position=0,
                                           speed_multiplier=.4), "target")
        self.assertIsNone(estimate.seconds)
        self.assertEqual(estimate.label(), "-# ⏳ Previsão indisponível")

    def test_old_snapshot_preview_requires_complete_proven_queue(self):
        state = raw(track("prior", 190))
        del state["queue_layout"]
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 260)
        state["logical_queue_size"] = 200
        self.assertIsNone(estimate_queue_wait(state, "target").seconds)
        del state["logical_queue_size"]
        del state["queue_size"]
        self.assertIsNone(estimate_queue_wait(state, "target").seconds)

    def test_malformed_layout_cannot_be_replaced_with_a_short_preview(self):
        self.assertIsNone(estimate_queue_wait(raw(queue_layout=None), "target").seconds)
        self.assertIsNone(estimate_queue_wait(raw(queue_layout=[]), "target").seconds)

    def test_zero_prior_duration_is_known_and_inputs_are_not_modified(self):
        state = raw(track("prior", 0))
        before = deepcopy(state)
        self.assertEqual(queue_wait_line(state, "target"), "-# ⏳ Vai começar em: 1 minuto e 10 segundos")
        self.assertEqual(state, before)

    def test_reorder_and_removal_are_reflected_on_each_new_estimate(self):
        state = raw(track("prior", 190))
        original = estimate_queue_wait(state, "target")
        state["queue_layout"].reverse()
        self.assertEqual(estimate_queue_wait(state, "target").seconds, 70)
        self.assertEqual(original.seconds, 260, "the inclusion snapshot must remain unchanged")


class LocalQueueClockTests(unittest.TestCase):
    def state(self):
        state = MusicGuildState()
        state.current = MusicTrack("Current", "", 1, duration=180, queue_item_id="current")
        state.current_status = "playing"
        state.current_started_at_monotonic = 100
        state.current_start_offset_seconds = 30
        state.forward_queue = deque([MusicTrack("Prior", "", 1, duration=190, queue_item_id="prior")])
        self.target = MusicTrack("Target", "", 1, duration=153, queue_item_id="target")
        state.queue.put_nowait(self.target)
        return state

    def test_local_monotonic_clock_offset_and_prior_forward_queue(self):
        state = self.state()
        self.assertEqual(estimate_queue_wait(state, self.target, now=180).seconds, 260)
        self.assertEqual(estimate_queue_wait(state, self.target, now=190).seconds, 250)

    def test_local_speed_changes_current_clock_and_future_media(self):
        state = self.state()
        state.playback_speed = 1.25
        self.assertAlmostEqual(estimate_queue_wait(state, self.target, now=164).seconds, 260 / 1.25)

    def test_local_pause_uses_frozen_position_including_zero(self):
        state = self.state()
        state.paused = True
        state.voice_status_pause_position_seconds = 0
        value = estimate_queue_wait(state, self.target, now=100000)
        self.assertEqual(value.seconds, 370)
        self.assertTrue(value.paused)
        state.voice_status_pause_position_seconds = 110
        self.assertEqual(estimate_queue_wait(state, self.target, now=100001).seconds, 260)

    def test_local_unknown_start_clock_cannot_guess_from_zero(self):
        state = self.state()
        state.current_started_at_monotonic = 0
        self.assertIsNone(estimate_queue_wait(state, self.target, now=180).seconds)

    def test_local_track_without_remote_id_uses_object_identity(self):
        state = self.state()
        self.target.queue_item_id = ""
        self.assertEqual(estimate_queue_wait(state, self.target, now=180).seconds, 260)
        copied = MusicTrack("Target", "", 1, duration=153)
        self.assertIsNone(estimate_queue_wait(state, copied, now=180).seconds)

    def test_agent_mirror_recovery_disconnection_and_stuck_active_monitor(self):
        state = self.state()
        state.current_backend = "agent"
        state.agent_remote_queue_size = 2
        state.agent_voice_connected = True
        state.loop_mode = LoopMode.ALL
        self.assertEqual(estimate_queue_wait(state, self.target, now=180).seconds, 260)
        state.agent_voice_recovery_pending = True
        self.assertIsNone(estimate_queue_wait(state, self.target, now=180).seconds)
        state.agent_voice_recovery_pending = False
        state.agent_voice_connected = False
        self.assertIsNone(estimate_queue_wait(state, self.target, now=180).seconds)
        state.agent_voice_connected = True
        state.agent_monitor_task = SimpleNamespace(done=lambda: False)
        state.agent_monitor_last_success_at = 160
        self.assertIsNone(estimate_queue_wait(state, self.target, now=180).seconds)
        state.agent_monitor_task = None
        self.assertEqual(estimate_queue_wait(state, self.target, now=180).seconds, 260,
                         "manual controller refresh can succeed without an active monitor")

    def test_local_agent_short_preview_is_not_the_full_queue(self):
        state = self.state()
        state.current_backend = "agent"
        state.agent_voice_connected = True
        state.agent_remote_queue_size = 60
        self.assertIsNone(estimate_queue_wait(state, self.target, now=180).seconds)


class VirtualQueueWaitTests(unittest.TestCase):
    def state(self, info=None):
        self.info = virtual(5, 8) if info is None else info
        self.target = track("target", 153)
        state = raw(target=self.target)
        state["queue_layout"] = [
            {"kind": "track", "track": track("physical", 10)},
            {"kind": "virtual", "virtual": self.info},
            {"kind": "track", "track": self.target},
        ]
        self.cache = cached(self.info, {5: 20, 6: 30, 7: 40})
        return state

    def test_only_matching_cached_virtual_range_before_target_is_summed(self):
        state = self.state()
        self.assertEqual(estimate_queue_wait(state, self.target, duration_state=self.cache).seconds, 170)
        self.cache.agent_virtual_duration_cache[next(iter(self.cache.agent_virtual_duration_cache))][99] = 999999
        self.assertEqual(estimate_queue_wait(state, self.target, duration_state=self.cache).seconds, 170)

    def test_raw_cursor_and_local_virtual_shapes_share_the_same_result(self):
        state = self.state()
        state["queue_layout"][1] = {"kind": "virtual", "cursor": dict(self.info), "remaining": 3}
        self.assertEqual(estimate_queue_wait(state, self.target, duration_state=self.cache).seconds, 170)

    def test_virtual_target_uses_source_index_before_its_own_duration(self):
        state = self.state()
        page_target = MusicTrack("Virtual", "", 1, virtual_playlist_instance_id="instance",
                                 virtual_provider="spotify_public",
                                 virtual_source_url=self.info["source_url"], virtual_source_index=6)
        self.assertEqual(estimate_queue_wait(state, page_target, duration_state=self.cache).seconds, 100)
        key = next(iter(self.cache.agent_virtual_duration_cache))
        self.cache.agent_virtual_duration_cache[key].pop(6)
        self.assertEqual(estimate_queue_wait(state, page_target, duration_state=self.cache).seconds, 100,
                         "a target's unknown duration does not affect its starting time")
        self.assertIsNone(estimate_queue_wait(state, "target", duration_state=self.cache).seconds)

    def test_first_virtual_target_needs_no_prior_virtual_metadata(self):
        state = self.state()
        page_target = {"virtual_playlist_instance_id": "instance", "virtual_provider": "spotify_public",
                       "virtual_source_url": self.info["source_url"], "virtual_source_index": 5}
        self.assertEqual(estimate_queue_wait(state, page_target).seconds, 80)

    def test_virtual_occurrence_identity_and_bounds_reject_stale_cache(self):
        state = self.state()
        self.cache.agent_virtual_duration_cache = {("old-instance", "spotify_public", self.info["source_url"]): {5: 20, 6: 30, 7: 40}}
        self.assertIsNone(estimate_queue_wait(state, self.target, duration_state=self.cache).seconds)
        self.assertEqual(estimate_queue_wait(state, "physical", duration_state=self.cache).seconds, 70)

    def test_virtual_after_target_has_no_effect_and_split_ranges_do_not_double_count(self):
        state = self.state()
        state["queue_layout"] = [state["queue_layout"][-1], state["queue_layout"][1]]
        self.assertEqual(estimate_queue_wait(state, "target", duration_state=self.cache).seconds, 70)
        state = self.state()
        one, two = dict(self.info, block_end_offset=6, remaining=1), dict(self.info, next_offset=7, remaining=1)
        state["queue_layout"] = [{"kind": "virtual", "virtual": one}, {"kind": "virtual", "virtual": two},
                                 {"kind": "track", "track": self.target}]
        self.assertEqual(estimate_queue_wait(state, "target", duration_state=self.cache).seconds, 130)

    def test_unknown_shuffled_or_unbounded_virtual_prefix_is_not_a_partial_sum(self):
        state = self.state()
        key = next(iter(self.cache.agent_virtual_duration_cache))
        for update in (dict(shuffle_seed=42), dict(block_end_offset=None, total_tracks=None, remaining=None)):
            with self.subTest(update=update):
                state["queue_layout"][1]["virtual"] = dict(self.info, **update)
                self.assertIsNone(estimate_queue_wait(state, "target", duration_state=self.cache).seconds)
        state["queue_layout"][1]["virtual"] = self.info
        self.cache.agent_virtual_duration_cache[key][6] = float("nan")
        self.assertIsNone(estimate_queue_wait(state, "target", duration_state=self.cache).seconds)

    def test_cache_known_end_and_remaining_bound_are_respected(self):
        state = self.state()
        info = state["queue_layout"][1]["virtual"]
        info.update(block_end_offset=None, total_tracks=None, remaining=None)
        key = next(iter(self.cache.agent_virtual_duration_cache))
        self.cache.agent_virtual_duration_ends[key] = 8
        self.assertEqual(estimate_queue_wait(state, "target", duration_state=self.cache).seconds, 170)
        info["remaining"] = 1
        self.assertEqual(estimate_queue_wait(state, "target", duration_state=self.cache).seconds, 100)

    def test_large_missing_range_is_bounded_by_actual_cache_and_does_not_mutate(self):
        state = self.state(virtual(0, 10**12))
        before, before_cache = deepcopy(state), deepcopy(self.cache.agent_virtual_duration_cache)
        self.assertIsNone(estimate_queue_wait(state, "target", duration_state=self.cache).seconds)
        self.assertEqual(state, before)
        self.assertEqual(self.cache.agent_virtual_duration_cache, before_cache)


if __name__ == "__main__":
    unittest.main()
