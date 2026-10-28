"""Bundled additions use the same SQLite transactions as phone publication."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from osaka_cd import bundled
from osaka_cd.store import RuntimeStore, StoreError, _contract


def score(identity, pitch=60):
    item = {'id': identity, 'name': identity, 'artist': 'Test', 'excerpt': '',
            'source_url': '', 'source_sha256': 'a' * 64, 'duration_ms': 500,
            'notes': [{'midi_note': pitch, 'start_ms': 0, 'duration_ms': 500,
                       'velocity': 90}]}
    item['score_sha256'] = _contract().score_sha256(item)
    return item


def package(path, identity, pitch=60):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('cogs/tts/cd_song_scores.json', json.dumps([score(identity, pitch)]))
        archive.writestr('cogs/tts/cd_song_videos.json', '{"songs":{}}')
        archive.writestr('cogs/tts/cd/previews/index.json', '{}')
    return path


class BundledInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cd-bundled-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / 'legacy'
        data = self.legacy / 'cogs/tts'
        (data / 'cd/previews').mkdir(parents=True)
        (data / 'cd_song_scores.json').write_text(json.dumps([score('young-girl-a')]))
        (data / 'cd_song_videos.json').write_text('{"songs":{}}')
        (data / 'cd/previews/index.json').write_text('{}')
        self.store = RuntimeStore(self.root / 'state', self.legacy)

    def apply(self, path, **kwargs):
        return bundled.install_missing_cd(self.store, identity='ievan-polkka',
            package=path, sha256=kwargs.get('sha256', hashlib.sha256(path.read_bytes()).hexdigest()))

    def publish(self, identity, pitch=60):
        path = package(self.root / f'publish-{identity}-{pitch}.zip', identity, pitch)
        receipt = self.store.receive(path)
        result = self.store.process(receipt['job_id'])
        self.assertEqual(result['state'], 'applied', result)
        return result

    def score_at_current(self, identity):
        root = Path(self.store.snapshot()['root'])
        entries = json.loads((root / 'cogs/tts/cd_song_scores.json').read_text())
        return next(item for item in entries if item['id'] == identity)

    def test_actual_shipped_package_merges_legacy_and_waits_for_reader_ack(self):
        before = {p: p.read_bytes() for p in self.legacy.rglob('*') if p.is_file()}
        self.assertEqual(hashlib.sha256(bundled._PACKAGE.read_bytes()).hexdigest(),
                         bundled._PACKAGE_SHA256)
        result = self.apply(bundled._PACKAGE)
        self.assertEqual(result['status'], 'applied')
        current = self.store.snapshot()
        self.assertEqual(set(current['versions']), {'young-girl-a', 'ievan-polkka'})
        self.assertFalse(self.store.job(result['job_id'])['installed'])
        song = self.score_at_current('ievan-polkka')
        arrangement = json.loads((Path(current['root']) / song['arrangement_path']).read_text())
        self.assertEqual([part['role'] for part in arrangement['parts']], ['lead', 'harmony', 'bass'])
        self.assertEqual(len(next(part for part in arrangement['parts'] if part['role'] == 'lead')['notes']), 86)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertTrue(self.store.acknowledge(result['generation']))
        self.assertTrue(self.store.job(result['job_id'])['installed'])

    def test_initialized_generation_and_versions_are_preserved(self):
        first = self.publish('rolling-girl', 64)
        prior = self.store.snapshot()
        old_root = Path(prior['root'])
        old_bytes = {p: p.read_bytes() for p in old_root.rglob('*') if p.is_file()}
        result = self.apply(bundled._PACKAGE)
        self.assertEqual(result['status'], 'applied')
        current = self.store.snapshot()
        for identity, version in prior['versions'].items():
            self.assertEqual(current['versions'][identity], version)
        self.assertNotEqual(current['generation'], first['generation'])
        self.assertEqual({p: p.read_bytes() for p in old_bytes}, old_bytes)
        second = self.apply(bundled._PACKAGE)
        self.assertEqual(second['status'], 'existing')
        self.assertEqual(self.store.snapshot()['generation'], current['generation'])

    def test_custom_runtime_song_wins_even_with_unavailable_shipped_file(self):
        result = self.publish('ievan-polkka', 76)
        skipped = bundled.install_missing_cd(self.store, identity='ievan-polkka',
            package=self.root / 'missing.zip', sha256='0' * 64)
        self.assertEqual(skipped['status'], 'existing')
        self.assertEqual(self.store.snapshot()['generation'], result['generation'])
        self.assertEqual(self.score_at_current('ievan-polkka')['notes'][0]['midi_note'], 76)

    def test_custom_legacy_song_is_not_migrated_or_overwritten(self):
        path = self.legacy / 'cogs/tts/cd_song_scores.json'
        path.write_text(json.dumps([score('young-girl-a'), score('ievan-polkka', 72)]))
        result = self.apply(bundled._PACKAGE)
        self.assertEqual(result['status'], 'existing')
        self.assertFalse(self.store.snapshot()['initialized'])
        self.assertEqual(json.loads(path.read_text())[-1]['notes'][0]['midi_note'], 72)

    def test_corrupt_checksum_cannot_publish_or_change_active_pointer(self):
        self.publish('rolling-girl')
        before = self.store.snapshot()
        with self.assertRaisesRegex(StoreError, 'checksum'):
            self.apply(bundled._PACKAGE, sha256='0' * 64)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.store.outstanding(), [])

    def test_wrong_identity_is_rejected_before_receipt(self):
        path = package(self.root / 'wrong.zip', 'other-song')
        with self.assertRaisesRegex(StoreError, 'identity'):
            self.apply(path)
        self.assertFalse(self.store.snapshot()['initialized'])
        self.assertEqual(self.store.outstanding(), [])

    def test_shipped_package_stages_without_forcing_or_unignoring_other_archives(self):
        repo = self.root / 'updater-staging'
        repo.mkdir()
        source = Path(__file__).resolve().parents[1]
        for relative in ('.gitignore', 'osaka_cd/bundled/.gitignore',
                         'osaka_cd/bundled/ievan-polkka.zip'):
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, destination)
        unrelated = repo / 'osaka_cd/bundled/unrelated.zip'
        unrelated.write_bytes(b'untracked local archive')
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        # Match the updater: ordinary explicit git add, with no -f override.
        staged = ['osaka_cd/bundled/.gitignore', 'osaka_cd/bundled/ievan-polkka.zip']
        subprocess.run(['git', '-C', str(repo), 'add', '-A', '--', *staged],
                       check=True, capture_output=True)
        result = subprocess.run(['git', '-C', str(repo), 'diff', '--cached',
                                 '--name-only', '-z'], check=True, capture_output=True)
        self.assertEqual(set(result.stdout.decode().strip('\0').split('\0')), set(staged))
        ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', '--',
                                  'osaka_cd/bundled/unrelated.zip'], capture_output=True)
        self.assertEqual(ignored.returncode, 0)

    def test_user_publication_between_snapshot_and_commit_wins(self):
        self.publish('rolling-girl')
        original_receive = self.store.receive
        custom = package(self.root / 'concurrent-user.zip', 'ievan-polkka', 79)
        user_results = []

        def receive(path, *args, **kwargs):
            receipt = original_receive(custom)
            user_results.append(self.store.process(receipt['job_id']))
            return original_receive(path, *args, **kwargs)

        with patch.object(self.store, 'receive', side_effect=receive):
            result = self.apply(bundled._PACKAGE)
        self.assertEqual(result['status'], 'existing')
        self.assertEqual(self.store.snapshot()['generation'], user_results[0]['generation'])
        self.assertEqual(self.score_at_current('ievan-polkka')['notes'][0]['midi_note'], 79)
        self.assertEqual(self.apply(bundled._PACKAGE)['status'], 'existing')

    def test_two_installers_share_one_durable_commit(self):
        self.publish('rolling-girl')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.apply(bundled._PACKAGE), range(2)))
        self.assertTrue(all(result['status'] in {'applied', 'existing'} for result in results))
        with self.store._connect(read_only=True) as connection:
            count = connection.execute('SELECT COUNT(*) FROM generations').fetchone()[0]
        # Legacy baseline + Rolling Girl publication + one bundled addition.
        self.assertEqual(count, 3)

    def test_optional_startup_failure_is_compact_and_keeps_other_cds(self):
        with patch.object(bundled, 'get_store', return_value=self.store), \
                patch.object(bundled, '_PACKAGE_SHA256', '0' * 64), \
                self.assertLogs('osaka_cd.bundled', level='WARNING') as logs:
            result = bundled.install_bundled_cds()
        self.assertEqual(result['status'], 'deferred')
        self.assertNotIn(str(self.root), ''.join(logs.output))
        self.assertFalse(self.store.snapshot()['initialized'])


class BundledStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_does_not_block_loop_and_cancellation_drains_transaction(self):
        started = asyncio.Event()
        finished = threading.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        physical_threads = []

        def install():
            physical_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise AssertionError('test failed to release physical installer')
            finished.set()
            return {'status': 'applied'}

        with patch.object(bundled, 'install_bundled_cds', side_effect=install):
            task = asyncio.create_task(bundled.initialize_bundled_cds())
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertNotEqual(physical_threads, [loop_thread])
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(finished.is_set())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertTrue(finished.is_set())

    async def test_cancelled_startup_keeps_cancellation_if_physical_work_fails(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def install():
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            raise RuntimeError('physical failure after startup cancellation')

        with patch.object(bundled, 'install_bundled_cds', side_effect=install):
            task = asyncio.create_task(bundled.initialize_bundled_cds())
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)

    def test_cog_installs_before_starting_the_existing_catalog_watcher(self):
        source = (Path(__file__).resolve().parents[1] / 'cogs/tts/cog.py').read_text()
        import ast
        tree = ast.parse(source)
        load = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == 'cog_load')
        body = ast.get_source_segment(source, load)
        self.assertLess(body.index('await initialize_bundled_cds()'),
                        body.index('start_catalog_watch(self)'))


if __name__ == '__main__':
    unittest.main()
