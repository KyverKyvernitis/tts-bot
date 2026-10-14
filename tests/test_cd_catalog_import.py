"""Offline integration: real MIDI, real MP3 and updater-compatible ZIPs.

Run without optional pytest: python -m unittest discover -s tests -p test_cd_catalog_import.py
"""
from __future__ import annotations
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'
sys.path.insert(0, str(HERE))
import catalog_import as importer
from cd_metadata import MetadataSearchError
from package_validation import validate_package, score_sha256

SPEC = importlib.util.spec_from_file_location('cd_import_cli_tests', HERE / 'import-midi.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def midi(pitch=60):
    # PPQ 480, simultaneous pitches deliberately exercise polyphonic extraction.
    events = bytes([0, 144, pitch, 100, 0, 144, pitch + 7, 80,
                    131, 96, 128, pitch, 0, 0, 128, pitch + 7, 0,
                    0, 255, 47, 0])
    return b'MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk' + len(events).to_bytes(4, 'big') + events


class ImportIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = tempfile.TemporaryDirectory(prefix='cd-import-test-assets-')
        folder = Path(cls.artifacts.name)
        cls.audio_path, cls.cover_path = folder / 'sample.mp3', folder / 'cover.jpg'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.6',
                        '-codec:a', 'libmp3lame', '-b:a', '128k', '-y', str(cls.audio_path)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=16x16',
                        '-frames:v', '1', '-threads', '1', '-y', str(cls.cover_path)], check=True)
        cls.audio, cls.image = cls.audio_path.read_bytes(), cls.cover_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.artifacts.cleanup()

    def setUp(self):
        # Restricted CI disallows asyncio's thread wakeup socket. Exercise the
        # same serial work inline; no MIDI, renderer output or validator mock.
        async def inline_thread(function, *args, **kwargs):
            return function(*args, **kwargs)
        self.thread_patch = patch.object(asyncio, 'to_thread', side_effect=inline_thread)
        self.thread_patch.start()
        self.temporary = tempfile.TemporaryDirectory(prefix='cd-import-integration-')
        self.folder = Path(self.temporary.name)
        self.root = self.folder / 'data'
        self.root.mkdir()
        (self.root / 'cd_song_scores.json').write_text('[]')
        (self.root / 'cd_song_videos.json').write_text('{"version":1,"songs":{}}')
        self.inbox = self.folder / 'inbox'
        self.inbox.mkdir()
        self.path = self.inbox / 'Demo - Test Song.mid'
        self.path.write_bytes(midi())
        self.hits = self.folder / 'candidates.json'
        self.hits.write_text(json.dumps([{'video_id': 'abcdefghijk', 'title': 'Demo Test Song',
                                         'channel': 'Demo', 'view_count': 500, 'duration': 180}]))
        self.output = self.folder / 'output'
        self.args = SimpleNamespace(inbox=self.inbox, data_root=self.root, cache_dir=self.folder / 'cache',
                                    output=self.output, candidates=self.hits, cover_file=self.cover_path,
                                    no_sample=False, force=False, retry=True, publish_url=None)
        # Pillow is an explicit kit dependency. In minimal CI, still use actual
        # JPEG bytes and hash, and let shared validation do its signature checks.
        try:
            import PIL
            self.cover_patch = None
        except ImportError:
            self.cover_patch = patch.object(importer, '_verify_cover', side_effect=lambda data: hashlib.sha256(data).hexdigest())
            self.cover_patch.start()

    def tearDown(self):
        self.thread_patch.stop()
        if self.cover_patch:
            self.cover_patch.stop()
        self.temporary.cleanup()

    def render(self, melody, target):
        target.write_bytes(self.audio)
        from worker_config import configuration_fingerprint
        melody['_render_report'] = {'configuration_fingerprint': configuration_fingerprint(),
                                   'mix_sha256': hashlib.sha256(self.audio).hexdigest(), 'worker_version': '1.11.41'}
        return 'ready'

    def run_import(self, **kwargs):
        with patch.object(importer, '_sample', side_effect=self.render):
            return asyncio.run(importer.import_midi(self.path, root=self.root, cache_dir=self.folder / 'cache',
                                                     candidates=self.hits, cover_file=self.cover_path,
                                                     export_zip=self.output / 'cd.zip', **kwargs))

    def test_real_polyphonic_midi_is_packaged_with_real_decodable_mp3(self):
        result = self.run_import()
        self.assertTrue(result['complete'])
        self.assertTrue(Path(result['zip']).is_file())
        self.assertEqual(result['extraction']['vocal_status'], 'ambiguous')
        self.assertIn('polyphonic_upper_voice_heuristic', result['extraction']['warnings'])
        self.assertNotEqual(result['score_sha256'], result['source_sha256'])
        self.assertEqual(result['validation']['artifacts']['test-song']['audio']['verified'], 'decoded')
        self.assertEqual(result['validation']['songs'], ['test-song'])
        with zipfile.ZipFile(result['zip']) as archive:
            self.assertIsNone(archive.testzip())
            scores = json.loads(archive.read('cogs/tts/cd_song_scores.json'))
            previews = json.loads(archive.read('cogs/tts/cd/previews/index.json'))
            self.assertEqual(len(scores), 1)
            self.assertTrue(all(set(note) == {'midi_note', 'start_ms', 'duration_ms', 'velocity'} for note in scores[0]['notes']))
            self.assertEqual(previews['test-song']['manifest_version'], 3)
            self.assertEqual(previews['test-song']['score_sha256'], score_sha256(scores[0]))
        validate_package(result['zip'], require_complete=True, audio_probe=True, require_cd_only=True)

    def test_ready_sample_survives_metadata_outage_and_cover_change(self):
        original = self.run_import()
        manifest = json.loads((self.root / 'previews/index.json').read_text())['test-song']
        with patch.object(importer, '_search', side_effect=MetadataSearchError('offline')), \
             patch.object(importer, '_sample', side_effect=AssertionError('must retain ready audio')):
            result = asyncio.run(importer.import_midi(self.path, root=self.root, cover_file=self.cover_path,
                                                     export_zip=self.output / 'outage.zip'))
        self.assertEqual(result['sample_status'], 'ready')
        self.assertEqual(original['score_sha256'], result['score_sha256'])
        self.assertEqual(manifest['mp3_sha256'], json.loads((self.root / 'previews/index.json').read_text())['test-song']['mp3_sha256'])
        validate_package(result['zip'], require_complete=True, audio_probe=True)

    def test_omitted_sidecar_aliases_survive_reimport_and_prior_video_is_reverified(self):
        self.path.with_suffix('.json').write_text(json.dumps({
            'aliases': ['別名'], 'artist_aliases': ['別名作者']}))
        initial = self.run_import()
        self.path.with_suffix('.json').write_text('{}')
        self.hits.write_text(json.dumps([{'video_id': '12345678901', 'title': 'Demo 別名',
                                         'channel': 'Demo', 'view_count': 600, 'duration': 180}]))
        captured = []
        search = importer._search

        def inspect(meta, **kwargs):
            captured.append(meta)
            return search(meta, **kwargs)

        with patch.object(importer, '_search', side_effect=inspect), \
             patch.object(importer, '_sample', side_effect=AssertionError('retain ready sample')):
            updated = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                                                       cover_file=self.cover_path,
                                                       export_zip=self.output / 'aliases.zip'))
        self.assertTrue(updated['complete'])
        self.assertEqual(updated['video_id'], '12345678901')
        self.assertEqual(captured[0]['aliases'], ['別名'])
        self.assertEqual(captured[0]['artist_aliases'], ['別名作者'])
        self.assertIn(initial['video_id'], captured[0]['seed_video_ids'])
        saved = json.loads((self.root / 'cd_song_videos.json').read_text())['songs']['test-song']
        self.assertEqual(saved['title_aliases'], ['Test Song', '別名'])
        self.assertEqual(saved['artist_aliases'], ['別名作者'])
        self.assertEqual(saved['channel'], 'Demo')
        validate_package(updated['zip'], require_complete=True, audio_probe=True)

    def test_alias_inheritance_respects_explicit_clearing_and_changed_identity(self):
        original = {'name': 'Test Song', 'artist': 'Demo'}
        prior = {'title_aliases': ['Test Song', '別名'], 'artist_aliases': ['別名作者']}
        meta = importer._meta_from_path(self.path)
        cleared = importer._inherit_aliases({**meta, '_explicit_fields': {'aliases', 'artist_aliases'}}, original, prior)
        self.assertEqual(cleared['aliases'], [])
        self.assertEqual(cleared['artist_aliases'], [])
        for replacement in ({'title': 'Other Song'}, {'artist': 'Other Artist'}):
            result = importer._inherit_aliases({**meta, **replacement}, original, prior)
            self.assertEqual(result['aliases'], [])
            self.assertEqual(result['artist_aliases'], [])

    def test_successful_fresh_search_does_not_use_stale_counts_to_choose_winner(self):
        self.run_import()
        self.hits.write_text(json.dumps([{'video_id': '12345678901', 'title': 'Demo Test Song',
                                         'channel': 'Demo', 'view_count': 100, 'duration': 180}]))
        updated = self.run_import()
        self.assertEqual(updated['video_id'], '12345678901')
        self.assertEqual(updated['view_count'], 100)

    def test_score_change_invalidates_old_audio_without_deleting_rollback_bytes(self):
        self.run_import()
        self.path.write_bytes(midi(62))
        with patch.object(importer, '_sample', return_value='pending_battery_or_battery_status'):
            result = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                                                     cover_file=self.cover_path, export_zip=self.output / 'changed.zip'))
        self.assertEqual(result['revision'], 2)
        self.assertNotIn('test-song', json.loads((self.root / 'previews/index.json').read_text()))
        self.assertEqual((self.root / 'previews/test-song.mp3').read_bytes(), self.audio)
        with zipfile.ZipFile(result['zip']) as archive:
            self.assertNotIn('cogs/tts/cd/previews/test-song.mp3', archive.namelist())
        self.assertEqual(result['validation']['artifacts']['test-song']['sample'], 'pending')

    def test_next_song_package_is_incremental_and_preserves_local_catalog(self):
        self.run_import()
        self.path.with_suffix('.json').write_text(json.dumps({'id': 'second-song', 'title': 'Test Song', 'artist': 'Demo'}))
        result = self.run_import()
        self.assertEqual(len(json.loads((self.root / 'cd_song_scores.json').read_text())), 2)
        with zipfile.ZipFile(result['zip']) as archive:
            self.assertEqual([song['id'] for song in json.loads(archive.read('cogs/tts/cd_song_scores.json'))], ['second-song'])
            self.assertEqual(set(json.loads(archive.read('cogs/tts/cd/previews/index.json'))), {'second-song'})

    def test_source_change_same_score_reuses_audio_and_refreshes_source_hash(self):
        original = self.run_import()
        raw = midi()
        events = b'\x00\xff\x03\x04Name' + raw[22:]
        self.path.write_bytes(raw[:18] + len(events).to_bytes(4, 'big') + events)
        with patch.object(importer, '_sample', side_effect=AssertionError('canonical score has not changed')):
            result = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                                                     cover_file=self.cover_path, export_zip=self.output / 'same-score.zip'))
        self.assertNotEqual(original['source_sha256'], result['source_sha256'])
        self.assertEqual(original['score_sha256'], result['score_sha256'])
        self.assertEqual(result['revision'], 2)
        self.assertEqual(json.loads((self.root / 'previews/index.json').read_text())['test-song']['source_sha256'], result['source_sha256'])
        validate_package(result['zip'], require_complete=True, audio_probe=True)

    def test_retry_sample_exports_whole_cd_even_without_source_in_inbox(self):
        with patch.object(importer, '_sample', return_value='pending_battery_or_battery_status'), \
             patch.object(cli, '_resource_ready', return_value=False):
            result = asyncio.run(cli.batch(self.args))
        self.assertTrue(result['processed'])
        self.path.unlink()
        with patch.object(cli, '_resource_ready', return_value=True), patch.object(cli, '_sample', side_effect=self.render):
            result = asyncio.run(cli.batch(self.args))
        self.assertEqual(result['processed'][0]['sample_status'], 'ready')
        with zipfile.ZipFile(result['processed'][0]['zip']) as archive:
            self.assertIn('cogs/tts/cd_song_scores.json', archive.namelist())
            self.assertIn('cogs/tts/cd_song_videos.json', archive.namelist())
            self.assertIn('cogs/tts/cd/thumbnails/abcdefghijk.jpg', archive.namelist())
            self.assertIn('cogs/tts/cd/previews/test-song.mp3', archive.namelist())
        validate_package(result['processed'][0]['zip'], require_complete=True, audio_probe=True)

    def test_pending_sample_without_midi_obeys_retry_cooldown(self):
        with patch.object(importer, '_sample', return_value='pending_worker_busy'), patch.object(cli, '_resource_ready', return_value=False):
            asyncio.run(cli.batch(self.args))
        self.path.unlink()
        self.args.retry = False
        with patch.object(cli, '_resource_ready', return_value=True), \
             patch.object(cli, '_sample', side_effect=AssertionError('sample cooldown must persist without source MIDI')):
            waiting = asyncio.run(cli.batch(self.args))
        self.assertEqual(waiting['processed'], [])
        self.args.retry = True
        with patch.object(cli, '_resource_ready', return_value=True), patch.object(cli, '_sample', side_effect=self.render):
            ready = asyncio.run(cli.batch(self.args))
        self.assertEqual(ready['processed'][0]['sample_status'], 'ready')

    def test_authentication_change_recovers_permanent_sample_without_source_midi(self):
        def reject(melody, target):
            melody['_render_report'] = {'status': 'pending_worker_auth_not_configured', 'retryable': False}
            return 'pending_worker_auth_not_configured'
        with patch.object(importer, '_sample', side_effect=reject), patch.object(cli, '_resource_ready', return_value=False):
            asyncio.run(cli.batch(self.args))
        self.path.unlink()
        self.args.retry = False
        token = 'new-private-worker-token-never-exposed'
        with patch.dict('os.environ', {'PHONE_WORKER_TOKEN': token}), patch.object(cli, '_resource_ready', return_value=True), \
             patch.object(cli, '_sample', side_effect=self.render):
            recovered = asyncio.run(cli.batch(self.args))
        self.assertEqual(recovered['processed'][0]['sample_status'], 'ready')
        self.assertNotIn(token, json.dumps(recovered))

    def test_permanent_sample_failure_waits_for_input_or_configuration_change(self):
        import os, time
        os.utime(self.path, (time.time() - 6, time.time() - 6))
        def reject(melody, target):
            melody['_render_report'] = {'status': 'pending_note_exceeds_vowel_limit', 'retryable': False}
            return 'pending_note_exceeds_vowel_limit'
        with patch.object(importer, '_sample', side_effect=reject), patch.object(cli, '_resource_ready', return_value=False):
            initial = asyncio.run(cli.batch(self.args))
        self.assertFalse(initial['processed'][0]['pending_artifacts'][0]['retryable'])
        self.args.retry = False
        with patch.object(cli, '_resource_ready', return_value=True), \
             patch.object(cli, 'import_midi', side_effect=AssertionError('permanent sample must wait')), \
             patch.object(cli, '_sample', side_effect=AssertionError('permanent sample must wait')):
            waiting = asyncio.run(cli.batch(self.args))
        self.assertEqual(waiting['pending'][0]['reason'], 'permanent_artifact_error')
        self.assertIn('permanent_sample_error', {row['reason'] for row in waiting['pending']})
        self.assertFalse(waiting['failures'])

    def test_permanent_failure_retries_immediately_when_midi_changes(self):
        self.path.write_bytes(b'not a midi')
        with patch.object(cli, '_resource_ready', return_value=False):
            result = asyncio.run(cli.batch(self.args))
        self.assertEqual(result['failures'][0]['kind'], 'permanent')
        self.args.retry = False
        import os, time
        os.utime(self.path, (time.time() - 6, time.time() - 6))
        with patch.object(cli, '_resource_ready', return_value=False):
            repeated = asyncio.run(cli.batch(self.args))
        self.assertEqual(repeated['pending'][0]['reason'], 'permanent_input_error')
        self.path.write_bytes(midi())
        os.utime(self.path, (time.time() - 6, time.time() - 6))
        with patch.object(importer, '_sample', side_effect=self.render), patch.object(cli, '_resource_ready', return_value=False):
            fixed = asyncio.run(cli.batch(self.args))
        self.assertTrue(fixed['processed'])
        self.assertFalse(fixed['failures'])

    def test_manual_retry_ignores_temporary_cooldown(self):
        with patch.object(cli, 'import_midi', side_effect=OSError('offline')), patch.object(cli, '_resource_ready', return_value=False):
            failed = asyncio.run(cli.batch(self.args))
        self.assertEqual(failed['failures'][0]['kind'], 'temporary')
        self.args.retry = False
        import os, time
        os.utime(self.path, (time.time() - 6, time.time() - 6))
        blocked = asyncio.run(cli.batch(self.args))
        self.assertEqual(blocked['pending'][0]['reason'], 'retry_cooldown')
        self.args.retry = True
        with patch.object(importer, '_sample', side_effect=self.render), patch.object(cli, '_resource_ready', return_value=False):
            retried = asyncio.run(cli.batch(self.args))
        self.assertTrue(retried['processed'])

    def test_failed_upload_retries_existing_validated_zip_without_reimport(self):
        import cd_publish
        self.args.publish_url = 'https://example.invalid/api/osaka/cd/publish'
        self.args._publication_configuration = {'publish_url': self.args.publish_url, 'token': 'fixture-token-more-than-thirty-two-characters'}
        with patch.object(importer, '_sample', side_effect=self.render), \
             patch.object(cli, '_resource_ready', return_value=False), \
             patch.object(cd_publish, 'publication_status', return_value={'ok': True, 'versions': {}}), \
             patch.object(cd_publish, 'publish_package', side_effect=OSError('unavailable')) as upload:
            initial = asyncio.run(cli.batch(self.args))
        self.assertEqual(upload.call_count, 1)
        self.assertFalse(initial['published_to_vps'])
        original_zip = initial['processed'][0]['zip']
        response = {'state': 'queued', 'installed': False, 'published_to_vps': True, 'candidate_id': 'candidate-test'}
        with patch.object(cli, 'import_midi', side_effect=AssertionError('reuse validated package')), \
             patch.object(cd_publish, 'publish_package', return_value=response), \
             patch.object(cli, '_resource_ready', return_value=False):
            retried = asyncio.run(cli.batch(self.args))
        self.assertTrue(retried['published_to_vps'])
        self.assertFalse(retried['installed_on_vps'])
        self.assertEqual(json.loads((self.root / 'pending_publications.json').read_text())[retried['processed'][0]['zip_sha256']]['zip'], original_zip)
        self.assertTrue(json.loads((self.root / 'pending_publications.json').read_text()))

    def test_enabling_publication_after_offline_import_reuses_existing_package(self):
        import cd_publish
        with patch.object(importer, '_sample', side_effect=self.render), patch.object(cli, '_resource_ready', return_value=False):
            imported = asyncio.run(cli.batch(self.args))
        original_zip = imported['processed'][0]['zip']
        self.args._publication_configuration = {'publish_url': 'https://example.test/api/osaka/cd/publish',
                                              'token': 'fixture-token-more-than-thirty-two-characters'}
        response = {'state': 'activated', 'installed': True, 'published_to_vps': True, 'generation': 'fixture-generation'}
        with patch.object(cli, 'import_midi', side_effect=AssertionError('existing validated artifact must suffice')), \
             patch.object(cd_publish, 'publication_status', return_value={'ok': True, 'versions': {}}), \
             patch.object(cd_publish, 'publish_package', return_value=response) as upload, \
             patch.object(cli, '_resource_ready', return_value=False):
            published = asyncio.run(cli.batch(self.args))
            self.assertTrue(published['installed_on_vps'])
            self.assertEqual(str(upload.call_args.args[0]), original_zip)
            self.assertEqual(upload.call_count, 1)
            idle = asyncio.run(cli.batch(self.args))
            self.assertEqual(upload.call_count, 1)
            self.assertEqual(idle['pending'], [])

    def test_harmony_change_invalidates_audio_even_when_lead_score_is_identical(self):
        original = self.run_import()
        raw = bytearray(midi())
        raw[24] = 62  # Lower note-on; upper lead remains G4.
        raw[33] = 62  # Matching lower note-off.
        self.path.write_bytes(raw)
        with patch.object(importer, '_sample', return_value='pending_worker_busy'):
            changed = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                cover_file=self.cover_path, export_zip=self.output / 'harmony.zip'))
        self.assertEqual(changed['score_sha256'], original['score_sha256'])
        self.assertNotEqual(changed['arrangement_sha256'], original['arrangement_sha256'])
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(changed['sample_status'], 'pending_worker_busy')
        self.assertNotIn('test-song', json.loads((self.root / 'previews/index.json').read_text()))
        self.assertEqual((self.root / 'previews/test-song.mp3').read_bytes(), self.audio)

    def test_full_arrangement_and_preview_have_exact_bindings(self):
        result = self.run_import()
        with zipfile.ZipFile(result['zip']) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        score = json.loads(files['cogs/tts/cd_song_scores.json'])[0]
        preview = json.loads(files['cogs/tts/cd/previews/index.json'])['test-song']
        self.assertIn(score['arrangement_path'], files)
        self.assertEqual(preview['source_arrangement_sha256'], score['arrangement_sha256'])
        self.assertEqual(preview['arrangement_sha256'], score['preview_arrangement_sha256'])
        full = json.loads(files[score['arrangement_path']])
        self.assertEqual(len(full['parts']), 2)
        full['parts'][1]['notes'][0]['midi_note'] += 1
        files[score['arrangement_path']] = json.dumps(full).encode()
        damaged = self.output / 'damaged.zip'
        with zipfile.ZipFile(damaged, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'hash do arranjo'):
            validate_package(damaged, require_cd_only=True, audio_probe=True)

    def test_publication_rejects_metadata_the_runtime_cannot_load(self):
        result = self.run_import()
        with zipfile.ZipFile(result['zip']) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        score = json.loads(files['cogs/tts/cd_song_scores.json'])[0]
        for field, limit in (('name', 100), ('artist', 200), ('excerpt', 500), ('source_url', 2048)):
            changed = dict(score, **{field: 'x' * (limit + 1)})
            damaged = self.output / f'invalid-{field}.zip'
            with zipfile.ZipFile(damaged, 'w') as archive:
                for name, data in files.items():
                    archive.writestr(name, json.dumps([changed]).encode() if name == 'cogs/tts/cd_song_scores.json' else data)
            with self.assertRaisesRegex(ValueError, field + ' inválido'):
                validate_package(damaged, require_cd_only=True)

    def test_decodable_audio_with_wrong_duration_is_not_a_complete_sample(self):
        result = self.run_import()
        long_audio = self.folder / 'wrong-duration.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                        '-codec:a', 'libmp3lame', '-y', str(long_audio)], check=True)
        data = long_audio.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        with zipfile.ZipFile(result['zip']) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        preview = json.loads(files['cogs/tts/cd/previews/index.json'])
        preview['test-song']['mp3_sha256'] = digest
        files['cogs/tts/cd/previews/index.json'] = json.dumps(preview).encode()
        package = json.loads(files['cogs/tts/cd/packages/test-song.json'])
        package['artifacts']['sample']['sha256'] = digest
        files['cogs/tts/cd/packages/test-song.json'] = json.dumps(package).encode()
        files['cogs/tts/cd/previews/test-song.mp3'] = data
        damaged = self.output / 'wrong-duration.zip'
        with zipfile.ZipFile(damaged, 'w') as archive:
            for name, contents in files.items():
                archive.writestr(name, contents)
        with self.assertRaisesRegex(ValueError, 'duração da amostra'):
            validate_package(damaged, require_cd_only=True, audio_probe=True)

    def test_legacy_audio_migration_is_explicit_and_limited_to_one_part(self):
        self.path.with_suffix('.json').write_text(json.dumps({'max_parts': 1}))
        self.run_import()
        indexes = json.loads((self.root / 'previews/index.json').read_text())
        indexes['test-song'] = {key: value for key, value in indexes['test-song'].items()
            if key not in {'arrangement_sha256', 'source_arrangement_sha256', 'arrangement_performance_sha256', 'rendering_profile'}}
        indexes['test-song']['manifest_version'] = 2
        (self.root / 'previews/index.json').write_text(json.dumps(indexes))
        with patch.object(importer, '_sample', side_effect=AssertionError('matching one-part legacy audio should survive')):
            migrated = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                cover_file=self.cover_path, export_zip=self.output / 'migrated.zip'))
        self.assertEqual(migrated['sample_status'], 'ready')
        item = json.loads((self.root / 'previews/index.json').read_text())['test-song']
        self.assertEqual(item['rendering_profile']['mode'], 'legacy-monophonic-migration')
        self.path.with_suffix('.json').write_text(json.dumps({'max_parts': 2}))
        with patch.object(importer, '_sample', return_value='pending_worker_busy'):
            expanded = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                cover_file=self.cover_path, export_zip=self.output / 'expanded.zip'))
        self.assertEqual(expanded['sample_status'], 'pending_worker_busy')
        self.assertEqual(expanded['parts'], 2)

    def test_configuration_change_invalidates_previously_accepted_legacy_audio(self):
        self.path.with_suffix('.json').write_text(json.dumps({'max_parts': 1}))
        self.run_import()
        indexes = json.loads((self.root / 'previews/index.json').read_text())
        indexes['test-song'] = {key: value for key, value in indexes['test-song'].items()
            if key not in {'arrangement_sha256', 'source_arrangement_sha256', 'arrangement_performance_sha256', 'rendering_profile'}}
        indexes['test-song']['manifest_version'] = 2
        (self.root / 'previews/index.json').write_text(json.dumps(indexes))
        with patch.object(importer, '_sample', side_effect=AssertionError('migration must reuse old audio')):
            self.run_import()
        with patch('worker_config.configuration_fingerprint', return_value='f'*64), \
             patch.object(importer, '_sample', return_value='pending_worker_busy'):
            changed = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                cover_file=self.cover_path, export_zip=self.output / 'configuration-changed.zip'))
        self.assertEqual(changed['sample_status'], 'pending_worker_busy')

    def test_real_supplied_rolling_audio_survives_v9_arrangement_migration(self):
        import shutil
        bundled = Path('/workspace/delivery/osaka-cd-termux-kit-v8/recovery/state')
        source = Path('/workspace/recovery/midi/wowaka - Rolling Girl.mid')
        if not source.is_file() or not (bundled / 'previews/rolling-girl.mp3').is_file():
            self.skipTest('User-provided Rolling Girl recovery assets are unavailable')
        shutil.copytree(bundled, self.root, dirs_exist_ok=True)
        self.path.unlink()
        self.path = self.inbox / 'wowaka - Rolling Girl.mid'
        self.path.write_bytes(source.read_bytes())
        self.path.with_suffix('.json').write_text(json.dumps({'id': 'rolling-girl', 'title': 'Rolling Girl',
            'artist': 'wowaka', 'aliases': ['Rollin Girl', 'ローリンガール'], 'artist_aliases': ['現実逃避P']}))
        self.hits.write_text(json.dumps([{'video_id': 'vnw8zURAxkU', 'title': 'wowaka Rolling Girl',
            'channel': 'wowaka', 'view_count': 44780292, 'duration': 191,
            'view_count_observed_at': '2026-10-13T17:18:48Z'}]))
        previous = (self.root / 'previews/rolling-girl.mp3').read_bytes()
        with patch.object(importer, '_sample', side_effect=AssertionError('Original validated MP3 must remain byte-identical')):
            result = asyncio.run(importer.import_midi(self.path, root=self.root, candidates=self.hits,
                cover_file=self.cover_path, export_zip=self.output / 'real-rolling-migration.zip'))
        self.assertEqual(result['notes'], 72)
        self.assertEqual(result['duration_ms'], 13413)
        self.assertEqual(result['parts'], 1)
        self.assertEqual(result['sample_status'], 'ready')
        self.assertEqual(result['score_sha256'], 'ecb0a351f73a9051e548ab160c1b6fe590893aa0807df9ed37f23dd4434de35d')
        self.assertEqual(hashlib.sha256(previous).hexdigest(), '6c5b39a5db5160cc17867b94bd351535a08063e191563c58465a7ae32d7024e0')
        self.assertEqual((self.root / 'previews/rolling-girl.mp3').read_bytes(), previous)
        verified = validate_package(result['zip'], require_cd_only=True, require_complete=True, audio_probe=True)
        self.assertEqual(verified['songs'], ['rolling-girl'])
        self.assertEqual(verified['artifacts']['rolling-girl']['audio']['verified'], 'decoded')

    def test_watcher_waits_for_stable_midi_and_sidecar_without_forcing_every_poll(self):
        self.args.watch = True
        with patch.object(cli.time, 'monotonic', return_value=0):
            self.assertFalse(cli._stable_for_watch(self.path, self.args))
        with patch.object(cli.time, 'monotonic', return_value=1):
            self.assertFalse(cli._stable_for_watch(self.path, self.args))
        with patch.object(cli.time, 'monotonic', return_value=2):
            self.assertTrue(cli._stable_for_watch(self.path, self.args))
        self.path.with_suffix('.json').write_text('{}')
        with patch.object(cli.time, 'monotonic', return_value=3):
            self.assertFalse(cli._stable_for_watch(self.path, self.args))
        with patch.object(cli.time, 'monotonic', return_value=5):
            self.assertTrue(cli._stable_for_watch(self.path, self.args))

    def test_idle_worker_inventory_probe_is_bounded_and_actual_work_forces_refresh(self):
        with patch('worker_config.configuration_fingerprint', side_effect=['a'*64, 'b'*64, 'c'*64]) as probe, \
             patch.object(cli.time, 'monotonic', side_effect=[0, 2, 4, 36]):
            self.assertEqual(cli._worker_fingerprint(self.args), 'a'*64)
            self.assertEqual(cli._worker_fingerprint(self.args), 'a'*64)
            self.assertEqual(cli._worker_fingerprint(self.args, force=True), 'b'*64)
            self.assertEqual(cli._worker_fingerprint(self.args), 'c'*64)
        self.assertEqual(probe.call_count, 3)


if __name__ == '__main__':
    unittest.main()
