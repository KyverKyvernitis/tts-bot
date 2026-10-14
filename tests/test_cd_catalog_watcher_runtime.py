"""Run production watcher lifecycle with scheduler/transport doubles, no SDK."""
from pathlib import Path
import asyncio
import ast
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

SOURCE = Path(__file__).resolve().parents[1] / 'cogs/tts/interface/painel_canto.py'


def watcher_methods(namespace):
    names = {'start_catalog_watch', 'stop_catalog_watch', '_watch_catalog'}
    nodes = [node for node in ast.parse(SOURCE.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)


class CatalogWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setup_watcher(self, panels=()):
        self.version = 1
        self.closed = False
        self.bot = SimpleNamespace(wait_until_ready=AsyncMock(), is_closed=lambda: self.closed)
        self.cog = SimpleNamespace(bot=self.bot)
        self.manager = SimpleNamespace(schedule=Mock(), close=Mock())
        self.ns = {'catalog_signature': lambda: self.version,
                   'list_melodies': lambda: [SimpleNamespace(id='rolling-girl')],
                   'DEFAULT_MELODY_ID': 'default', '_CD_EMOJIS': self.manager,
                   '_ACTIVE_PANELS': list(panels), '_CATALOG_TASK': None,
                   '_CATALOG_COG': None, 'asyncio': asyncio}
        watcher_methods(self.ns)
        return self.ns

    async def test_startup_and_catalog_arrival_sync_without_open_panels(self):
        ns = self.setup_watcher()
        ticks = []
        async def tick(delay):
            self.assertEqual(delay, 15)
            ticks.append(delay)
            self.version += 1
            self.closed = len(ticks) >= 2
        ns['asyncio'] = SimpleNamespace(sleep=tick, current_task=asyncio.current_task)
        await ns['_watch_catalog'](self.cog)
        self.bot.wait_until_ready.assert_awaited_once()
        self.assertEqual(self.manager.schedule.call_count, 2)

    async def test_panel_expiring_during_refresh_is_not_edited_or_revived(self):
        state = {'expired': False}
        panel = SimpleNamespace(selected_melody_id='rolling-girl', message=object(),
                                _is_expired=lambda: state['expired'], is_finished=lambda: False,
                                _edit_panel=AsyncMock())
        async def refresh(**kwargs):
            self.assertFalse(kwargs['rebuild'])
            state['expired'] = True
        panel.refresh = AsyncMock(side_effect=refresh)
        ns = self.setup_watcher([panel])
        ticks = []
        async def tick(_):
            ticks.append(1)
            self.version += 1
            self.closed = len(ticks) >= 2
        ns['asyncio'] = SimpleNamespace(sleep=tick, current_task=asyncio.current_task)
        await ns['_watch_catalog'](self.cog)
        panel.refresh.assert_awaited_once()
        panel._edit_panel.assert_not_called()
        self.assertTrue(state['expired'])

    async def test_old_cog_shutdown_cannot_stop_new_watcher(self):
        ns = self.setup_watcher()
        old_cog = SimpleNamespace(bot=self.bot)
        old_task, new_task = Mock(), Mock()
        ns['_CATALOG_COG'], ns['_CATALOG_TASK'] = old_cog, old_task
        ns['asyncio'] = SimpleNamespace(create_task=Mock(return_value=new_task))
        ns['start_catalog_watch'](self.cog)
        coroutine = ns['asyncio'].create_task.call_args.args[0]
        coroutine.close()
        old_task.cancel.assert_called_once()
        ns['stop_catalog_watch'](old_cog)
        new_task.cancel.assert_not_called()
        self.manager.close.assert_not_called()
        ns['stop_catalog_watch'](self.cog)
        new_task.cancel.assert_called_once()
        self.manager.close.assert_called_once()

    async def test_cancelled_old_task_does_not_clear_new_task_reference(self):
        ns = self.setup_watcher()
        entered = asyncio.Event()
        async def wait(_):
            entered.set()
            await asyncio.Event().wait()
        ns['asyncio'] = SimpleNamespace(sleep=wait, current_task=asyncio.current_task)
        task = asyncio.create_task(ns['_watch_catalog'](self.cog))
        await entered.wait()
        new_task = object()
        ns['_CATALOG_TASK'], ns['_CATALOG_COG'] = new_task, self.cog
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIs(ns['_CATALOG_TASK'], new_task)
        self.assertIs(ns['_CATALOG_COG'], self.cog)

    async def test_publication_events_refresh_immediately_and_coalesce(self):
        ns = self.setup_watcher()
        listeners = []
        completed = asyncio.Event()
        loaded = []
        async def acknowledge_off_thread(callback):
            # The store's actual SQLite/threads are exercised separately.
            return callback()
        ns.update(subscribe_catalog_events=lambda callback: listeners.append(callback) or Mock(),
                  pin_snapshot=nullcontext, log=Mock(),
                  acknowledge_catalog=Mock(return_value=True),
                  asyncio=SimpleNamespace(Event=asyncio.Event, get_running_loop=asyncio.get_running_loop,
                    wait_for=asyncio.wait_for, TimeoutError=asyncio.TimeoutError,
                    current_task=asyncio.current_task, to_thread=acknowledge_off_thread))
        def load():
            loaded.append(self.version)
            if self.version == 2:
                completed.set()
            return []
        ns['list_melodies'] = load
        task = asyncio.create_task(ns['_watch_catalog'](self.cog))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
                if loaded:
                    break
            self.assertEqual(loaded, [1])
            self.version = 2
            for _ in range(100):
                listeners[0]({'generation': 'new'})
            await asyncio.wait_for(completed.wait(), timeout=.5)
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(loaded, [1, 2])
            self.assertEqual(self.manager.schedule.call_count, 2)
            self.assertEqual(ns['acknowledge_catalog'].call_count, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_busy_panel_refresh_is_coalesced_and_expired_panel_is_skipped(self):
        source = ast.parse(SOURCE.read_text())
        node = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == '_refresh_catalog_panels')
        ns = {'asyncio': asyncio, 'log': Mock(), 'DEFAULT_MELODY_ID': 'default',
              'list_melodies': lambda: [SimpleNamespace(id='song')]}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
        def panel(*, busy=False, expired=False):
            return SimpleNamespace(_is_expired=lambda: expired, is_finished=lambda: False,
                _preview_running=busy, _saving=False, _catalog_refresh_pending=False,
                selected_melody_id='song', message=object(), refresh=AsyncMock(), _edit_panel=AsyncMock())
        active, busy, expired = panel(), panel(busy=True), panel(expired=True)
        await ns['_refresh_catalog_panels']([active, busy, expired])
        active.refresh.assert_awaited_once_with(rebuild=False)
        active._edit_panel.assert_awaited_once()
        busy.refresh.assert_not_awaited()
        self.assertTrue(busy._catalog_refresh_pending)
        expired.refresh.assert_not_awaited()
        self.assertFalse(expired._catalog_refresh_pending)


if __name__ == '__main__':
    unittest.main()
