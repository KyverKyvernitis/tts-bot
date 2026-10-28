"""Bundled additions use the same SQLite transactions as phone publication."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
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


class BundledMetadataTests(unittest.TestCase):
    apply = BundledInstallTests.apply
    publish = BundledInstallTests.publish
    score_at_current = BundledInstallTests.score_at_current

    def setUp(self):
        BundledInstallTests.setUp(self)
        with zipfile.ZipFile(bundled._PACKAGE) as archive:
            self.old_files = {name: archive.read(name) for name in archive.namelist()}
        self.provider = self.root / 'provider-artwork.zip'
        from PIL import Image
        import io
        target = io.BytesIO()
        Image.new('RGB', (32, 32), (35, 180, 155)).save(target, 'JPEG')
        self.image = target.getvalue()
        self.image_path = 'cogs/tts/cd/thumbnails/provider001.jpg'
        self.desired = {'video_id': 'provider001', 'title': 'Ievan Polkka / Otomania',
                        'artist': 'Otomania feat. Hatsune Miku',
                        'selection_basis': 'highest-view-count-among-inspected-original-vocal-matches',
                        'thumbnail_sha256': hashlib.sha256(self.image).hexdigest(),
                        'view_count': 100, 'selection_audit': {'provider': 'test-fixture'}}
        files = dict(self.old_files)
        files['cogs/tts/cd_song_videos.json'] = json.dumps({'version': 1, 'songs': {
            'ievan-polkka': self.desired}}).encode()
        files[self.image_path] = self.image
        manifest_path = 'cogs/tts/cd/packages/ievan-polkka.json'
        manifest = json.loads(files[manifest_path])
        manifest['artifacts']['thumbnail'] = {'status': 'ready', 'path': self.image_path,
                                             'sha256': self.desired['thumbnail_sha256']}
        files[manifest_path] = json.dumps(manifest).encode()
        self.write_package(self.provider, files)
        _contract().validate_package(self.provider, require_cd_only=True)

    @staticmethod
    def write_package(path, files):
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        return path

    def repair(self, checksum=None):
        return bundled.repair_cd_metadata(self.store, identity='ievan-polkka',
            package=self.provider, sha256=checksum or hashlib.sha256(self.provider.read_bytes()).hexdigest())

    def current_files(self):
        root = Path(self.store.snapshot()['root'])
        return root, {path.relative_to(root).as_posix(): path.read_bytes()
                      for path in root.rglob('*') if path.is_file()}

    def test_old_installed_cd_receives_provider_metadata_and_cover_without_music_changes(self):
        self.apply(bundled._PACKAGE)
        before = self.store.snapshot()
        old_root, old_files = self.current_files()
        self.assertTrue(bundled.bundled_cd_needs_artwork(self.store))
        result = self.repair()
        self.assertTrue(result['metadata_updated'])
        after = self.store.snapshot()
        new_root, new_files = self.current_files()
        self.assertEqual(before['versions']['young-girl-a'], after['versions']['young-girl-a'])
        for relative in ('cogs/tts/cd_song_scores.json', 'cogs/tts/cd/arrangements/ievan-polkka.json',
                         'cogs/tts/cd/previews/index.json'):
            self.assertEqual(new_files[relative], old_files[relative])
        self.assertEqual({p: (old_root / p).read_bytes() for p in old_files}, old_files)
        video = json.loads(new_files['cogs/tts/cd_song_videos.json'])['songs']['ievan-polkka']
        self.assertEqual(video['video_id'], self.desired['video_id'])
        self.assertEqual(video['view_count'], 100)
        self.assertEqual(new_files[self.image_path], self.image)
        self.assertFalse(bundled.bundled_cd_needs_artwork(self.store))
        self.assertEqual(self.repair()['reason'], 'cover_ready')
        self.assertEqual(self.store.snapshot()['generation'], after['generation'])

    def test_legacy_installed_cd_can_receive_only_the_missing_metadata(self):
        data = self.legacy / 'cogs/tts'
        original_scores = json.loads((data / 'cd_song_scores.json').read_text())
        for relative, raw in self.old_files.items():
            target = self.legacy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        (data / 'cd_song_scores.json').write_text(json.dumps(original_scores + json.loads(
            self.old_files['cogs/tts/cd_song_scores.json'])))
        self.assertFalse(self.store.snapshot()['initialized'])
        self.assertTrue(bundled.bundled_cd_needs_artwork(self.store))
        self.assertTrue(self.repair()['metadata_updated'])
        self.assertEqual(set(self.store.snapshot()['versions']), {'young-girl-a', 'ievan-polkka'})

    def test_custom_music_and_newer_valid_cover_are_preserved(self):
        self.publish('ievan-polkka', 76)
        before = self.store.snapshot()
        self.assertFalse(bundled.bundled_cd_needs_artwork(self.store))
        self.assertEqual(self.repair()['reason'], 'custom_song')
        self.assertEqual(self.store.snapshot()['generation'], before['generation'])

    def test_custom_metadata_without_cover_is_not_replaced(self):
        self.apply(bundled._PACKAGE)
        path = self.write_package(self.root / 'custom-video.zip', {
            'cogs/tts/cd_song_videos.json': json.dumps({'songs': {'ievan-polkka': {
                'video_id': 'newcustom01', 'title': 'Custom recording',
                'selection_basis': 'user-selected'}}}).encode()})
        job = self.store.receive(path)
        self.assertTrue(self.store.process(job['job_id'])['applied'])
        before = self.store.snapshot()
        self.assertFalse(bundled.bundled_cd_needs_artwork(self.store))
        self.assertEqual(self.repair()['reason'], 'custom_metadata')
        self.assertEqual(self.store.snapshot()['generation'], before['generation'])

    def test_checksum_rejection_leaves_current_generation_untouched(self):
        self.apply(bundled._PACKAGE)
        before = self.store.snapshot()
        with self.assertRaisesRegex(StoreError, 'checksum'):
            self.repair('0' * 64)
        self.assertEqual(self.store.snapshot(), before)

    def test_lost_owned_cover_can_be_healed_after_a_previous_applied_repair(self):
        self.apply(bundled._PACKAGE)
        self.repair()
        first = self.store.snapshot()
        (Path(first['root']) / self.image_path).unlink()
        self.assertTrue(bundled.bundled_cd_needs_artwork(self.store))
        result = self.repair()
        self.assertTrue(result['metadata_updated'])
        self.assertNotEqual(self.store.snapshot()['generation'], first['generation'])
        self.assertEqual((Path(self.store.snapshot()['root']) / self.image_path).read_bytes(), self.image)

    def test_same_semantic_job_with_old_failed_expected_version_can_retry(self):
        self.apply(bundled._PACKAGE)
        receive = self.store.receive
        poisoned = []
        def receive_after_failure(path, *args, **kwargs):
            if not poisoned:
                receipt = receive(path, expected_versions={'ievan-polkka': 'f' * 64})
                poisoned.append(self.store.process(receipt['job_id']))
            return receive(path, *args, **kwargs)
        with patch.object(self.store, 'receive', side_effect=receive_after_failure):
            self.assertTrue(self.repair()['metadata_updated'])
        self.assertEqual(poisoned[0]['error_code'], 'conflict')

    def test_concurrent_user_cover_wins_and_prevents_retry_overwrite(self):
        self.apply(bundled._PACKAGE)
        receive = self.store.receive
        published = []
        def receive_after_custom_cover(path, *args, **kwargs):
            if not published:
                # A different valid cover arrives after our local eligibility check.
                custom = dict(self.desired, video_id='newcustom01', title='User-selected recording')
                custom_path = 'cogs/tts/cd/thumbnails/newcustom01.jpg'
                archive = self.write_package(self.root / 'concurrent-cover.zip', {
                    'cogs/tts/cd_song_videos.json': json.dumps({'songs': {'ievan-polkka': custom}}).encode(),
                    custom_path: self.image})
                receipt = receive(archive)
                published.append(self.store.process(receipt['job_id']))
            return receive(path, *args, **kwargs)
        with patch.object(self.store, 'receive', side_effect=receive_after_custom_cover):
            self.assertEqual(self.repair()['reason'], 'cover_ready')
        self.assertTrue(published[0]['applied'])
        self.assertEqual(self.store.snapshot()['generation'], published[0]['generation'])
        video = json.loads((Path(self.store.snapshot()['root']) /
            'cogs/tts/cd_song_videos.json').read_text())['songs']['ievan-polkka']
        self.assertEqual(video['video_id'], 'newcustom01')

    def publish_audio(self, receive=None):
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg required for a real preserved sample')
        receive = receive or self.store.receive
        audio = self.root / 'sample.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=16.35', '-c:a', 'libmp3lame', str(audio)],
                       check=True, capture_output=True)
        melody = self.score_at_current('ievan-polkka')
        metadata = {'manifest_version': 3, 'id': 'ievan-polkka', 'revision': melody['revision'],
                    'source_sha256': melody['source_sha256'], 'score_sha256': melody['score_sha256'],
                    'mp3_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(),
                    'voice_id': 'kasane-teto', 'language': 'pt-BR', 'pitch_semitones': 0, 'speech_rate': 1.0,
                    'arrangement_sha256': melody['preview_arrangement_sha256'],
                    'source_arrangement_sha256': melody['arrangement_sha256'],
                    'arrangement_performance_sha256': melody['preview_performance_sha256'],
                    'rendering_profile': {'mode': 'multipart-utau',
                        'configuration_fingerprint': 'b' * 64, 'render_fingerprint': 'c' * 64}}
        archive = self.write_package(self.root / 'audio.zip', {
            'cogs/tts/cd/previews/index.json': json.dumps({'ievan-polkka': metadata}).encode(),
            'cogs/tts/cd/previews/ievan-polkka.mp3': audio.read_bytes()})
        receipt = receive(archive)
        result = self.store.process(receipt['job_id'])
        self.assertTrue(result['applied'], result)
        return result

    def test_concurrent_audio_publication_is_retained_by_metadata_retry(self):
        self.apply(bundled._PACKAGE)
        receive = self.store.receive
        published = []
        before = []
        def receive_after_audio(path, *args, **kwargs):
            if not published:
                published.append(self.publish_audio(receive))
                root = Path(self.store.snapshot()['root'])
                before.append({relative: (root / relative).read_bytes() for relative in (
                    'cogs/tts/cd/previews/index.json', 'cogs/tts/cd/previews/ievan-polkka.mp3')})
            return receive(path, *args, **kwargs)
        with patch.object(self.store, 'receive', side_effect=receive_after_audio):
            self.assertTrue(self.repair()['metadata_updated'])
        root = Path(self.store.snapshot()['root'])
        for relative, raw in before[0].items():
            self.assertEqual((root / relative).read_bytes(), raw)
        self.assertFalse(bundled.bundled_cd_needs_artwork(self.store))


class BundledArtworkCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = BundledMetadataTests(methodName='test_checksum_rejection_leaves_current_generation_untouched')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.apply(bundled._PACKAGE)

    async def test_background_imports_provider_once_off_loop_then_skips_ready_cover(self):
        from osaka_cd import bundled_artwork
        calls, threads = [], []
        loop_thread = threading.get_ident()
        @asynccontextmanager
        async def provider():
            calls.append(1)
            yield self.fixture.provider
        def store():
            threads.append(threading.get_ident())
            return self.fixture.store
        with patch.object(bundled, 'get_store', side_effect=store), \
                patch.object(bundled_artwork, 'imported_artwork_package', provider):
            result = await bundled.refresh_bundled_cd_artwork()
            self.assertTrue(result['metadata_updated'])
            self.assertEqual((await bundled.refresh_bundled_cd_artwork())['status'], 'existing')
        self.assertEqual(calls, [1])
        self.assertTrue(threads)
        self.assertNotIn(loop_thread, threads)

    async def test_provider_failures_retry_without_exposing_urls_or_blocking_startup(self):
        from osaka_cd import bundled_artwork
        attempts = []
        @asynccontextmanager
        async def provider():
            attempts.append(1)
            raise RuntimeError('https://private-token@example.invalid/private')
            yield  # pragma: no cover
        sleep = AsyncMock()
        with patch.object(bundled, 'get_store', return_value=self.fixture.store), \
                patch.object(bundled_artwork, 'imported_artwork_package', provider), \
                patch.object(bundled.asyncio, 'sleep', sleep), \
                self.assertLogs('osaka_cd.bundled', level='WARNING') as logs:
            result = await bundled.refresh_bundled_cd_artwork()
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(attempts, [1, 1, 1])
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [60, 180])
        self.assertNotIn('private-token', ''.join(logs.output))
        self.assertNotIn('example.invalid', ''.join(logs.output))

    async def test_no_provider_lookup_for_a_custom_existing_cover(self):
        from osaka_cd import bundled_artwork
        self.fixture.repair()
        with patch.object(bundled, 'get_store', return_value=self.fixture.store), \
                patch.object(bundled_artwork, 'imported_artwork_package',
                             side_effect=AssertionError('already has a usable cover')) as provider:
            self.assertEqual((await bundled.refresh_bundled_cd_artwork())['status'], 'existing')
        provider.assert_not_called()

    async def test_cancelled_refresh_keeps_provider_package_alive_until_repair_finishes(self):
        from osaka_cd import bundled_artwork
        started = asyncio.Event()
        released = threading.Event()
        completed = threading.Event()
        closed = []
        loop = asyncio.get_running_loop()
        @asynccontextmanager
        async def provider():
            try:
                yield self.fixture.provider
            finally:
                closed.append(completed.is_set())
        original = bundled._apply_imported_artwork
        def held_repair(store, package):
            loop.call_soon_threadsafe(started.set)
            if not released.wait(3):
                raise AssertionError('test did not release metadata transaction')
            try:
                return original(store, package)
            finally:
                completed.set()
        with patch.object(bundled, 'get_store', return_value=self.fixture.store), \
                patch.object(bundled_artwork, 'imported_artwork_package', provider), \
                patch.object(bundled, '_apply_imported_artwork', side_effect=held_repair):
            task = asyncio.create_task(bundled.refresh_bundled_cd_artwork())
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(closed, [])
            finally:
                released.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(closed, [True])
        self.assertTrue(completed.is_set())


if __name__ == '__main__':
    unittest.main()
