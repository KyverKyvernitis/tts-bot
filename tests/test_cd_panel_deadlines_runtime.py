"""Run production callbacks with stalled async collaborators, without the SDK."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import test_cd_panel_optimization as optimization
from test_cd_panel_reuse_runtime import DISCORD
from cogs.tts.singing_errors import PhoneWorkerResponseError, singing_failure_code

methods = optimization.methods


class PanelDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def never(self, *args, **kwargs):
        await asyncio.Event().wait()

    def clock(self):
        async def accelerated(awaitable, timeout):
            return await asyncio.wait_for(awaitable, min(timeout, .03))
        return SimpleNamespace(wait_for=accelerated, TimeoutError=asyncio.TimeoutError)

    async def test_settings_timeout_releases_lock_and_next_refresh_recovers(self):
        view, db, ns = optimization.PanelOptimizationTests().panel(prepared=b'validated audio')
        ns.update(asyncio=self.clock(), log=Mock())
        db.resolve_tts.side_effect = self.never
        await view.refresh()
        self.assertFalse(view._settings_lock.locked())
        self.assertFalse(view._settings_available)
        self.assertFalse(view._preview_available)
        self.assertIn('indisponíveis', view._availability_note)
        db.resolve_tts.side_effect = None
        await view.refresh(force=True)
        self.assertTrue(view._settings_available)
        self.assertTrue(view._preview_available)

    async def test_waiting_for_settings_lock_is_bounded(self):
        view, db, ns = optimization.PanelOptimizationTests().panel(prepared=b'validated audio')
        ns.update(asyncio=self.clock(), log=Mock())
        await view._settings_lock.acquire()
        try:
            await view.refresh()
            db.resolve_tts.assert_not_awaited()
        finally:
            view._settings_lock.release()
        await view.refresh(force=True)
        self.assertTrue(view._settings_available)

    async def test_edit_timeout_closes_new_upload_and_returns_recoverable_failure(self):
        ns = {'discord': DISCORD, 'asyncio': self.clock(), 'log': Mock(),
              'cover_revision': lambda _: 'current'}
        extracted = methods('cogs/tts/interface/painel_canto.py',
                            ['_edit_panel', '_edit_panel_current'], ns)
        view = type('ProductionEdit', (), extracted)()
        view.message = SimpleNamespace(attachments=[], edit=AsyncMock(side_effect=self.never))
        view.selected_melody_id = 'rolling-girl'
        view.guild_id, view.owner_id = 20, 10
        view._cover_attachment_key = None
        view._rebuild = Mock()
        file = SimpleNamespace(close=Mock())
        view.panel_files = Mock(return_value=[file])
        self.assertFalse(await view._edit_panel())
        file.close.assert_called_once()
        self.assertIsNone(view._cover_attachment_key)

    def preview(self):
        ns = {'asyncio': asyncio, 'discord': DISCORD, 'log': Mock(),
              '_reply': AsyncMock(), '_public_notice': lambda code, **kwargs: code,
              'singing_failure_code': singing_failure_code,
              'get_melody': lambda _: SimpleNamespace(name='Song'),
              'voice_label': lambda _: 'Teto', 'language_label': lambda _: 'pt-BR',
              'prepared_sample': lambda *args: None}
        extracted = methods('cogs/tts/interface/painel_canto.py', ['preview_melody'], ns)
        view = type('ProductionPreview', (), extracted)()
        view.guild_id, view.owner_id = 20, 10
        view._preview_running = False
        view._preview_available = True
        view._availability_note = ''
        view._preferences = {'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR'}
        view.selected_melody_id = 'song'
        view.interaction_check = AsyncMock(return_value=True)
        view._edit_panel = AsyncMock()
        view.refresh = AsyncMock()
        view.cog = SimpleNamespace(_cd_preview_mp3=AsyncMock(side_effect=PhoneWorkerResponseError(
            'A letra não encaixa nas frases do MIDI', status=400)))
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        return view, interaction, ns

    async def test_preview_acknowledges_before_settings_and_releases_busy_flag_on_unavailable(self):
        view, interaction, ns = self.preview()
        async def refresh(**kwargs):
            interaction.response.defer.assert_awaited_once()
            view._preview_available = False
            view._availability_note = 'Worker offline'
        view.refresh.side_effect = refresh
        await view.preview_melody(interaction)
        view.cog._cd_preview_mp3.assert_not_awaited()
        self.assertFalse(view._preview_running)
        self.assertEqual(ns['_reply'].call_args.args[1], 'Worker offline')

    async def test_preview_uses_specific_input_failure_and_restores_controls(self):
        view, interaction, ns = self.preview()
        await view.preview_melody(interaction)
        self.assertFalse(view._preview_running)
        self.assertIn('ttv_singing_lyrics_mismatch', ns['_reply'].call_args.args[1])
        self.assertEqual(view._edit_panel.await_count, 2)

    async def test_stalled_save_releases_saving_flag_and_refreshes_current_selection(self):
        ns = {'asyncio': self.clock(), '_reply': AsyncMock(),
              'get_melody': lambda value: SimpleNamespace(id=value)}
        extracted = methods('cogs/tts/interface/painel_canto.py', ['apply_melody'], ns)
        view = type('ProductionSave', (), extracted)()
        view.guild_id, view.owner_id = 20, 10
        view._saving = False
        view._settings_available = True
        view.active_melody_id, view.selected_melody_id = 'old', 'selected'
        view.interaction_check = AsyncMock(return_value=True)
        view.cog = SimpleNamespace(_set_user_tts_and_refresh=AsyncMock(side_effect=self.never))
        view.refresh = AsyncMock()
        view._edit_panel = AsyncMock()
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        await view.apply_melody(interaction)
        self.assertFalse(view._saving)
        self.assertEqual(view.active_melody_id, 'old')
        self.assertIn('conferir sua seleção', ns['_reply'].call_args.args[1])
        view.refresh.assert_awaited_once_with(force=True, rebuild=False)
        view._edit_panel.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
