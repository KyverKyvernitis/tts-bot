"""Real ZIP/SQLite/image/audio integration for the runtime CD data store."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from osaka_cd.store import RuntimeStore, StoreError, _contract


class RuntimeStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory(prefix='runtime-cd-audio-')
        folder = Path(cls.assets.name)
        audio = folder / 'sample.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5',
                        '-codec:a', 'libmp3lame', '-y', str(audio)], check=True)
        cls.audio = audio.read_bytes()
        from PIL import Image
        image = folder / 'cover.jpg'
        Image.new('RGB', (16, 16), 'red').save(image, 'JPEG')
        cls.image = image.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runtime-cd-test-')
        self.folder = Path(self.temp.name)
        self.legacy = self.folder / 'legacy'
        self.legacy.mkdir()
        self.store = RuntimeStore(self.folder / 'data', self.legacy)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def package(self, identity='demo', revision=1, pitch=60, video='abcdefghijk', sample=True, cover=True,
                timestamp=(2026, 1, 1, 0, 0, 0), broken_audio=False):
        module = _contract()
        score = {'id': identity, 'name': identity, 'artist': 'Demo', 'excerpt': 'test',
                 'source_url': '', 'source_sha256': 'a' * 64, 'revision': revision, 'duration_ms': 500,
                 'notes': [{'midi_note': pitch, 'start_ms': 0, 'duration_ms': 500, 'velocity': 100}]}
        score['score_sha256'] = module.score_sha256(score)
        files = {module.SCORES: json.dumps([score]).encode(), module.VIDEOS: json.dumps({'version': 1, 'songs': {
            identity: {'video_id': video, 'title': identity, **({'thumbnail_sha256': hashlib.sha256(self.image).hexdigest()} if cover else {})}}}).encode()}
        artifacts = {'score': {'status': 'ready', 'sha256': score['score_sha256']},
                     'thumbnail': {'status': 'pending'}, 'sample': {'status': 'pending'}}
        if cover:
            relative = f'cogs/tts/cd/thumbnails/{video}.jpg'
            files[relative] = self.image
            artifacts['thumbnail'] = {'status': 'ready', 'path': relative, 'sha256': hashlib.sha256(self.image).hexdigest()}
        if sample:
            audio = b'ID3-corrupt' if broken_audio else self.audio
            audio_hash = hashlib.sha256(audio).hexdigest()
            relative = f'cogs/tts/cd/previews/{identity}.mp3'
            files[relative] = audio
            files[module.PREVIEWS] = json.dumps({identity: {'manifest_version': 2, 'id': identity,
                'revision': revision, 'source_sha256': 'a' * 64, 'score_sha256': score['score_sha256'],
                'voice_id': 'kasane-teto', 'language': 'pt-BR', 'pitch_semitones': 0,
                'speech_rate': 1.0, 'mp3_sha256': audio_hash}}).encode()
            artifacts['sample'] = {'status': 'ready', 'path': relative, 'sha256': audio_hash}
        files[f'cogs/tts/cd/packages/{identity}.json'] = json.dumps({'manifest_version': 1, 'id': identity,
            'revision': revision, 'source_sha256': 'a' * 64, 'score_sha256': score['score_sha256'],
            'artifacts': artifacts}).encode()
        self.serial += 1
        path = self.folder / f'package-{self.serial}.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, data in files.items():
                entry = zipfile.ZipInfo(relative, timestamp)
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data)
        return path

    def publish(self, path, expected=None):
        job = self.store.receive(path, expected_versions=expected)
        return self.store.process(job['job_id'])

    def combined_package(self, *sources):
        module = _contract()
        files, scores, videos, previews = {}, [], {}, {}
        for source in sources:
            with zipfile.ZipFile(source) as archive:
                files.update({name: archive.read(name) for name in archive.namelist()})
                scores.extend(json.loads(archive.read(module.SCORES)))
                videos.update(json.loads(archive.read(module.VIDEOS))['songs'])
                if module.PREVIEWS in archive.namelist():
                    previews.update(json.loads(archive.read(module.PREVIEWS)))
        files.update({module.SCORES: json.dumps(scores).encode(),
                      module.VIDEOS: json.dumps({'version': 1, 'songs': videos}).encode(),
                      module.PREVIEWS: json.dumps(previews).encode()})
        target = self.folder / 'combined.zip'
        with zipfile.ZipFile(target, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return target

    def arrangement_package(self, revision=1, pitch=60):
        from osaka_cd.arrangement import arrangement_sha256, arrangement_performance_sha256, preview_arrangement
        module = _contract()
        source = self.package(revision=revision, pitch=pitch, sample=False)
        with zipfile.ZipFile(source) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        score = json.loads(files[module.SCORES])[0]
        arrangement = {'score_schema': 2, 'duration_ms': 500,
            'parts': [{'id': 'lead', 'role': 'lead', 'lyrics_source': 'vowel', 'notes': score['notes']}],
            'source': {'sha256': score['source_sha256']}, 'warnings': []}
        selected = preview_arrangement(arrangement, start_ms=0, end_ms=500)['arrangement']
        relative = 'cogs/tts/cd/arrangements/demo.json'
        score.update(arrangement_path=relative, arrangement_sha256=arrangement_sha256(arrangement),
            arrangement_preview_start_ms=0, arrangement_preview_end_ms=500,
            preview_arrangement_sha256=arrangement_sha256(selected),
            preview_performance_sha256=arrangement_performance_sha256(selected))
        manifest = json.loads(files['cogs/tts/cd/packages/demo.json'])
        manifest.update(manifest_version=2, arrangement_sha256=score['arrangement_sha256'])
        content = json.dumps(arrangement).encode()
        manifest['artifacts']['arrangement'] = {'status': 'ready', 'path': relative,
                                                'sha256': hashlib.sha256(content).hexdigest()}
        files.update({module.SCORES: json.dumps([score]).encode(), relative: content,
            'cogs/tts/cd/packages/demo.json': json.dumps(manifest).encode()})
        with zipfile.ZipFile(source, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return source

    def test_durable_receipt_and_explicit_activation(self):
        source = self.package()
        received = self.store.receive(source)
        source.unlink()
        reloaded = RuntimeStore(self.folder / 'data', self.legacy)
        self.assertIn(received['job_id'], reloaded.outstanding())
        applied = reloaded.process(received['job_id'])
        self.assertEqual(applied['state'], 'applied', applied)
        self.assertFalse(applied['installed'])
        self.assertTrue(Path(reloaded.snapshot()['root'], 'cogs/tts/cd/previews/demo.mp3').is_file())
        self.assertTrue(reloaded.acknowledge(applied['generation']))
        self.assertTrue(reloaded.job(received['job_id'])['installed'])

    def test_semantic_idempotency_ignores_zip_timestamps(self):
        first = self.package()
        second = self.package(timestamp=(2026, 2, 2, 0, 0, 0))
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        result = self.publish(first)
        retried = self.store.receive(second)
        self.assertEqual(result['job_id'], retried['job_id'])
        self.assertEqual(result['generation'], self.store.process(retried['job_id'])['generation'])

    def test_corrupt_decodable_signature_cannot_replace_active(self):
        good = self.publish(self.package())
        failed = self.publish(self.package(revision=2, pitch=62, broken_audio=True))
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(self.store.snapshot()['generation'], good['generation'])

    def test_simultaneous_songs_merge_and_acknowledge_both(self):
        paths = [self.package('one'), self.package('two', video='12345678901')]
        jobs = [self.store.receive(path) for path in paths]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda job: self.store.process(job['job_id']), jobs))
        self.assertEqual([result['state'] for result in results], ['applied', 'applied'], results)
        self.assertEqual(set(self.store.snapshot()['versions']), {'one', 'two'})
        self.store.acknowledge(self.store.snapshot()['generation'])
        self.assertTrue(all(self.store.job(job['job_id'])['installed'] for job in jobs))

    def test_song_compare_and_swap_rejects_stale_cover_update(self):
        self.publish(self.package())
        version = self.store.snapshot()['versions']['demo']
        changed = self.publish(self.package(video='12345678901'), {'demo': version})
        self.assertEqual(changed['state'], 'applied')
        refused = self.publish(self.package(video='98765432101'), {'demo': version})
        self.assertEqual(refused['error_code'], 'conflict')
        self.assertEqual(self.store.snapshot()['generation'], changed['generation'])

    def test_cover_only_pending_package_retains_compatible_sample(self):
        old = self.publish(self.package())
        current = self.publish(self.package(video='12345678901', sample=False))
        self.assertEqual(current['state'], 'applied', current)
        root = Path(self.store.snapshot()['root'])
        index = json.loads((root / 'cogs/tts/cd/previews/index.json').read_text())
        self.assertEqual(index['demo']['mp3_sha256'], hashlib.sha256(self.audio).hexdigest())
        self.assertEqual(current['validation']['artifacts']['demo']['sample'], 'ready')

    def test_same_video_pending_excerpt_inherits_verified_cover(self):
        self.publish(self.package('first', cover=False, sample=False))
        result = self.publish(self.package('second'))
        self.assertEqual(result['state'], 'applied', result)
        root = Path(self.store.snapshot()['root'])
        videos = json.loads((root / 'cogs/tts/cd_song_videos.json').read_text())['songs']
        self.assertEqual(videos['first']['thumbnail_sha256'], videos['second']['thumbnail_sha256'])

    def test_ready_reused_assets_are_linked_but_previous_indices_stay_immutable(self):
        first = self.publish(self.package())
        first_root = Path(self.store.snapshot()['root'])
        original_index = (first_root / 'cogs/tts/cd_song_videos.json').read_bytes()
        self.publish(self.package(video='12345678901', sample=False))
        new_root = Path(self.store.snapshot()['root'])
        self.assertEqual((first_root / 'cogs/tts/cd_song_videos.json').read_bytes(), original_index)
        self.assertEqual((first_root / 'cogs/tts/cd/previews/demo.mp3').stat().st_ino,
                         (new_root / 'cogs/tts/cd/previews/demo.mp3').stat().st_ino)

    def test_score_change_invalidates_sample_and_rollback_is_per_song(self):
        previous = self.publish(self.package())
        self.publish(self.package('other', video='12345678901'))
        changed = self.publish(self.package(revision=2, pitch=64, sample=False))
        self.assertEqual(changed['validation']['artifacts']['demo']['sample'], 'pending')
        old_root = Path(self.folder / 'data/generations' / previous['generation'])
        self.assertTrue((old_root / 'cogs/tts/cd/previews/demo.mp3').is_file())
        rolled = self.store.rollback('demo', previous['generation'], self.store.snapshot()['versions']['demo'])
        self.assertEqual(rolled['state'], 'applied', rolled)
        self.assertEqual(set(self.store.snapshot()['versions']), {'demo', 'other'})
        self.assertEqual(rolled['validation']['artifacts']['demo']['sample'], 'ready')
        scores = json.loads((Path(self.store.snapshot()['root']) / 'cogs/tts/cd_song_scores.json').read_text())
        self.assertEqual(next(song for song in scores if song['id'] == 'demo')['revision'], 1)

    def test_path_traversal_and_code_uploads_rejected_before_durable_queue(self):
        for relative in ('../escape', 'cogs/tts/cog.py'):
            path = self.folder / 'bad.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr(relative, b'bad')
            with self.assertRaises(StoreError):
                self.store.receive(path)
        self.assertEqual(self.store.outstanding(), [])

    def test_legacy_scores_and_unbound_audio_preserved_without_fabricated_binding(self):
        legacy_package = self.package('old')
        with zipfile.ZipFile(legacy_package) as archive:
            archive.extractall(self.legacy)
        index = self.legacy / 'cogs/tts/cd/previews/index.json'
        value = json.loads(index.read_text())
        value['old'].pop('manifest_version')
        value['old']['score_sha256'] = 'a' * 64
        index.write_text(json.dumps(value))
        published = self.publish(self.package('new', video='12345678901'))
        self.assertEqual(published['state'], 'applied', published)
        root = Path(self.store.snapshot()['root'])
        self.assertEqual(set(self.store.snapshot()['versions']), {'old', 'new'})
        self.assertTrue((root / 'cogs/tts/cd/previews/old.mp3').is_file())
        self.assertNotIn('old', json.loads((root / 'cogs/tts/cd/previews/index.json').read_text()))
        self.assertNotIn('manifest_version', json.loads(index.read_text())['old'])

    def test_failed_commit_keeps_previous_pointer_and_can_resume_processing(self):
        previous = self.publish(self.package())
        received = self.store.receive(self.package('next', video='12345678901'))
        # A process death before the SQLite commit is different from a caught
        # validation error: BaseException leaves a resumable processing job.
        with patch.object(self.store, '_commit', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.process(received['job_id'])
        self.assertEqual(self.store.snapshot()['generation'], previous['generation'])
        reloaded = RuntimeStore(self.folder / 'data', self.legacy)
        self.assertEqual(reloaded.process(received['job_id'])['state'], 'applied')

    def test_rollback_request_is_durable_across_interruption(self):
        original = self.publish(self.package())
        self.publish(self.package(revision=2, pitch=64))
        with patch.object(self.store, '_commit', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.rollback('demo', original['generation'])
        reloaded = RuntimeStore(self.folder / 'data', self.legacy)
        jobs = reloaded.outstanding()
        self.assertEqual(len(jobs), 1)
        result = reloaded.process(jobs[0])
        self.assertEqual(result['state'], 'applied', result)
        root = Path(reloaded.snapshot()['root'])
        self.assertEqual(json.loads((root / 'cogs/tts/cd_song_scores.json').read_text())[0]['revision'], 1)

    def test_storage_failure_backoff_then_recovers_without_new_payload(self):
        received = self.store.receive(self.package())
        with patch.object(self.store, '_commit', side_effect=OSError('disk temporarily full')):
            result = self.store.process(received['job_id'])
        self.assertEqual(result['state'], 'received', result)
        self.assertEqual(result['error_code'], 'storage_unavailable')
        self.assertNotIn(received['job_id'], self.store.outstanding())
        with patch('osaka_cd.store.time.time', return_value=result['retry_at'] + 1):
            self.assertIn(received['job_id'], self.store.outstanding())
            self.assertEqual(self.store.process(received['job_id'])['state'], 'applied')

    def test_manual_retry_rebases_failed_cas_explicitly(self):
        source = self.package()
        received = self.store.receive(source, expected_versions={'demo': 'f' * 64})
        self.assertEqual(self.store.process(received['job_id'])['state'], 'failed')
        self.assertEqual(self.store.receive(source)['state'], 'failed')
        reset = self.store.receive(source, expected_versions={'demo': None}, retry_failed=True)
        self.assertEqual(reset['job_id'], received['job_id'])
        self.assertEqual(self.store.process(reset['job_id'])['state'], 'applied')

    def test_first_publication_can_rollback_an_existing_legacy_song(self):
        with zipfile.ZipFile(self.package()) as archive:
            archive.extractall(self.legacy)
        changed = self.publish(self.package(revision=2, pitch=64))
        self.assertEqual(changed['state'], 'applied', changed)
        result = self.store.rollback('demo')
        self.assertEqual(result['state'], 'applied', result)
        root = Path(self.store.snapshot()['root'])
        self.assertEqual(json.loads((root / 'cogs/tts/cd_song_scores.json').read_text())[0]['revision'], 1)

    def test_falsey_non_object_expected_versions_are_rejected(self):
        source = self.package()
        for value in ([], False, 0, ''):
            with self.subTest(value=value), self.assertRaises(StoreError):
                self.store.receive(source, expected_versions=value)
        self.assertEqual(self.store.outstanding(), [])

    def test_first_publication_cas_uses_advertised_legacy_song_version(self):
        with zipfile.ZipFile(self.package()) as archive:
            archive.extractall(self.legacy)
        original_score = (self.legacy / _contract().SCORES).read_bytes()
        status = self.store.snapshot(strict=True, advertise_legacy=True)
        self.assertFalse(status['initialized'])
        self.assertIsNone(status['generation'])
        self.assertIsNone(status['activated_generation'])
        self.assertEqual(set(status['versions']), {'demo'})
        self.assertEqual(len(status['versions']['demo']), 64)
        updated = self.publish(self.package(video='12345678901', sample=False), status['versions'])
        self.assertEqual(updated['state'], 'applied', updated)
        self.assertFalse(updated['installed'])
        self.assertEqual(updated['validation']['artifacts']['demo']['sample'], 'ready')
        self.assertEqual((self.legacy / _contract().SCORES).read_bytes(), original_score)

    def test_legacy_normalization_is_identical_in_status_and_first_cas(self):
        with zipfile.ZipFile(self.package()) as archive:
            archive.extractall(self.legacy)
        index = self.legacy / _contract().PREVIEWS
        data = json.loads(index.read_text())
        data['demo'].pop('manifest_version')
        index.write_text(json.dumps(data))
        videos = self.legacy / _contract().VIDEOS
        raw = json.loads(videos.read_text())
        raw['songs']['demo'].pop('thumbnail_sha256')
        videos.write_text(json.dumps(raw))
        status = self.store.snapshot(strict=True, advertise_legacy=True)
        self.assertEqual(set(status['versions']), {'demo'})
        updated = self.publish(self.package(video='12345678901', sample=False), status['versions'])
        self.assertEqual(updated['state'], 'applied', updated)
        self.assertEqual(updated['validation']['artifacts']['demo']['sample'], 'pending')
        self.assertNotIn('manifest_version', json.loads(index.read_text())['demo'])
        self.assertNotIn('thumbnail_sha256', json.loads(videos.read_text())['songs']['demo'])

    def test_changed_legacy_song_invalidates_initial_status_cas(self):
        with zipfile.ZipFile(self.package()) as archive:
            archive.extractall(self.legacy)
        status = self.store.snapshot(strict=True, advertise_legacy=True)
        videos = self.legacy / _contract().VIDEOS
        raw = json.loads(videos.read_text())
        raw['songs']['demo']['title'] = 'Changed after status'
        videos.write_text(json.dumps(raw))
        refreshed = self.store.snapshot(strict=True, advertise_legacy=True)
        self.assertNotEqual(refreshed['versions']['demo'], status['versions']['demo'])
        refused = self.publish(self.package(video='12345678901'), status['versions'])
        self.assertEqual(refused['error_code'], 'conflict')
        self.assertFalse(self.store.snapshot()['initialized'])

    def test_existing_legacy_song_cannot_be_claimed_absent(self):
        with zipfile.ZipFile(self.package()) as archive:
            archive.extractall(self.legacy)
        refused = self.publish(self.package(video='12345678901'), {'demo': None})
        self.assertEqual(refused['error_code'], 'conflict')
        self.assertFalse(self.store.snapshot()['initialized'])

    def test_historical_activation_does_not_claim_current_installation(self):
        source = self.package()
        first = self.publish(source)
        self.store.acknowledge(first['generation'])
        changed = self.publish(self.package(video='12345678901'))
        historical = self.store.receive(source)
        self.assertFalse(historical['installed'])
        self.assertFalse(historical['current'])
        self.assertEqual(historical['error_code'], 'superseded')
        fresh = self.store.receive(source, expected_versions=self.store.snapshot()['versions'], retry_failed=True)
        self.assertEqual(fresh['state'], 'received')
        restored = self.store.process(fresh['job_id'])
        self.assertEqual(restored['state'], 'applied', restored)
        self.assertNotEqual(restored['generation'], changed['generation'])

    def test_rollback_restores_old_arrangement_json(self):
        first = self.publish(self.arrangement_package())
        self.assertEqual(first['state'], 'applied', first)
        old_root = Path(self.store.snapshot()['root'])
        expected = (old_root / 'cogs/tts/cd/arrangements/demo.json').read_bytes()
        updated = self.publish(self.arrangement_package(revision=2, pitch=64))
        self.assertEqual(updated['state'], 'applied', updated)
        rolled = self.store.rollback('demo', first['generation'])
        self.assertEqual(rolled['state'], 'applied', rolled)
        root = Path(self.store.snapshot()['root'])
        self.assertEqual((root / 'cogs/tts/cd/arrangements/demo.json').read_bytes(), expected)
        self.assertEqual(_contract().validate_cd_tree(root, ['demo'])['artifacts']['demo']['arrangement'], 'ready')

    def test_shared_video_cover_rollback_never_corrupts_other_song(self):
        first = self.publish(self.package('one'))
        self.publish(self.package('two'))
        from PIL import Image
        cover = self.folder / 'blue.jpg'
        Image.new('RGB', (16, 16), 'blue').save(cover, 'JPEG')
        self.image = cover.read_bytes()
        updated = self.publish(self.combined_package(self.package('one'), self.package('two')))
        self.assertEqual(updated['state'], 'applied', updated)
        current = self.store.snapshot()
        with self.assertRaises(StoreError) as caught:
            self.store.rollback('one', first['generation'])
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(self.store.snapshot()['generation'], current['generation'])
        _contract().validate_cd_tree(current['root'], ['one', 'two'])

    def test_concurrent_rollback_processing_cannot_overwrite_applied_job(self):
        first = self.publish(self.package())
        self.publish(self.package(revision=2, pitch=64))
        secondary = RuntimeStore(self.store.root, self.legacy)
        committed, processing, release = threading.Event(), threading.Event(), threading.Event()
        commit = self.store._commit
        rollback = secondary.rollback
        def delayed_commit(*args, **kwargs):
            committed.set()
            self.assertTrue(release.wait(5))
            return commit(*args, **kwargs)
        def scheduled_rollback(*args, **kwargs):
            processing.set()
            return rollback(*args, **kwargs)
        with patch.object(self.store, '_commit', side_effect=delayed_commit), \
             patch.object(secondary, 'rollback', side_effect=scheduled_rollback), ThreadPoolExecutor(max_workers=2) as pool:
            direct = pool.submit(self.store.rollback, 'demo', first['generation'])
            self.assertTrue(committed.wait(5))
            pending = secondary.outstanding()
            self.assertEqual(len(pending), 1)
            worker = pool.submit(secondary.process, pending[0])
            self.assertTrue(processing.wait(5))
            release.set()
            self.assertEqual(direct.result(10)['state'], 'applied')
            self.assertEqual(worker.result(10)['state'], 'applied')
        self.assertEqual(secondary.job(pending[0])['state'], 'applied')

    def test_rollback_is_queued_durably_without_request_thread_decoding(self):
        first = self.publish(self.package())
        self.publish(self.package(revision=2, pitch=64))
        current = self.store.snapshot()['generation']
        with patch.object(_contract(), 'validate_cd_tree', side_effect=AssertionError('must not decode on admission')):
            received = self.store.enqueue_rollback('demo', first['generation'])
        self.assertEqual(received['state'], 'received')
        self.assertEqual(self.store.snapshot()['generation'], current)
        restarted = RuntimeStore(self.store.root, self.legacy)
        self.assertEqual(restarted.process(received['job_id'])['state'], 'applied')

    def test_storage_quota_preserves_catalog_and_duplicate_receipt(self):
        source = self.package()
        first = self.publish(source)
        with patch.dict('os.environ', {'OSAKA_CD_MAX_STORAGE_BYTES': '1'}):
            self.assertEqual(self.store.receive(source)['job_id'], first['job_id'])
            with self.assertRaises(StoreError) as caught:
                self.store.receive(self.package('another'))
        self.assertEqual(caught.exception.code, 'storage_quota')
        self.assertEqual(self.store.snapshot()['generation'], first['generation'])

    def test_oversized_reader_index_cannot_activate_and_staging_is_recovered(self):
        first = self.publish(self.package())
        orphan = self.store.root / 'staging/catalog-interrupted'
        orphan.mkdir()
        (orphan / 'partial').write_bytes(b'abandoned')
        orphan_generation = self.store.root / 'generations/g-interrupted-before-pointer'
        orphan_generation.mkdir()
        (orphan_generation / 'partial').write_bytes(b'abandoned')
        orphan_received = self.store.root / ('received/' + 'f' * 64 + '.zip')
        orphan_received.write_bytes(b'abandoned')
        received = self.store.receive(self.package('another'))
        with patch('osaka_cd.store.MAX_INDEX_BYTES', 16):
            result = self.store.process(received['job_id'])
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(orphan.exists())
        self.assertFalse(orphan_generation.exists())
        self.assertFalse(orphan_received.exists())
        self.assertEqual(self.store.snapshot()['generation'], first['generation'])


if __name__ == '__main__':
    unittest.main()
