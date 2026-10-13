"""Real JPEG conversion and production registry with simulated Discord API."""
from pathlib import Path
import asyncio
import importlib.util
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

SOURCE = Path(__file__).resolve().parents[1] / 'cogs/tts/interface/application_emojis.py'
spec = importlib.util.spec_from_file_location('cd_emoji_production', SOURCE)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def jpeg():
    output = io.BytesIO()
    Image.new('RGB', (480, 360), 'gray').save(output, format='JPEG')
    return output.getvalue()


class APIError(Exception):
    def __init__(self, status=400, code=50035, retry_after=None):
        self.status, self.code, self.retry_after = status, code, retry_after


class Panel:
    def __init__(self, expired=False):
        self.message = object()
        self.expired = expired
        self._edit_panel = AsyncMock()
        self._rebuild = lambda: None
    def _is_expired(self): return self.expired
    def is_finished(self): return False


class EmojiLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, ids, *, create=None, assets=None):
        self.remote = []
        async def create_default(*, name, image):
            with Image.open(io.BytesIO(image)) as converted:
                self.assertEqual(converted.size, (128, 128))
                self.assertEqual(converted.format, 'JPEG')
                converted.load()
            row = SimpleNamespace(name=name, id=100 + len(self.remote))
            self.remote.append(row)
            return row
        self.bot = SimpleNamespace(fetch_application_emojis=AsyncMock(side_effect=lambda: list(self.remote)),
                                   create_application_emoji=AsyncMock(side_effect=create or create_default))
        manager = registry.ApplicationSelectEmojis('osakacd', assets or (lambda _: jpeg()), lambda: ids)
        self.addAsyncCleanup(self.cleanup_manager, manager)
        return manager, create_default

    async def cleanup_manager(self, manager):
        task = manager._task
        manager.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def test_bad_earlier_song_does_not_block_rolling_girl(self):
        manager, create_default = self.manager(['bad-song', 'rolling-girl'])
        async def create(*, name, image):
            if name == manager._name('bad-song'): raise APIError()
            return await create_default(name=name, image=image)
        self.bot.create_application_emoji.side_effect = create
        expired, active = Panel(True), Panel()
        manager.schedule(self.bot, expired)
        manager.schedule(self.bot, active)
        await manager._task
        self.assertIn(manager._name('rolling-girl'), manager._known)
        self.assertEqual(manager.diagnostics('bad-song')['http_status'], 400)
        self.assertEqual(active._edit_panel.await_count, 1)
        self.assertEqual(expired._edit_panel.await_count, 0)
        self.assertIsNotNone(manager._retry_handle)

    async def test_registered_emoji_is_reused_without_create_or_repeat_fetch(self):
        manager, _ = self.manager(['rolling-girl'])
        manager.schedule(self.bot)
        await manager._task
        manager.schedule(self.bot)
        self.assertEqual(self.bot.create_application_emoji.await_count, 1)
        self.assertEqual(self.bot.fetch_application_emojis.await_count, 1)
        self.assertEqual(manager.diagnostics('rolling-girl')['state'], 'ready')

    async def test_retry_of_uncertain_post_fetches_existing_emoji_without_duplicate(self):
        manager, create_default = self.manager(['rolling-girl'])
        async def timed_out(*, name, image):
            await create_default(name=name, image=image)
            raise TimeoutError()
        self.bot.create_application_emoji.side_effect = timed_out
        manager.schedule(self.bot)
        await manager._task
        manager._pending[manager._name('rolling-girl')]['retry_at'] = 0
        manager.schedule(self.bot)
        await manager._task
        self.assertEqual(self.bot.create_application_emoji.await_count, 1)
        self.assertEqual(manager.diagnostics('rolling-girl')['state'], 'ready')

    async def test_capacity_error_is_logged_and_does_not_delete_emojis(self):
        manager, _ = self.manager(['rolling-girl'], create=lambda **_: (_ for _ in ()).throw(APIError(400, 30008)))
        manager.schedule(self.bot)
        await manager._task
        self.assertGreater(manager._blocked_until, 0)
        before = self.bot.create_application_emoji.await_count
        manager._requested_ids = ()
        manager.schedule(self.bot)
        self.assertEqual(self.bot.create_application_emoji.await_count, before)
        self.assertEqual(manager.diagnostics('rolling-girl')['discord_code'], 30008)

    async def test_missing_local_asset_can_appear_before_scheduled_retry(self):
        available = []
        manager, _ = self.manager(['rolling-girl'], assets=lambda _: jpeg() if available else None)
        manager.schedule(self.bot)
        await manager._task
        self.assertEqual(manager.diagnostics('rolling-girl')['state'], 'local_asset_missing')
        available.append(True)
        manager._pending[manager._name('rolling-girl')]['retry_at'] = 0
        manager.schedule(self.bot)
        await manager._task
        self.assertEqual(manager.diagnostics('rolling-girl')['state'], 'ready')

    async def test_transient_creation_recovers_without_another_panel_open(self):
        recovered = asyncio.Event()
        manager, create_default = self.manager(['rolling-girl'])
        attempts = []
        async def create(*, name, image):
            attempts.append(name)
            if len(attempts) == 1: raise APIError(503, 0)
            row = await create_default(name=name, image=image)
            recovered.set()
            return row
        self.bot.create_application_emoji.side_effect = create
        manager._retry_delay = lambda *args: 0.01
        manager.schedule(self.bot)
        await manager._task
        await asyncio.wait_for(recovered.wait(), timeout=2)
        self.assertEqual(len(attempts), 2)
        self.assertIn(manager._name('rolling-girl'), manager._known)
        self.assertGreater(manager.generation, 0)

    async def test_close_during_changed_catalog_never_restarts_synchronization(self):
        ids = ['first-song']
        manager, _ = self.manager(ids)
        entered, blocked = asyncio.Event(), asyncio.Event()
        async def fetch():
            entered.set()
            await blocked.wait()
            return []
        self.bot.fetch_application_emojis.side_effect = fetch
        manager.schedule(self.bot)
        task = manager._task
        await entered.wait()
        ids.append('rolling-girl')
        manager.schedule(self.bot)
        manager.close()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(manager._closed)
        self.assertIsNone(manager._task)
        self.assertIsNone(manager._retry_handle)
        self.assertEqual(self.bot.fetch_application_emojis.await_count, 1)
        self.bot.create_application_emoji.assert_not_called()

    async def test_obsolete_version_failure_does_not_keep_a_retry_alive(self):
        manager, _ = self.manager(['rolling-girl'])
        version = ['old']
        manager.version_for_id = lambda _: version[0]
        manager.assets = lambda _: None
        manager.schedule(self.bot)
        await manager._task
        old_name = manager._name('rolling-girl')
        version[0] = 'new'
        manager._known[manager._name('rolling-girl')] = 555
        manager.schedule(self.bot)
        self.assertNotIn(old_name, manager._pending)
        self.assertIsNone(manager._retry_handle)

    async def test_stale_post_response_cannot_change_new_bot_registry(self):
        manager, create_default = self.manager(['rolling-girl'])
        entered, release = asyncio.Event(), asyncio.Event()
        async def create(*, name, image):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return SimpleNamespace(name=name, id=999)
        self.bot.create_application_emoji.side_effect = create
        manager.schedule(self.bot)
        old_task = manager._task
        await entered.wait()
        next_bot = SimpleNamespace(fetch_application_emojis=AsyncMock(return_value=[]),
                                   create_application_emoji=AsyncMock(side_effect=create_default))
        manager.schedule(next_bot)
        new_task = manager._task
        await new_task
        release.set()
        await old_task
        self.assertNotEqual(manager._known[manager._name('rolling-girl')], 999)
        self.assertIsNone(manager._task)


if __name__ == '__main__': unittest.main()
