from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class _Component:
    def __init__(self, *children, **kwargs):
        self.children = list(children)
        self.__dict__.update(kwargs)
        self.value = "0"

    def add_item(self, item):
        self.children.append(item)

    def clear_items(self):
        self.children.clear()

    def add_option(self, **kwargs):
        if kwargs.get("default"):
            self.value = kwargs["value"]


class _Modal(_Component):
    def __init_subclass__(cls, *, title=None, **kwargs):
        super().__init_subclass__(**kwargs)


class _Layout(_Component):
    def __init__(self, cog, owner_id, guild_id):
        super().__init__()
        self.cog, self.owner_id, self.guild_id = cog, owner_id, guild_id

    def _is_expired(self):
        return False


def _panel_module():
    discord = types.ModuleType("discord")
    discord.ui = types.SimpleNamespace(
        Modal=_Modal, RadioGroup=_Component, Label=_Component, Button=_Component,
        Container=_Component, TextDisplay=_Component, Separator=_Component,
        ActionRow=_Component,
    )
    discord.ButtonStyle = types.SimpleNamespace(primary=1, secondary=2)
    base = types.ModuleType("cogs.tts.interface.visoes_layout")
    base.VisaoLayoutBaseTTS = _Layout
    ownership = types.ModuleType("cogs.tts.interface.visoes_base")

    async def responder_painel_tts_de_outro(interaction):
        await interaction.response.send_message("Esse painel neh seu não djacho!", ephemeral=True)

    ownership.responder_painel_tts_de_outro = responder_painel_tts_de_outro
    name = "cogs.tts.interface._advanced_panel_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "cogs/tts/interface/visao_efeitos_avancados.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"discord": discord, base.__name__: base, ownership.__name__: ownership, name: module}):
        spec.loader.exec_module(module)
    return module


def _settings_probe():
    # Exercita os helpers reais sem iniciar bot, Mongo ou os provedores TTS.
    tree = ast.parse((ROOT / "cogs/tts/cog.py").read_text(encoding="utf-8"))
    cog = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSVoice")
    names = {"_set_user_tts_and_refresh", "_notify_status_views_changed"}
    probe = ast.ClassDef(
        name="Probe", bases=[], keywords=[], decorator_list=[],
        body=[node for node in cog.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names],
    )
    namespace = {"asyncio": asyncio, "logger": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])), "tts-settings", "exec"), namespace)
    instance = namespace["Probe"]()
    instance._status_views_by_target = {}
    instance._status_refresh_locks = {}
    instance._status_refresh_tasks = {}
    instance._status_refresh_pending = set()
    instance._schedule_tts_background = Mock(side_effect=asyncio.create_task)
    instance._maybe_await = AsyncMock(side_effect=lambda value: value)
    return instance


def _interaction(owner=2):
    response = types.SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(), send_message=AsyncMock())

    async def defer():
        response.is_done.return_value = True

    response.defer.side_effect = defer
    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=owner), response=response,
        edit_original_response=AsyncMock(),
    )


class AdvancedPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = _panel_module()
        self.saved = {"engine": "edge", "advanced_nightcore_level": "1", "advanced_reverb_level": "2"}
        self.db = types.SimpleNamespace(resolve_tts=Mock(side_effect=lambda *_: dict(self.saved)))
        self.cog = types.SimpleNamespace(
            _get_db=lambda: self.db,
            _maybe_await=AsyncMock(side_effect=lambda value: value),
            _set_user_tts_and_refresh=AsyncMock(),
        )
        self.panel = self.module.VisaoEfeitosAvancadosTTS(self.cog, 2, 1, resolved=self.saved)

    async def test_save_acknowledges_before_slow_persistence_and_edits_after_success(self):
        interaction = _interaction()
        started, release = asyncio.Event(), asyncio.Event()

        async def persist(*args, **kwargs):
            self.assertTrue(interaction.response.is_done())
            started.set()
            await release.wait()

        self.cog._set_user_tts_and_refresh.side_effect = persist
        modal = self.module.ModalEfeitosAvancadosTTS(self.panel)
        modal.nightcore.value = "3"
        task = asyncio.create_task(modal.on_submit(interaction))
        await started.wait()
        interaction.response.defer.assert_awaited_once_with()
        interaction.edit_original_response.assert_not_awaited()
        self.assertEqual(self.panel._levels(), (1, 0, 2))
        release.set()
        await task
        self.assertEqual(self.panel._levels(), (3, 0, 2))
        interaction.edit_original_response.assert_awaited_once_with(view=self.panel)
        self.cog._set_user_tts_and_refresh.assert_awaited_once_with(
            1, 2, background_refresh=True, advanced_nightcore_level=3,
            advanced_slowed_level=0, advanced_reverb_level=2,
        )

    async def test_unchanged_modal_skips_database_write(self):
        interaction = _interaction()
        await self.module.ModalEfeitosAvancadosTTS(self.panel).on_submit(interaction)
        interaction.response.defer.assert_awaited_once()
        self.cog._set_user_tts_and_refresh.assert_not_awaited()
        interaction.edit_original_response.assert_awaited_once_with(view=self.panel)

    async def test_noop_reads_current_settings_when_another_panel_already_saved(self):
        modal = self.module.ModalEfeitosAvancadosTTS(self.panel)
        modal.nightcore.value = "3"
        self.saved["advanced_nightcore_level"] = "3"
        await modal.on_submit(_interaction())
        self.cog._set_user_tts_and_refresh.assert_not_awaited()
        self.assertEqual(self.panel._levels(), (3, 0, 2))

    async def test_reset_saves_once_and_repeated_reset_is_noop(self):
        interaction = _interaction()

        async def save(*args, **kwargs):
            self.assertTrue(interaction.response.is_done())
            self.saved.update({key: value for key, value in kwargs.items() if key.startswith("advanced_")})

        self.cog._set_user_tts_and_refresh.side_effect = save
        await self.panel._disable_all(interaction)
        self.assertEqual(self.panel._levels(), (0, 0, 0))
        await self.panel._disable_all(_interaction())
        self.cog._set_user_tts_and_refresh.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once_with(view=self.panel)

    async def test_database_failure_does_not_render_unsaved_levels(self):
        interaction = _interaction()
        self.cog._set_user_tts_and_refresh.side_effect = RuntimeError("db unavailable")
        with self.assertRaisesRegex(RuntimeError, "db unavailable"):
            await self.panel._disable_all(interaction)
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_not_awaited()
        self.assertEqual(self.panel._levels(), (1, 0, 2))

    async def test_defer_failure_prevents_write(self):
        interaction = _interaction()
        interaction.response.defer.side_effect = RuntimeError("interaction expired")
        with self.assertRaisesRegex(RuntimeError, "interaction expired"):
            await self.panel._disable_all(interaction)
        self.cog._set_user_tts_and_refresh.assert_not_awaited()

    async def test_other_user_cannot_submit_modal(self):
        interaction = _interaction(owner=3)
        await self.module.ModalEfeitosAvancadosTTS(self.panel).on_submit(interaction)
        self.db.resolve_tts.assert_not_called()
        self.cog._set_user_tts_and_refresh.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with("Esse painel neh seu não djacho!", ephemeral=True)

    async def test_selecting_slowed_disables_existing_nightcore(self):
        modal = self.module.ModalEfeitosAvancadosTTS(self.panel)
        modal.slowed.value = "2"
        await modal.on_submit(_interaction())
        self.assertEqual(self.panel._levels(), (0, 2, 2))


class StatusRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.probe = _settings_probe()
        self.view = types.SimpleNamespace(message=object(), is_finished=lambda: False, refresh_from_config_change=AsyncMock())
        self.probe._status_views_by_target[(1, 2)] = [self.view]

    async def test_burst_shares_one_background_task_and_one_refresh(self):
        for _ in range(20):
            await self.probe._notify_status_views_changed(1, 2, background=True)
        self.probe._schedule_tts_background.assert_called_once()
        await self.probe._status_refresh_tasks[(1, 2)]
        self.view.refresh_from_config_change.assert_awaited_once()
        self.assertEqual(self.probe._status_refresh_tasks, {})
        self.assertEqual(self.probe._status_refresh_pending, set())

    async def test_changes_during_refresh_are_merged_into_one_followup(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def refresh():
            started.set()
            await release.wait()

        self.view.refresh_from_config_change.side_effect = refresh
        await self.probe._notify_status_views_changed(1, 2, background=True)
        task = self.probe._status_refresh_tasks[(1, 2)]
        await started.wait()
        for _ in range(10):
            await self.probe._notify_status_views_changed(1, 2, background=True)
        release.set()
        await task
        self.assertEqual(self.view.refresh_from_config_change.await_count, 2)
        self.probe._schedule_tts_background.assert_called_once()

    async def test_default_settings_call_still_waits_for_refresh(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def refresh():
            started.set()
            await release.wait()

        self.view.refresh_from_config_change.side_effect = refresh
        self.probe._get_db = lambda: types.SimpleNamespace(set_user_tts=Mock(return_value="saved"))
        task = asyncio.create_task(self.probe._set_user_tts_and_refresh(1, 2, rate="+10%"))
        await started.wait()
        self.assertFalse(task.done())
        release.set()
        self.assertEqual(await task, "saved")

    async def test_background_settings_call_returns_before_discord_refresh(self):
        self.probe._get_db = lambda: types.SimpleNamespace(set_user_tts=Mock(return_value="saved"))
        result = await self.probe._set_user_tts_and_refresh(1, 2, background_refresh=True, rate="+10%")
        self.assertEqual(result, "saved")
        self.view.refresh_from_config_change.assert_not_awaited()
        await self.probe._status_refresh_tasks[(1, 2)]

    async def test_background_cancellation_releases_coalescing_state(self):
        started = asyncio.Event()

        async def refresh():
            started.set()
            await asyncio.Event().wait()

        self.view.refresh_from_config_change.side_effect = refresh
        await self.probe._notify_status_views_changed(1, 2, background=True)
        task = self.probe._status_refresh_tasks[(1, 2)]
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.probe._status_refresh_tasks, {})
        self.assertEqual(self.probe._status_refresh_pending, set())

    async def test_cancellation_before_task_start_also_releases_state(self):
        await self.probe._notify_status_views_changed(1, 2, background=True)
        task = self.probe._status_refresh_tasks[(1, 2)]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.probe._status_refresh_tasks, {})
        self.assertEqual(self.probe._status_refresh_pending, set())


if __name__ == "__main__":
    unittest.main()
