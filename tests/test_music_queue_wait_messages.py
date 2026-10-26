"""Queue acknowledgements through the actual command formatter and watcher.

The production methods are compiled from their AST. Discord message editing,
the existing title-watcher clock and remote status boundaries are represented
by explicit doubles. This does not exercise a live Discord API or voice call.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.nucleo.modelos import MusicTrack


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "cogs/musica/comandos/tocar.py"


def production_command():
    from cogs.musica.reproducao.estimativa_fila import queue_wait_line

    tree = ast.parse(SOURCE.read_text())
    original = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "FluxoTocar")
    wanted = {"_music_agent_play_message", "_music_agent_confirmed_playing",
              "_music_agent_guild_state", "_watch_queued_music_agent_title"}
    methods = [node for node in original.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    assert {node.name for node in methods} == wanted
    klass = ast.ClassDef(name="Command", bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    namespace = dict(asyncio=asyncio, contextlib=contextlib,
                     config=SimpleNamespace(MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS=5),
                     logger=mock.Mock(), queue_wait_line=queue_wait_line,
                     MusicTrack=MusicTrack,
                     monitor_music_agent_ativo=mock.Mock(return_value=True),
                     estado_local_music_agent=mock.Mock(),
                     music_agent_status=mock.AsyncMock())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, klass], type_ignores=[])),
                 str(SOURCE), "exec"), namespace)
    return namespace["Command"], namespace


def queued_result(*, title="Labyrinth", duration=153):
    previous = {"queue_item_id": "prior", "title": "Earlier song", "duration": 190}
    target = {"queue_item_id": "target", "title": title, "duration": duration}
    return {
        "ok": True, "queued": True, "track": target,
        "state": {
            "status": "playing", "paused": False, "loop_mode": "off",
            "confirmed_playing": True, "voice_connected": True, "player_present": True,
            "position_ms": 110000, "speed_multiplier": 1.0,
            "current": {"queue_item_id": "current", "title": "CURRENT must not be acknowledged",
                        "duration": 180, "is_live": False},
            "queue_size": 2, "queue": [previous, target],
            "queue_layout": [{"kind": "track", "track": previous},
                             {"kind": "track", "track": target}],
        },
    }


class QueueWaitCommandMessageTests(unittest.TestCase):
    def setUp(self):
        Command, self.namespace = production_command()
        self.command = Command()
        self.command.router = SimpleNamespace(get_state=mock.Mock(return_value=None))
        self.track = MusicTrack("Local fallback", "https://example.invalid/target", 1,
                                duration=999, queue_item_id="target")

    def render(self, result, **kwargs):
        return self.command._music_agent_play_message(self.track, result, **kwargs)

    def test_added_message_uses_target_title_and_duration_and_excludes_its_duration_from_wait(self):
        result = queued_result()
        before = deepcopy(result)
        message = self.render(result)
        self.assertIn("Adicionada à fila:", message)
        self.assertIn("Labyrinth", message)
        self.assertIn("`2:33`", message)
        self.assertIn("Começa em aproximadamente **4:20**", message)
        self.assertNotIn("CURRENT must not be acknowledged", message)
        self.assertNotIn("16:39", message)
        self.assertEqual(result, before)
        self.namespace["music_agent_status"].assert_not_called()
        self.namespace["estado_local_music_agent"].assert_not_called()

    def test_same_title_entries_use_identity_and_stop_before_the_added_item(self):
        result = queued_result()
        result["state"]["queue_layout"][0]["track"]["title"] = "Labyrinth"
        result["state"]["queue_layout"].append({
            "kind": "track", "track": {"queue_item_id": "after", "title": "Labyrinth", "duration": 900}})
        self.assertIn("**4:20**", self.render(result))

    def test_full_layout_is_used_for_target_beyond_the_queue_preview(self):
        result = queued_result()
        leading = [{"queue_item_id": f"before-{i}", "title": "Repeated", "duration": 1}
                   for i in range(60)]
        result["state"]["queue"] = leading[:50]
        result["state"]["queue_size"] = 61
        result["state"]["queue_layout"] = [
            *({"kind": "track", "track": value} for value in leading),
            {"kind": "track", "track": result["track"]},
        ]
        self.assertIn("**2:10**", self.render(result))

    def test_preexisting_preview_fallback_still_uses_the_queued_entry(self):
        result = queued_result()
        result.pop("track")
        message = self.render(result)
        self.assertIn("Labyrinth", message)
        self.assertIn("**4:20**", message)

    def test_preview_last_other_identity_cannot_replace_selected_track_title(self):
        result = queued_result()
        result.pop("track")
        result["state"]["queue"] = [{
            "queue_item_id": "different", "title": "WRONG preview last entry", "duration": 999}]
        message = self.render(result)
        self.assertIn("Local fallback", message)
        self.assertIn("**4:20**", message)
        self.assertNotIn("WRONG", message)
        self.assertNotIn("CURRENT must not be acknowledged", message)

    def test_provisional_title_keeps_the_wait_line(self):
        result = queued_result(title="YouTube", duration=None)
        self.track = MusicTrack("YouTube", "https://example.invalid/target", 1,
                                queue_item_id="target")
        message = self.render(result)
        self.assertIn("Carregando detalhes...", message)
        self.assertIn("**4:20**", message)
        self.assertNotIn("CURRENT must not be acknowledged", message)

    def test_missing_previous_duration_produces_unavailable_instead_of_a_partial_number(self):
        result = queued_result()
        result["state"]["queue_layout"][0]["track"]["duration"] = None
        message = self.render(result)
        self.assertIn("Previsão indisponível", message)
        self.assertNotIn("**1:10**", message)

    def test_wait_line_override_preserves_the_original_snapshot(self):
        result = queued_result()
        result["state"]["position_ms"] = 175000
        result["state"]["queue_layout"] = [{"kind": "track", "track": result["track"]}]
        original_line = "⏳ Começa em aproximadamente **4:20**"
        message = self.render(result, wait_line=original_line)
        self.assertTrue(message.endswith(original_line))
        self.assertNotIn("**0:05**", message)

    def test_optional_local_duration_cache_reaches_the_shared_estimator(self):
        helper = mock.Mock(return_value="⏳ Previsão indisponível")
        self.namespace["queue_wait_line"] = helper
        cache = SimpleNamespace(agent_virtual_duration_cache={})
        result = queued_result()
        self.render(result, duration_state=cache)
        helper.assert_called_once()
        args, kwargs = helper.call_args
        self.assertIs(args[0], result["state"])
        self.assertIs(args[1], result["track"])
        self.assertIs(kwargs["duration_state"], cache)

    def test_playing_and_starting_messages_do_not_receive_queue_wait(self):
        for state, prefix in [
            ({"status": "playing", "confirmed_playing": True}, "Tocando:"),
            ({"status": "preparing"}, "Preparando para tocar:"),
        ]:
            with self.subTest(state=state):
                result = {"state": {**state, "current": {"title": "Current", "duration": 234}}}
                message = self.render(result)
                self.assertIn(prefix, message)
                self.assertIn("Current", message)
                self.assertIn("`3:54`", message)
                self.assertNotIn("⏳", message)
                self.assertNotIn("Previsão", message)

    def test_existing_failure_sanitization_remains_in_place(self):
        message = self.render({"state": {"status": "failed", "last_error": "Music Agent unauthorized"}})
        self.assertIn("backend musical ainda não está pronto", message)
        self.assertNotIn("unauthorized", message)
        self.assertNotIn("⏳", message)


class QueueTitleWatcherMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Command, self.namespace = production_command()
        self.command = Command()
        self.command.router = SimpleNamespace(get_state=mock.Mock(return_value=SimpleNamespace()))
        self.track = MusicTrack("YouTube", "https://example.invalid/target", 1,
                                queue_item_id="target")
        self.clock = 0.0

        async def step_sleep(seconds):
            self.clock += seconds

        self.namespace["asyncio"] = SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: self.clock),
            sleep=step_sleep)
        self.wait_line = "⏳ Começa em aproximadamente **4:20**"
        # Title enrichment may read newer metadata, but cannot recalculate an
        # acknowledgement's inclusion-time estimate.
        self.namespace["queue_wait_line"] = mock.Mock(side_effect=AssertionError("ETA was recalculated"))
        self.message = SimpleNamespace(
            content="`🎶` **Música adicionada à fila.** Carregando detalhes...\n" + self.wait_line,
            edit=mock.AsyncMock())

    async def watch(self, **kwargs):
        await self.command._watch_queued_music_agent_title(
            self.message, 1, self.track, "target", wait_line=self.wait_line, **kwargs)

    async def test_late_title_edit_preserves_static_wait_even_when_live_state_has_changed(self):
        updated = queued_result(title="Actual resolved title")
        updated["state"]["position_ms"] = 175000
        updated["state"]["queue"] = [updated["track"]]
        updated["state"]["queue_layout"] = [{"kind": "track", "track": updated["track"]}]
        self.namespace["estado_local_music_agent"].return_value = updated["state"]
        await self.watch()
        content = self.message.edit.await_args.kwargs["content"]
        self.assertIn("Actual resolved title", content)
        self.assertIn("`2:33`", content)
        self.assertTrue(content.endswith(self.wait_line))
        self.assertNotIn("**0:05**", content)
        self.message.edit.assert_awaited_once()
        self.namespace["music_agent_status"].assert_not_called()

    async def test_existing_remote_status_path_preserves_wait_without_an_extra_request(self):
        updated = queued_result(title="Resolved remotely")
        self.namespace["monitor_music_agent_ativo"].return_value = False
        self.namespace["music_agent_status"].return_value = {"guilds": {"1": updated["state"]}}
        await self.watch()
        self.namespace["music_agent_status"].assert_awaited_once()
        self.namespace["estado_local_music_agent"].assert_not_called()
        self.assertTrue(self.message.edit.await_args.kwargs["content"].endswith(self.wait_line))

    async def test_timed_out_title_still_keeps_original_wait(self):
        self.namespace["estado_local_music_agent"].return_value = {"queue": [], "current": {}}
        await self.watch()
        content = self.message.edit.await_args.kwargs["content"]
        self.assertIn("Música adicionada à fila", content)
        self.assertTrue(content.endswith(self.wait_line))
        self.assertNotIn("Carregando detalhes...", content)
        self.message.edit.assert_awaited_once()
        self.namespace["music_agent_status"].assert_not_called()

    async def test_wrong_same_title_entry_is_not_used_for_target(self):
        updated = queued_result(title="Resolved target")
        updated["state"]["queue"] = [
            {"queue_item_id": "wrong", "title": "WRONG exact same media", "duration": 999},
            updated["track"],
        ]
        self.namespace["estado_local_music_agent"].return_value = updated["state"]
        await self.watch()
        content = self.message.edit.await_args.kwargs["content"]
        self.assertIn("Resolved target", content)
        self.assertNotIn("WRONG", content)

    async def test_compact_monitor_uses_existing_typed_queue_without_a_status_request(self):
        resolved = MusicTrack("Resolved from typed mirror", "https://example.invalid/target", 1,
                              duration=153, queue_item_id="target")
        self.command.router.get_state.return_value = SimpleNamespace(
            agent_queue_layout=[{"kind": "track", "track": resolved}],
            current=None, forward_queue=[], queue=SimpleNamespace(_queue=[]))
        self.namespace["estado_local_music_agent"].return_value = {"status": "playing"}
        await self.watch()
        content = self.message.edit.await_args.kwargs["content"]
        self.assertIn("Resolved from typed mirror", content)
        self.assertTrue(content.endswith(self.wait_line))
        self.namespace["music_agent_status"].assert_not_called()
        self.command.router.get_state.assert_called_once_with(1)

    async def test_older_watcher_call_preserves_wait_line_already_displayed_in_message(self):
        updated = queued_result(title="Resolved without explicit override")
        self.namespace["estado_local_music_agent"].return_value = updated["state"]
        await self.command._watch_queued_music_agent_title(self.message, 1, self.track, "target")
        content = self.message.edit.await_args.kwargs["content"]
        self.assertIn("Resolved without explicit override", content)
        self.assertTrue(content.endswith(self.wait_line))


if __name__ == "__main__":
    unittest.main()
