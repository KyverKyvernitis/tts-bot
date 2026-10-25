"""Gateway-cache readiness before play; real server methods, no Discord I/O."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest import mock

from cogs.musica.runtime_telefone.agente.estado import GuildMusicState
from tests.test_music_output_api import load_api


class ChannelBitrateHintTests(unittest.TestCase):
    def setUp(self):
        Agent, _ = load_api()
        self.agent = Agent()
        self.agent.states = {}
        self.agent.executor_mode = "full"
        self.agent.log = mock.Mock()
        self.channel = SimpleNamespace(id=19, bitrate=96000, guild=SimpleNamespace(id=1))
        self.channels = {19: self.channel}
        self.get_channel = mock.Mock(side_effect=self.channels.get)
        self.agent.client = SimpleNamespace(get_channel=self.get_channel)
        self.agent.cmd_play = mock.AsyncMock(return_value={"ok": True})
        self.agent.cmd_enqueue_discord_attachment = mock.AsyncMock(return_value={"ok": True})
        self.agent.cmd_skip = mock.AsyncMock(return_value={"ok": True})
        self.agent.cmd_previous = mock.AsyncMock(return_value={"ok": True})
        self.agent.cmd_queue_play_now = mock.AsyncMock(return_value={"ok": True})
        self.agent.cmd_queue_virtual_action = mock.AsyncMock(return_value={"ok": True})
        self.duration = .04
        self.globals = Agent._wait_channel_bitrate_hint.__globals__
        self.patch = mock.patch.dict(self.globals, {"env_float": lambda name, default: self.duration})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def body(self, **patch):
        return {"guild_id": 1, "voice_channel_id": 19, "channel_bitrate_hint_channel_id": 19,
                "channel_bitrate_hint_bps": 256000, **patch}

    def wait(self, body):
        return asyncio.run(self.agent._wait_channel_bitrate_hint(body))

    def test_no_hint_and_invalid_values_return_without_cache_or_wait(self):
        for patch in ({"channel_bitrate_hint_bps": None}, {"channel_bitrate_hint_bps": True},
                      {"channel_bitrate_hint_bps": "256000"}, {"channel_bitrate_hint_bps": 7999},
                      {"channel_bitrate_hint_bps": 512001}, {"guild_id": 0}, {"guild_id": -1},
                      {"voice_channel_id": 0}, {"voice_channel_id": -19},
                      {"channel_bitrate_hint_channel_id": 20}):
            with self.subTest(patch=patch):
                result = self.wait(self.body(**patch))
                self.assertEqual(result, {"state": "ignored", "wait_ms": 0.0})
        self.get_channel.assert_not_called()

    def test_missing_hint_takes_fast_path_through_play_and_discards_forged_timings(self):
        body = {"guild_id": 1, "voice_channel_id": 19,
                "_channel_bitrate_received_monotonic": -123,
                "_channel_bitrate_wait_ms": 900000}
        result = asyncio.run(self.agent._dispatch_action(body, "play"))
        self.assertTrue(result["ok"])
        passed = self.agent.cmd_play.call_args.args[0]
        self.assertNotIn("_channel_bitrate_received_monotonic", passed)
        self.assertNotIn("_channel_bitrate_wait_ms", passed)
        self.assertEqual(passed["_agent_action"], "play")
        self.get_channel.assert_not_called()
        self.assertEqual(body["_channel_bitrate_wait_ms"], 900000)

    def test_cache_ready_or_above_target_returns_immediately_without_modification(self):
        for bitrate in (256000, 384000):
            self.channel.bitrate = bitrate
            before = time.monotonic()
            result = self.wait(self.body())
            self.assertEqual(result["state"], "ready")
            self.assertLess(time.monotonic() - before, .03)
            self.assertEqual(self.channel.bitrate, bitrate)

    def test_delayed_gateway_update_is_observed_before_play(self):
        async def scenario():
            async def update_cache():
                await asyncio.sleep(.01)
                self.channel.bitrate = 256000
            updater = asyncio.create_task(update_cache())
            before = time.monotonic()
            result = await self.agent._dispatch_action(self.body(
                _channel_bitrate_received_monotonic=-100,
                _channel_bitrate_wait_ms=999999), "play")
            elapsed = (time.monotonic() - before) * 1000
            await updater
            return result, elapsed, before
        result, elapsed, before = asyncio.run(scenario())
        self.assertTrue(result["ok"])
        passed = self.agent.cmd_play.call_args.args[0]
        self.assertGreaterEqual(passed["_channel_bitrate_received_monotonic"], before)
        self.assertGreaterEqual(passed["_channel_bitrate_wait_ms"], 10)
        self.assertLess(passed["_channel_bitrate_wait_ms"], 100)
        self.assertLessEqual(passed["_channel_bitrate_wait_ms"], elapsed + 2)
        self.assertEqual(self.channel.bitrate, 256000)

    def test_timeout_keeps_authoritative_old_cap_and_allows_pcm_start(self):
        result = asyncio.run(self.agent._dispatch_action(self.body(), "play"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.channel.bitrate, 96000)
        self.agent.cmd_play.assert_awaited_once()
        passed = self.agent.cmd_play.call_args.args[0]
        self.assertGreaterEqual(passed["_channel_bitrate_wait_ms"], 30)
        self.assertLess(passed["_channel_bitrate_wait_ms"], 100)
        self.assertEqual(self.agent.log.call_args.kwargs["state"], "timeout")
        self.assertEqual(self.agent.log.call_args.kwargs["observed_bps"], 96000)

    def test_wrong_cached_guild_channel_or_missing_channel_is_ignored(self):
        for channel in (None, SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1)),
                        SimpleNamespace(id=19, bitrate=256000, guild=SimpleNamespace(id=2))):
            with self.subTest(channel=channel):
                self.channels[19] = channel
                result = self.wait(self.body())
                self.assertEqual(result["state"], "ignored")
                self.assertLess(result["wait_ms"], 10)

    def test_stop_generation_change_cancels_without_starting_music(self):
        state = GuildMusicState(1)
        self.agent.states[1] = state
        async def scenario():
            async def stop_generation():
                await asyncio.sleep(.01)
                state.playback_token += 1
                state.last_action = "stop"
            update = asyncio.create_task(stop_generation())
            result = await self.agent._dispatch_action(self.body(), "play")
            await update
            return result
        result = asyncio.run(scenario())
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.agent.cmd_play.assert_not_awaited()

    def test_queue_reset_or_replaced_state_cancels(self):
        for change in ("reset", "replace"):
            with self.subTest(change=change):
                state = GuildMusicState(1)
                self.agent.states[1] = state
                async def scenario():
                    async def mutate():
                        await asyncio.sleep(.01)
                        if change == "reset":
                            state.queue_reset_generation += 1
                        else:
                            self.agent.states[1] = GuildMusicState(1)
                    update = asyncio.create_task(mutate())
                    result = await self.agent._wait_channel_bitrate_hint(self.body())
                    await update
                    return result
                self.assertEqual(asyncio.run(scenario())["state"], "cancelled")

    def test_idle_state_created_by_matching_speculative_prepare_is_allowed(self):
        async def scenario():
            async def prewarm():
                await asyncio.sleep(.01)
                self.agent.states[1] = GuildMusicState(1, voice_channel_id=19)
                self.channel.bitrate = 256000
            update = asyncio.create_task(prewarm())
            result = await self.agent._dispatch_action(self.body(), "play")
            await update
            return result
        self.assertTrue(asyncio.run(scenario())["ok"])
        self.agent.cmd_play.assert_awaited_once()

    def test_new_busy_state_while_waiting_cancels_instead_of_claiming_it(self):
        async def scenario():
            async def different_play():
                await asyncio.sleep(.01)
                self.agent.states[1] = GuildMusicState(1, voice_channel_id=19, playback_token=2)
            update = asyncio.create_task(different_play())
            result = await self.agent._wait_channel_bitrate_hint(self.body())
            await update
            return result
        self.assertEqual(asyncio.run(scenario())["state"], "cancelled")

    def test_append_aliases_survive_normal_same_channel_track_advance(self):
        for action in ('play', 'enqueue', 'play_direct', 'enqueue_many', 'queue_many',
                       'add_many', 'playlist', 'enqueue_discord_attachment'):
            with self.subTest(action=action):
                state = GuildMusicState(1, voice_channel_id=19, playback_token=4, status='playing')
                state.last_action = 'play'
                self.agent.states[1] = state
                self.channel.bitrate = 96000
                target = (self.agent.cmd_enqueue_discord_attachment if action == 'enqueue_discord_attachment'
                          else self.agent.cmd_play)
                target.reset_mock()
                async def scenario():
                    async def natural_advance():
                        await asyncio.sleep(.01)
                        state.playback_token += 1
                        self.channel.bitrate = 256000
                    update = asyncio.create_task(natural_advance())
                    result = await self.agent._dispatch_action(self.body(), action)
                    await update
                    return result
                self.assertTrue(asyncio.run(scenario())['ok'])
                target.assert_awaited_once()
                self.assertEqual(state.queue_reset_generation, 0)

    def test_two_append_requests_waiting_on_one_channel_are_both_dispatched(self):
        async def accepted(body):
            self.agent.states.setdefault(1, GuildMusicState(1, voice_channel_id=19,
                                                          playback_token=1, status='starting'))
            self.agent.states[1].last_action = 'play'
            await asyncio.sleep(0)
            return {'ok': True}
        self.agent.cmd_play.side_effect = accepted
        async def scenario():
            async def gateway_update():
                await asyncio.sleep(.01)
                self.channel.bitrate = 256000
            update = asyncio.create_task(gateway_update())
            results = await asyncio.gather(
                self.agent._dispatch_action(self.body(), 'play'),
                self.agent._dispatch_action(self.body(), 'enqueue'))
            await update
            return results
        self.assertTrue(all(result['ok'] for result in asyncio.run(scenario())))
        self.assertEqual(self.agent.cmd_play.await_count, 2)

    def test_skip_and_queue_jump_keep_strict_track_generation_checks(self):
        for action, target in (('skip', self.agent.cmd_skip),
                               ('queue_jump', self.agent.cmd_queue_play_now)):
            with self.subTest(action=action):
                state = GuildMusicState(1, voice_channel_id=19, playback_token=4, status='playing')
                state.last_action = 'play'
                self.agent.states[1] = state
                self.channel.bitrate = 96000
                async def scenario():
                    async def natural_advance():
                        await asyncio.sleep(.01)
                        state.playback_token += 1
                        self.channel.bitrate = 256000
                    update = asyncio.create_task(natural_advance())
                    result = await self.agent._dispatch_action(self.body(), action)
                    await update
                    return result
                self.assertTrue(asyncio.run(scenario())['cancelled'])
                target.assert_not_awaited()

    def test_append_does_not_adopt_replacement_of_an_existing_session(self):
        self.agent.states[1] = GuildMusicState(1, voice_channel_id=19, playback_token=4)
        async def scenario():
            async def replace_session():
                await asyncio.sleep(.01)
                self.agent.states[1] = GuildMusicState(1, voice_channel_id=19, playback_token=5)
                self.channel.bitrate = 256000
            update = asyncio.create_task(replace_session())
            result = await self.agent._dispatch_action(self.body(), 'enqueue')
            await update
            return result
        self.assertTrue(asyncio.run(scenario())['cancelled'])
        self.agent.cmd_play.assert_not_awaited()

    def test_append_still_cancels_on_stop_or_clear_even_if_another_play_follows(self):
        for destructive in ('stop', 'queue_clear'):
            with self.subTest(destructive=destructive):
                state = GuildMusicState(1, voice_channel_id=19, playback_token=4)
                self.agent.states[1] = state
                self.channel.bitrate = 96000
                async def scenario():
                    async def reset_then_play():
                        await asyncio.sleep(.01)
                        state.queue_reset_generation += 1
                        state.last_action = destructive
                        state.playback_token += 1
                        state.last_action = 'play'
                        self.channel.bitrate = 256000
                    update = asyncio.create_task(reset_then_play())
                    result = await self.agent._dispatch_action(self.body(), 'enqueue')
                    await update
                    return result
                self.assertTrue(asyncio.run(scenario())['cancelled'])
                self.agent.cmd_play.assert_not_awaited()

    def test_append_does_not_adopt_new_session_with_a_prior_stop_or_clear(self):
        async def scenario():
            async def stopped_then_played():
                await asyncio.sleep(.01)
                self.agent.states[1] = GuildMusicState(1, voice_channel_id=19, playback_token=2,
                                                      queue_reset_generation=1, status='playing')
                self.agent.states[1].last_action = 'play'
                self.channel.bitrate = 256000
            update = asyncio.create_task(stopped_then_played())
            result = await self.agent._dispatch_action(self.body(), 'enqueue')
            await update
            return result
        self.assertTrue(asyncio.run(scenario())['cancelled'])
        self.agent.cmd_play.assert_not_awaited()

    def test_idle_prewarm_for_another_channel_is_not_adopted(self):
        async def scenario():
            async def different_prewarm():
                await asyncio.sleep(.01)
                self.agent.states[1] = GuildMusicState(1, voice_channel_id=20)
                self.channel.bitrate = 256000
            update = asyncio.create_task(different_prewarm())
            result = await self.agent._dispatch_action(self.body(), "play")
            await update
            return result
        result = asyncio.run(scenario())
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.agent.cmd_play.assert_not_awaited()

    def test_voice_channel_move_without_token_change_cancels_old_hint(self):
        state = GuildMusicState(1, voice_channel_id=19)
        self.agent.states[1] = state
        async def scenario():
            async def channel_move():
                await asyncio.sleep(.01)
                state.voice_channel_id = 20
                self.channel.bitrate = 256000
            update = asyncio.create_task(channel_move())
            result = await self.agent._dispatch_action(self.body(), "play")
            await update
            return result
        result = asyncio.run(scenario())
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.agent.cmd_play.assert_not_awaited()

    def test_legitimate_move_from_idle_old_channel_to_requested_channel_is_allowed(self):
        self.agent.states[1] = GuildMusicState(1, voice_channel_id=19)
        requested = SimpleNamespace(id=20, bitrate=96000, guild=SimpleNamespace(id=1))
        self.channels[20] = requested
        async def scenario():
            async def gateway_update():
                await asyncio.sleep(.01)
                requested.bitrate = 256000
            update = asyncio.create_task(gateway_update())
            result = await self.agent._dispatch_action(self.body(
                voice_channel_id=20, channel_bitrate_hint_channel_id=20), "play")
            await update
            return result
        self.assertTrue(asyncio.run(scenario())["ok"])
        self.agent.cmd_play.assert_awaited_once()

    def test_requested_move_is_cancelled_if_another_action_moves_to_third_channel(self):
        state = GuildMusicState(1, voice_channel_id=19)
        self.agent.states[1] = state
        requested = SimpleNamespace(id=20, bitrate=96000, guild=SimpleNamespace(id=1))
        self.channels[20] = requested
        async def scenario():
            async def another_move():
                await asyncio.sleep(.01)
                state.voice_channel_id = 21
                requested.bitrate = 256000
            update = asyncio.create_task(another_move())
            result = await self.agent._dispatch_action(self.body(
                voice_channel_id=20, channel_bitrate_hint_channel_id=20), "play")
            await update
            return result
        result = asyncio.run(scenario())
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.agent.cmd_play.assert_not_awaited()

    def test_hint_wait_is_clamped_to_one_and_half_seconds_without_real_sleep(self):
        self.duration = 99
        clock = SimpleNamespace(value=0.0)
        async def sleep(seconds):
            clock.value += seconds
        fake_time = SimpleNamespace(monotonic=lambda: clock.value)
        fake_asyncio = SimpleNamespace(sleep=sleep)
        with mock.patch.dict(self.globals, {"time": fake_time, "asyncio": fake_asyncio}):
            result = self.wait(self.body())
        self.assertEqual(result["state"], "timeout")
        self.assertAlmostEqual(result["wait_ms"], 1500, delta=.1)
        self.assertEqual(self.channel.bitrate, 96000)

    def test_negative_wait_configuration_is_zero(self):
        self.duration = -1
        result = self.wait(self.body())
        self.assertEqual(result["state"], "timeout")
        self.assertLess(result["wait_ms"], 5)

    def test_archive_executor_rejects_startup_before_any_cache_wait(self):
        self.agent.executor_mode = "archive"
        result = asyncio.run(self.agent._dispatch_action(self.body(), "play"))
        self.assertFalse(result["ok"])
        self.get_channel.assert_not_called()
        self.agent.cmd_play.assert_not_awaited()

    def test_virtual_queue_remove_and_move_ignore_startup_hint(self):
        for alias in ('queue_virtual_action', 'virtual_queue_action'):
            for operation in ('remove', 'move'):
                with self.subTest(action=alias, operation=operation):
                    result = asyncio.run(self.agent._dispatch_action(
                        self.body(virtual_action=operation), alias))
                    self.assertTrue(result['ok'])
        self.assertEqual(self.agent.cmd_queue_virtual_action.await_count, 4)
        self.get_channel.assert_not_called()

    def test_virtual_queue_play_now_observes_channel_cache_for_both_aliases(self):
        self.channel.bitrate = 256000
        for alias in ('queue_virtual_action', 'virtual_queue_action'):
            with self.subTest(action=alias):
                result = asyncio.run(self.agent._dispatch_action(
                    self.body(virtual_action='play_now'), alias))
                self.assertTrue(result['ok'])
        self.assertEqual(self.agent.cmd_queue_virtual_action.await_count, 2)
        self.assertEqual(self.get_channel.call_count, 2)


if __name__ == "__main__":
    unittest.main()
