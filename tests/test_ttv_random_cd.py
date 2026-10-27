"""Exercise personal shuffle persistence, routing and fresh synthesis choices."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cogs.tts import random_cd
from cogs.tts.aliases import get_prefixed_aliases
from cogs.tts.prefix import match_prefix_control_command, dispatch_prefix_control_command
from cogs.tts.melodies import DEFAULT_MELODY_ID, get_melody, list_melodies

ROOT = Path(__file__).resolve().parents[1]


def settings_db():
    motor = ModuleType("motor")
    backend = ModuleType("motor.motor_asyncio")
    backend.AsyncIOMotorClient = object
    motor.motor_asyncio = backend
    with patch.dict(sys.modules, {"motor": motor, "motor.motor_asyncio": backend}):
        spec = importlib.util.spec_from_file_location("shuffle_test_settings", ROOT / "db.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    db = module.SettingsDB.__new__(module.SettingsDB)
    db.user_cache, db.guild_cache, db._resolved_tts_cache = {}, {}, {}
    db.coll = SimpleNamespace(update_one=AsyncMock(), delete_one=AsyncMock())
    return db


DISCORD = SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: "no-mentions"),
    utils=SimpleNamespace(escape_markdown=lambda text: text))


class ShuffleSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cog = SimpleNamespace()
        self.pool = tuple(SimpleNamespace(id=key) for key in (DEFAULT_MELODY_ID, "one", "two", "three"))

    def choose(self, guild=1, user=2, **extra):
        return random_cd.choose_synthesis_melody(self.cog, guild, user,
            {"ttv_random_cd": True, "ttv_melody_id": "one", **extra})

    def test_fixed_mode_does_not_read_catalog_or_mutate_history(self):
        with patch.object(random_cd, "available_random_melodies", side_effect=AssertionError("unneeded draw")):
            self.assertEqual(self.choose(ttv_random_cd=False), "one")
            self.assertEqual(self.choose(ttv_random_cd="true"), "one")
        self.assertFalse(hasattr(self.cog, "_random_cd_cycles"))

    def test_every_cd_plays_once_before_a_new_cycle_without_changing_settings(self):
        values = {"ttv_random_cd": True, "ttv_melody_id": "one", "voice": "unchanged"}
        before = deepcopy(values)
        with patch.object(random_cd, "list_melodies", return_value=self.pool), \
             patch.object(random_cd.random, "choice", side_effect=lambda pool: pool[0]) as draw:
            chosen = [random_cd.choose_synthesis_melody(self.cog, 1, 2, values) for _ in range(9)]
        self.assertEqual(draw.call_count, 9)
        self.assertNotIn(DEFAULT_MELODY_ID, chosen)
        self.assertEqual(chosen[0], "one", "A saved CD has not already played in the random cycle")
        for start in range(0, 9, 3):
            self.assertEqual(set(chosen[start:start + 3]), {"one", "two", "three"})
        self.assertTrue(all(left != right for left, right in zip(chosen, chosen[1:])))
        self.assertEqual(values, before)

    def test_random_draw_only_receives_remaining_cds_in_the_cycle(self):
        offered = []
        def draw(pool):
            offered.append(tuple(item.id for item in pool))
            return pool[-1]
        with patch.object(random_cd, "available_random_melodies", return_value=self.pool[1:]), \
             patch.object(random_cd.random, "choice", side_effect=draw):
            chosen = [self.choose() for _ in range(3)]
        self.assertEqual(chosen, ["three", "two", "one"])
        self.assertEqual(offered, [("one", "two", "three"), ("one", "two"), ("one",)])

    def test_cycle_boundary_does_not_immediately_repeat_its_last_cd(self):
        def draw(pool):
            return pool[-1] if any(item.id == "one" for item in pool) else pool[0]
        with patch.object(random_cd, "available_random_melodies", return_value=self.pool[1:]), \
             patch.object(random_cd.random, "choice", side_effect=draw):
            first_cycle = [self.choose() for _ in range(3)]
            second_cycle = [self.choose() for _ in range(3)]
        self.assertEqual(first_cycle[-1], "one")
        self.assertNotEqual(second_cycle[0], first_cycle[-1])
        self.assertEqual(set(second_cycle), {"one", "two", "three"})

    def test_draw_history_is_personal_to_user_and_server(self):
        with patch.object(random_cd, "available_random_melodies", return_value=self.pool[1:]), \
             patch.object(random_cd.random, "choice", side_effect=lambda pool: pool[0]):
            first = self.choose()
            self.assertNotEqual(self.choose(), first)
            self.assertEqual(self.choose(user=3), first)
            self.assertEqual(self.choose(guild=3), first)

    def test_live_catalog_removal_never_selects_retired_cd(self):
        with patch.object(random_cd, "available_random_melodies", return_value=self.pool[1:]), \
             patch.object(random_cd.random, "choice", side_effect=lambda pool: pool[0]):
            retired = self.choose()
        surviving = tuple(item for item in self.pool[1:] if item.id != retired)
        with patch.object(random_cd, "available_random_melodies", return_value=surviving):
            self.assertIn(self.choose(), {item.id for item in surviving})

    def test_newly_installed_cd_joins_the_current_cycle_before_any_repeat(self):
        with patch.object(random_cd, "available_random_melodies", return_value=self.pool[1:]), \
             patch.object(random_cd.random, "choice", side_effect=lambda pool: pool[0]):
            self.assertEqual(self.choose(), "one")
        expanded = self.pool[1:] + (SimpleNamespace(id="four"),)
        with patch.object(random_cd, "available_random_melodies", return_value=expanded), \
             patch.object(random_cd.random, "choice", side_effect=lambda pool: pool[0]):
            self.assertEqual([self.choose() for _ in range(4)], ["two", "three", "four", "one"])

    def test_catalog_removal_can_finish_the_cycle_and_never_blocks_reset(self):
        with patch.object(random_cd, "available_random_melodies", return_value=self.pool[1:]), \
             patch.object(random_cd.random, "choice", side_effect=lambda pool: pool[0]):
            self.assertEqual([self.choose(), self.choose()], ["one", "two"])
        remaining = self.pool[1:3]
        with patch.object(random_cd, "available_random_melodies", return_value=remaining), \
             patch.object(random_cd.random, "choice", side_effect=lambda pool: pool[0]):
            self.assertEqual([self.choose(), self.choose()], ["one", "two"])

    def test_single_available_cd_and_default_fallback_are_valid(self):
        with patch.object(random_cd, "list_melodies", return_value=self.pool[:1]):
            self.assertEqual(self.choose(), DEFAULT_MELODY_ID)
        with patch.object(random_cd, "list_melodies", return_value=self.pool[1:2]):
            self.assertEqual(self.choose(), "one")
            self.assertEqual(self.choose(), "one")
        with patch.object(random_cd, "list_melodies", return_value=()):
            with self.assertRaisesRegex(ValueError, "Nenhum CD"):
                self.choose()

    def test_history_is_bounded_without_affecting_other_recent_users(self):
        with patch.object(random_cd, "_HISTORY_LIMIT", 2), \
             patch.object(random_cd, "available_random_melodies", return_value=self.pool[1:]):
            for user in (2, 3, 4):
                self.choose(user=user)
        self.assertEqual(set(self.cog._random_cd_cycles), {(1, 3), (1, 4)})

    def test_prefix_honors_custom_prefix_and_rejects_arguments(self):
        for prefix in ("_", "!", "os!"):
            with self.subTest(prefix=prefix):
                self.assertEqual(get_prefixed_aliases(prefix, "rcd"), (prefix + "rcd",))
                self.assertEqual(match_prefix_control_command(prefix + "RCD", prefix).kind, "rcd")
                self.assertIsNone(match_prefix_control_command(prefix + "rcd now", prefix))
                self.assertIsNone(match_prefix_control_command(prefix + "rcdmore", prefix))


class ShufflePersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = settings_db()

    async def test_toggle_persists_through_reload_and_preserves_other_preferences(self):
        original = {"voice": "EdgeVoice", "ttv_language": "en-US", "future_preference": [1, 2]}
        self.db.user_cache[(1, 2)] = {"tts": deepcopy(original), "chips": 45}
        self.db.user_cache[(1, 3)] = {"tts": {"ttv_random_cd": True}}
        other = deepcopy(self.db.user_cache[(1, 3)])
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)
        await self.db.set_user_tts(1, 2, ttv_random_cd=True)
        stored = deepcopy(self.db.coll.update_one.await_args.args[1]["$set"])
        self.assertEqual(stored["tts"], {**original, "ttv_random_cd": True})
        self.assertEqual(stored["chips"], 45)
        self.assertEqual(self.db.user_cache[(1, 3)], other)
        async def cursor():
            yield stored
        self.db.coll.find = lambda *_: cursor()
        await self.db.load_cache()
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], True)
        self.assertEqual(self.db.get_user_tts(1, 2)["voice"], "EdgeVoice")

    async def test_invalid_boolean_rejects_whole_edit_before_persistence(self):
        self.db.user_cache[(1, 2)] = {"tts": {"voice": "old"}}
        before = deepcopy(self.db.user_cache)
        for invalid in ("true", "false", 0, 1, [], {}):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                await self.db.set_user_tts(1, 2, ttv_random_cd=invalid, voice="new")
        self.assertEqual(self.db.user_cache, before)
        self.db.coll.update_one.assert_not_awaited()

    async def test_failed_save_does_not_publish_enabled_mode_in_cache(self):
        self.db.user_cache[(1, 2)] = {"tts": {"voice": "old"}}
        before = self.db.resolve_tts(1, 2)
        self.db.coll.update_one.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            await self.db.set_user_tts(1, 2, ttv_random_cd=True)
        self.assertEqual(self.db.resolve_tts(1, 2), before)
        self.assertNotIn("ttv_random_cd", self.db.user_cache[(1, 2)]["tts"])

    async def test_explicit_cd_choice_disables_shuffle_but_voice_edits_do_not(self):
        await self.db.set_user_tts(1, 2, ttv_random_cd=True)
        await self.db.set_user_tts(1, 2, ttv_language="en-US")
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], True)
        selected = list_melodies()[1].id
        await self.db.set_user_tts(1, 2, ttv_melody_id=selected)
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)
        self.assertEqual(self.db.resolve_tts(1, 2)["ttv_melody_id"], selected)
        self.assertEqual(self.db.resolve_tts(1, 2)["ttv_language"], "en-US")

    async def test_reset_removes_personal_shuffle(self):
        await self.db.set_user_tts(1, 2, ttv_random_cd=True)
        await self.db.reset_user_tts(1, 2)
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)

    def cog_and_message(self):
        async def update(guild, user, **kwargs):
            await self.db.set_user_tts(guild, user, **kwargs)
        cog = SimpleNamespace(_get_db=lambda: self.db, _set_user_tts_and_refresh=update)
        message = SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2), reply=AsyncMock())
        return cog, message

    async def test_command_toggles_only_invoking_user_and_replies_compactly(self):
        cog, message = self.cog_and_message()
        with patch.dict(sys.modules, {"discord": DISCORD}):
            self.assertTrue(await random_cd.toggle_random_cd(cog, message))
            self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], True)
            on = message.reply.await_args.kwargs
            self.assertNotIn("embed", on)
            self.assertNotIn("embeds", on)
            self.assertIsInstance(on["content"], str)
            self.assertLess(len(on["content"]), 200)
            self.assertIn("repete", on["content"].lower())
            self.assertEqual(on["allowed_mentions"], "no-mentions")
            self.assertIs(on["mention_author"], False)
            self.assertTrue(await random_cd.toggle_random_cd(cog, message))
            off = message.reply.await_args.kwargs
            self.assertNotIn("embed", off)
            self.assertLess(len(off["content"]), 200)
            self.assertIn("desativados", off["content"].lower())
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)
        self.assertNotIn((1, 3), self.db.user_cache)
        self.assertEqual(message.reply.await_count, 2)

    async def test_command_failure_cannot_claim_shuffle_was_enabled(self):
        cog, message = self.cog_and_message()
        self.db.coll.update_one.side_effect = RuntimeError("database unavailable")
        with patch.dict(sys.modules, {"discord": DISCORD}):
            self.assertTrue(await random_cd.toggle_random_cd(cog, message))
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)
        failed = message.reply.await_args.kwargs
        self.assertNotIn("embed", failed)
        self.assertIn("Não consegui", failed["content"])

    async def test_toggling_off_and_on_keeps_the_unfinished_cycle(self):
        cog, message = self.cog_and_message()
        pool = tuple(SimpleNamespace(id=key) for key in ("one", "two", "three"))
        def draw():
            return random_cd.choose_synthesis_melody(cog, 1, 2, self.db.resolve_tts(1, 2))
        with patch.dict(sys.modules, {"discord": DISCORD}), \
             patch.object(random_cd, "available_random_melodies", return_value=pool), \
             patch.object(random_cd.random, "choice", side_effect=lambda choices: choices[0]):
            await random_cd.toggle_random_cd(cog, message)
            self.assertEqual(draw(), "one")
            await random_cd.toggle_random_cd(cog, message)
            await random_cd.toggle_random_cd(cog, message)
            self.assertEqual([draw(), draw(), draw()], ["two", "three", "one"])

    async def test_missing_fixed_cd_still_gets_a_short_text_confirmation(self):
        cog, message = self.cog_and_message()
        self.db.user_cache[(1, 2)] = {"tts": {
            "ttv_random_cd": True, "ttv_melody_id": "removed"}}
        with patch.dict(sys.modules, {"discord": DISCORD}):
            await random_cd.toggle_random_cd(cog, message)
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)
        response = message.reply.await_args.kwargs
        self.assertNotIn("embed", response)
        self.assertIn("desativados", response["content"].lower())
        self.assertLess(len(response["content"]), 200)

    async def test_plain_text_reply_falls_back_to_channel_without_mentions(self):
        message = SimpleNamespace(channel=SimpleNamespace(send=AsyncMock()))
        with patch.dict(sys.modules, {"discord": DISCORD}):
            await random_cd._reply(message, "🎲 CDs aleatórios ligados.")
        message.channel.send.assert_awaited_once_with(
            content="🎲 CDs aleatórios ligados.", allowed_mentions="no-mentions")

    async def test_concurrent_commands_serialize_toggles_for_one_person(self):
        cog, message = self.cog_and_message()
        started, release = asyncio.Event(), asyncio.Event()
        async def persist(*_args, **_kwargs):
            if self.db.coll.update_one.await_count == 1:
                started.set()
                await release.wait()
        self.db.coll.update_one.side_effect = persist
        with patch.dict(sys.modules, {"discord": DISCORD}):
            first = asyncio.create_task(random_cd.toggle_random_cd(cog, message))
            await started.wait()
            second = asyncio.create_task(random_cd.toggle_random_cd(cog, message))
            await asyncio.sleep(0)
            self.assertEqual(self.db.coll.update_one.await_count, 1)
            release.set()
            await asyncio.gather(first, second)
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)
        self.assertEqual(self.db.coll.update_one.await_count, 2)

    async def test_dispatch_awaits_random_command_once(self):
        with patch.object(random_cd, "toggle_random_cd", new=AsyncMock(return_value=True)) as action:
            self.assertTrue(await dispatch_prefix_control_command(object(), object(),
                match_prefix_control_command("_rcd", "_")))
        action.assert_awaited_once()

    async def test_mode_change_refreshes_only_owners_live_cd_panel(self):
        cog = SimpleNamespace()
        def panel(guild, owner, *, expired=False):
            return SimpleNamespace(cog=cog, guild_id=guild, owner_id=owner,
                _is_expired=lambda: expired, refresh=AsyncMock(), _edit_panel=AsyncMock())
        own, other_user, other_guild, expired = panel(1, 2), panel(1, 3), panel(3, 2), panel(1, 2, expired=True)
        module = SimpleNamespace(_ACTIVE_PANELS=[own, other_user, other_guild, expired])
        with patch.dict(sys.modules, {"cogs.tts.interface.painel_canto": module}):
            await random_cd._refresh_open_panels(cog, 1, 2)
        own.refresh.assert_awaited_once_with(force=True)
        own._edit_panel.assert_awaited_once()
        for item in (other_user, other_guild, expired):
            item.refresh.assert_not_awaited()

    async def test_open_panel_can_restore_the_same_cd_when_shuffle_is_enabled(self):
        path = ROOT / "cogs/tts/interface/painel_canto.py"
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and any(isinstance(child, ast.AsyncFunctionDef) and child.name == "apply_melody" for child in node.body))
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "apply_melody")
        ns = {"asyncio": asyncio, "discord": SimpleNamespace(Interaction=object),
              "_reply": AsyncMock(), "get_melody": get_melody}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), ns)
        selected = list_melodies()[1].id
        await self.db.set_user_tts(1, 2, ttv_melody_id=selected, ttv_random_cd=True)
        cog, _ = self.cog_and_message()
        panel = SimpleNamespace(interaction_check=AsyncMock(return_value=True), _saving=False,
            _preview_running=False, _settings_available=True, _random_cd_enabled=True,
            selected_melody_id=selected, active_melody_id=selected, guild_id=1, owner_id=2,
            cog=cog, _preferences={}, refresh=AsyncMock(), _edit_panel=AsyncMock())
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        await ns["apply_melody"](panel, interaction)
        self.assertIs(self.db.resolve_tts(1, 2)["ttv_random_cd"], False)
        self.assertEqual(self.db.resolve_tts(1, 2)["ttv_melody_id"], selected)
        ns["_reply"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
