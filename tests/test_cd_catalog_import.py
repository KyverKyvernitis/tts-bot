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
        with zipfile.ZipFile(result['zip']) as archive:
            self.assertIsNone(archive.testzip())
            scores = json.loads(archive.read('cogs/tts/cd_song_scores.json'))
            previews = json.loads(archive.read('cogs/tts/cd/previews/index.json'))
            self.assertEqual(len(scores), 1)
            self.assertTrue(all(set(note) == {'midi_note', 'start_ms', 'duration_ms', 'velocity'} for note in scores[0]['notes']))
            self.assertEqual(previews['test-song']['manifest_version'], 2)
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
        self.assertEqual(result['revision'], 1)
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
        with patch.object(importer, '_sample', side_effect=self.render), \
             patch.object(cli, '_resource_ready', return_value=False), \
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
        self.assertEqual(retried['processed'][0]['zip'], original_zip)
        self.assertEqual(json.loads((self.root / 'pending_publications.json').read_text()), {})


if __name__ == '__main__':
    unittest.main()
