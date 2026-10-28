"""Real MIDI subprocess/SQLite publication tests and bounded input cases."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import sys
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image

from osaka_cd import discord_import as service
from osaka_cd.store import RuntimeStore, _contract


async def offline_importer(directory):
    """Run the actual import pipeline with only its metadata provider offline."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'))
    import catalog_import
    from cd_metadata import MetadataSearchError
    with patch.object(catalog_import, '_search', side_effect=MetadataSearchError('offline')):
        await service._prepare_importer(directory)
    return json.loads((directory / 'report.json').read_text())


def midi():
    events = bytes([0, 144, 60, 100, 131, 96, 128, 60, 0,
                    0, 144, 64, 100, 131, 96, 128, 64, 0, 0, 255, 47, 0])
    return b'MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk' + len(events).to_bytes(4, 'big') + events


def package(path, identity='demo', pitch=60, *, revision=1, marker=None, cover=True):
    contract = _contract()
    score = {'id': identity, 'name': identity, 'artist': 'Demo', 'excerpt': 'test',
             'source_url': '', 'source_sha256': hashlib.sha256(midi()).hexdigest(),
             'revision': revision, 'duration_ms': 500,
             'notes': [{'midi_note': pitch, 'start_ms': 0, 'duration_ms': 500, 'velocity': 100}]}
    if marker:
        score['discord_import'] = service._marker(score, marker)
    score['score_sha256'] = contract.score_sha256(score)
    contents = {contract.SCORES: json.dumps([score]).encode(),
                contract.VIDEOS: b'{"version":1,"songs":{}}'}
    if cover:
        output = io.BytesIO()
        Image.new('RGB', (16, 16), 'red').save(output, 'JPEG')
        image = output.getvalue()
        contents[contract.VIDEOS] = json.dumps({'version': 1, 'songs': {identity: {
            'video_id': 'abcdefghijk', 'thumbnail_sha256': hashlib.sha256(image).hexdigest(),
            'selection_audit': {'inspected_candidates': 2}}}}).encode()
        contents['cogs/tts/cd/thumbnails/abcdefghijk.jpg'] = image
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for rel, data in contents.items():
            archive.writestr(rel, data)
    return path


class InputTests(unittest.TestCase):
    def test_unicode_filename_stable_safe_id_and_title(self):
        prepared = service.prepare_input('少女A（单人声）.midi', midi())
        metadata = service.inspect_input(prepared)
        self.assertEqual(metadata['title'], '少女A（单人声）')
        self.assertRegex(metadata['id'], '[a-z0-9-]+')
        self.assertEqual(service.inspect_input(prepared)['id'], metadata['id'])
        all_unicode = service.inspect_input(service.prepare_input('少女.midi', midi()))
        self.assertEqual(all_unicode['id'], 'cd-' + hashlib.sha256(midi()).hexdigest()[:16])

    def test_filename_inference_and_optional_json_modal_overrides(self):
        prepared = service.prepare_input('Artist - Name.mid', midi(), b'{"title":"Override","track_index":1}')
        metadata = service.inspect_input(prepared, {'artist': 'Other', 'start_ms': 95600, 'max_duration_ms': 29500})
        self.assertEqual((metadata['title'], metadata['artist'], metadata['track_index']), ('Override', 'Other', 1))
        self.assertEqual((metadata['start_ms'], metadata['max_duration_ms']), (95600, 29500))

    def test_bad_filename_signature_json_and_limits_are_public_errors(self):
        for filename, data, sidecar in [
            ('../song.mid', midi(), None), ('song\\file.mid', midi(), None),
            ('song\n.mid', midi(), None), ('song.mp3', b'ID3', None),
            ('song.mid', b'not midi', None), ('song.zip', b'not zip', None),
            ('song.mid', midi(), b'[]'), ('song.mid', midi(), b'{"title":"a","title":"b"}'),
            ('song.mid', midi(), b'{"start_ms":NaN}'),
            ('song.mid', midi(), b'{' + b' ' * service.MAX_SIDECAR_BYTES),
        ]:
            with self.subTest(filename=filename, sidecar=bool(sidecar)), self.assertRaises(service.ImportCDError) as caught:
                service.prepare_input(filename, data, sidecar)
            self.assertLess(len(caught.exception.public_message), 220)
        with patch.object(service, 'MAX_MIDI_BYTES', 2), self.assertRaises(service.ImportCDError) as caught:
            service.prepare_input('song.mid', midi())
        self.assertEqual(caught.exception.code, 'file_too_large')

    def test_sidecar_cannot_supply_paths_and_selection_is_strict(self):
        prepared = service.prepare_input('Song.mid', midi(), b'{"root":"/tmp/evil","cover_file":"x","title":"Song"}')
        self.assertNotIn('root', service.inspect_input(prepared))
        self.assertNotIn('cover_file', service.inspect_input(prepared))
        for overrides in [{'max_duration_ms': True}, {'start_ms': -1}, {'track_index': 64},
                          {'max_duration_ms': 60001}, {'title': 'a' * 101},
                          {'include_bass': 'false'}, {'id': '../no'}, {'root': '/tmp'},
                          {'seed_video_ids': ['not-video']}, {'expected_song_duration_seconds': float('inf')}]:
            with self.subTest(overrides=overrides), self.assertRaises(service.ImportCDError):
                service.inspect_input(prepared, overrides)


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.activation_patch = patch.object(service, '_ACTIVATION_TIMEOUT', 0)
        self.activation_patch.start()
        self.temp = tempfile.TemporaryDirectory(prefix='discord-cd-service-')
        self.root = Path(self.temp.name)
        self.legacy = self.root / 'legacy'
        self.legacy.mkdir()
        self.store = RuntimeStore(self.root / 'data', self.legacy)

    def tearDown(self):
        self.activation_patch.stop()
        self.temp.cleanup()

    def seed(self, identity='other', **kwargs):
        path = package(self.root / (identity + '.zip'), identity, **kwargs)
        job = self.store.receive(path)
        result = self.store.process(job['job_id'])
        self.assertTrue(result['applied'], result)
        return path

    async def test_real_midi_subprocess_publishes_music_and_reports_pending_without_network(self):
        self.seed()
        prepared = service.prepare_input('New Song.midi', midi())
        before = self.store.snapshot()['versions']['other']
        original = self.store.snapshot(strict=True, advertise_legacy=True)
        directory = self.root / 'child'
        directory.mkdir()
        (directory / 'source.mid').write_bytes(prepared.data)
        service._dump(directory / 'source.json', service.inspect_input(prepared))
        service._seed_catalog(original, directory / 'catalog', 'new-song')
        child = await service._run_importer(directory)
        self.assertTrue(child['ok'])
        result = service._publish(self.store, directory / 'package.zip', original,
            service._request_hash(prepared, service.inspect_input(prepared)))
        self.assertTrue(result.applied)
        self.assertEqual(result.state, 'applied')
        self.assertEqual(result.songs[0]['id'], 'new-song')
        self.assertEqual(set(result.pending), {'metadata', 'thumbnail'})
        self.assertEqual(self.store.snapshot()['versions']['other'], before)
        _contract().validate_cd_tree(self.store.snapshot()['root'], ['new-song'])
        score = next(row for row in json.loads((Path(self.store.snapshot()['root']) / _contract().SCORES).read_text()) if row['id'] == 'new-song')
        self.assertIn('discord_import', score)

    async def test_cd_zip_stays_outside_code_updater_and_repeats_are_idempotent(self):
        self.seed()
        path = package(self.root / 'incoming.zip', 'new-song')
        prepared = service.prepare_input(path.name, path.read_bytes())
        first = await service.import_cd(prepared, store=self.store)
        self.assertTrue(first.applied)
        current = self.store.snapshot()['generation']
        again = await service.import_cd(prepared, store=self.store)
        self.assertTrue(again.already_installed)
        self.assertEqual(self.store.snapshot()['generation'], current)
        self.assertEqual(set(self.store.snapshot()['versions']), {'other', 'new-song'})

    async def test_zip_with_code_or_traversal_is_rejected_without_catalog_changes(self):
        self.seed()
        before = self.store.snapshot()['generation']
        for name in ['bot.py', '../escape.py', 'cogs/tts/cd/../../bad.py']:
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr(name, b'raise Exception()')
            with self.subTest(name=name), self.assertRaises(service.ImportCDError) as caught:
                await service.import_cd(service.prepare_input('cd.zip', data.getvalue()), store=self.store)
            self.assertEqual(caught.exception.code, 'invalid_package')
            self.assertEqual(self.store.snapshot()['generation'], before)

    async def test_ready_midi_repeat_skips_provider_and_keeps_generation(self):
        prepared = service.prepare_input('Demo - Demo.mid', midi())
        metadata = service.inspect_input(prepared)
        marker = service._request_hash(prepared, metadata)
        self.seed('demo', marker=marker)
        before = self.store.snapshot()['generation']
        with patch.object(service, '_run_importer', side_effect=AssertionError('must not repeat provider')):
            result = await service.import_cd(prepared, store=self.store)
        self.assertTrue(result.already_installed)
        self.assertEqual(self.store.snapshot()['generation'], before)

    async def test_pending_cover_allows_a_new_metadata_attempt(self):
        prepared = service.prepare_input('Demo - Demo.mid', midi())
        metadata = service.inspect_input(prepared)
        marker = service._request_hash(prepared, metadata)
        self.seed('demo', marker=marker, cover=False)
        called = []
        async def imported(directory):
            called.append(True)
            package(directory / 'package.zip', 'demo')
        with patch.object(service, '_run_importer', side_effect=imported):
            result = await service.import_cd(prepared, store=self.store)
        self.assertTrue(called)
        self.assertFalse(result.already_installed)

    async def test_user_publication_during_preparation_wins(self):
        self.seed()
        async def imported(directory):
            package(directory / 'package.zip', 'demo')
            self.seed('demo', pitch=65)
        with patch.object(service, '_run_importer', side_effect=imported):
            with self.assertRaises(service.ImportCDError) as caught:
                await service.import_cd(service.prepare_input('Demo - Demo.mid', midi()), store=self.store)
        self.assertEqual(caught.exception.code, 'conflict')
        scores = json.loads((Path(self.store.snapshot()['root']) / _contract().SCORES).read_text())
        self.assertEqual(next(row for row in scores if row['id'] == 'demo')['notes'][0]['midi_note'], 65)

    async def test_progress_message_failure_does_not_cancel_publication(self):
        path = package(self.root / 'new.zip', 'new-song')
        result = await service.import_cd(service.prepare_input('new.zip', path.read_bytes()), store=self.store,
                                         progress=AsyncMock(side_effect=RuntimeError('deleted message')))
        self.assertTrue(result.applied)

    async def test_transaction_cancellation_drains_before_temp_package_is_removed(self):
        path = package(self.root / 'new.zip', 'new-song')
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        original = service._publish
        observed = []
        def physical(store, package, snapshot, request_hash=None):
            started.set()
            release.wait(3)
            observed.append(package.exists())
            result = original(store, package, snapshot, request_hash)
            done.set()
            return result
        with patch.object(service, '_publish', side_effect=physical):
            task = asyncio.create_task(service.import_cd(service.prepare_input('new.zip', path.read_bytes()), store=self.store))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(done.is_set())
        self.assertEqual(observed, [True])

    async def test_child_cancel_kills_and_reaps_its_private_process_group(self):
        process = type('Process', (), {})()
        process.pid, process.returncode = 99999, None
        process.wait = AsyncMock(side_effect=[asyncio.CancelledError(), None])
        def stopped(*_args):
            process.returncode = -15
        with patch.object(asyncio, 'create_subprocess_exec', return_value=process), \
             patch.object(service.os, 'killpg', side_effect=stopped) as kill:
            with self.assertRaises(asyncio.CancelledError):
                await service._run_importer(self.root)
        kill.assert_called_once_with(99999, service.signal.SIGTERM)
        self.assertEqual(process.wait.await_count, 2)

    async def test_seed_preserves_valid_local_assets_in_private_layout(self):
        self.seed('demo')
        destination = self.root / 'seed'
        service._seed_catalog(self.store.snapshot(), destination, 'demo')
        video = json.loads((destination / 'cd_song_videos.json').read_text())['songs']['demo']
        old = Path(self.store.snapshot()['root']) / 'cogs/tts/cd/thumbnails/abcdefghijk.jpg'
        new = destination / 'thumbnails/abcdefghijk.jpg'
        self.assertEqual(new.read_bytes(), old.read_bytes())
        self.assertNotEqual(new.stat().st_ino, old.stat().st_ino)
        self.assertEqual(video['thumbnail_sha256'], hashlib.sha256(new.read_bytes()).hexdigest())

    async def test_activation_requires_genuine_store_acknowledgement(self):
        path = package(self.root / 'new.zip', 'new-song')
        async def progress(phase):
            if phase == 'activating':
                self.store.acknowledge(self.store.snapshot()['generation'])
        with patch.object(service, '_ACTIVATION_TIMEOUT', 1):
            result = await service.import_cd(service.prepare_input('new.zip', path.read_bytes()),
                store=self.store, progress=progress)
        self.assertEqual(result.state, 'activated')
        self.assertTrue(self.store.job(result.job_id)['installed'])

    async def test_importer_metadata_outage_is_reported_even_when_old_cover_is_valid(self):
        self.seed('demo')
        async def imported(directory):
            package(directory / 'package.zip', 'demo')
            return {'pending': ['metadata']}
        with patch.object(service, '_run_importer', side_effect=imported):
            result = await service.import_cd(service.prepare_input('Demo - Demo.mid', midi()), store=self.store)
        self.assertEqual(result.pending, ('metadata',))

    async def test_automatic_same_title_other_artist_does_not_replace_another_cd(self):
        self.seed('demo')
        # Existing fixture score belongs to another artist/source than the new request.
        prepared = service.prepare_input('Other Artist - demo.mid', midi() + b'\x00')
        observed = []
        async def imported(directory):
            metadata = json.loads((directory / 'source.json').read_text())
            observed.append(metadata['id'])
            package(directory / 'package.zip', metadata['id'])
        before = self.store.snapshot()['versions']['demo']
        with patch.object(service, '_run_importer', side_effect=imported):
            result = await service.import_cd(prepared, store=self.store)
        self.assertNotEqual(observed, ['demo'])
        self.assertEqual(self.store.snapshot()['versions']['demo'], before)
        self.assertTrue(result.applied)

    async def test_marker_cannot_skip_music_edited_after_the_original_import(self):
        prepared = service.prepare_input('Demo - Demo.mid', midi())
        metadata = service.inspect_input(prepared)
        marker = service._request_hash(prepared, metadata)
        path = package(self.root / 'edited.zip', 'demo', marker=marker)
        with zipfile.ZipFile(path) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        scores = json.loads(contents[_contract().SCORES])
        scores[0]['notes'][0]['midi_note'] = 65
        scores[0]['score_sha256'] = _contract().score_sha256(scores[0])
        contents[_contract().SCORES] = json.dumps(scores).encode()
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
        self.store.process(self.store.receive(path)['job_id'])
        self.assertIsNone(service._existing_midi(self.store.snapshot(), metadata, marker))

    @patch.object(service, '_run_importer', new=offline_importer)
    async def test_real_midi_reimport_preserves_matching_generated_sample_and_old_generation(self):
        prepared = service.prepare_input('Demo - Demo.mid', midi())
        initial = await service.import_cd(prepared, store=self.store)
        initial_root = Path(self.store.snapshot()['root'])
        score = json.loads((initial_root / _contract().SCORES).read_text())[0]
        audio_path = self.root / 'real.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=' + str(score['duration_ms'] / 1000),
            '-codec:a', 'libmp3lame', '-y', str(audio_path)], check=True)
        audio = audio_path.read_bytes()
        audio_hash = hashlib.sha256(audio).hexdigest()
        preview = {'manifest_version': 3, 'id': 'demo', 'revision': score['revision'],
            'source_sha256': score['source_sha256'], 'score_sha256': score['score_sha256'],
            'voice_id': 'kasane-teto', 'language': 'pt-BR', 'pitch_semitones': 0,
            'speech_rate': 1.0, 'mp3_sha256': audio_hash,
            'arrangement_sha256': score['preview_arrangement_sha256'],
            'source_arrangement_sha256': score['arrangement_sha256'],
            'arrangement_performance_sha256': score['preview_performance_sha256'],
            'rendering_profile': {'mode': 'multipart-utau', 'configuration_fingerprint': 'a' * 64,
                                  'render_fingerprint': audio_hash}}
        overlay = self.root / 'sample.zip'
        with zipfile.ZipFile(overlay, 'w') as archive:
            archive.writestr(_contract().PREVIEWS, json.dumps({'demo': preview}))
            archive.writestr('cogs/tts/cd/previews/demo.mp3', audio)
        published = self.store.process(self.store.receive(overlay)['job_id'])
        self.assertTrue(published['applied'], published)
        old_root = Path(self.store.snapshot()['root'])
        old_index = (old_root / _contract().PREVIEWS).read_bytes()
        again = await service.import_cd(prepared, store=self.store)
        self.assertTrue(again.applied)
        latest_root = Path(self.store.snapshot()['root'])
        self.assertEqual((latest_root / 'cogs/tts/cd/previews/demo.mp3').read_bytes(), audio)
        self.assertEqual((old_root / 'cogs/tts/cd/previews/demo.mp3').read_bytes(), audio)
        self.assertEqual((old_root / _contract().PREVIEWS).read_bytes(), old_index)
        self.assertEqual(json.loads((latest_root / _contract().PREVIEWS).read_text())['demo']['mp3_sha256'], audio_hash)

    async def test_acknowledgement_read_outage_does_not_report_rollback_after_commit(self):
        result = service.ImportResult((), applied=True, generation='g-one', state='applied', job_id='a' * 64)
        import sqlite3
        with patch.object(service, '_ACTIVATION_TIMEOUT', 1), \
             patch.object(self.store, 'job', side_effect=sqlite3.OperationalError('busy')):
            self.assertEqual(await service._wait_for_activation(self.store, result), result)

    async def test_child_timeout_stops_the_private_process_group(self):
        process = type('Process', (), {})()
        process.pid, process.returncode = 99998, None
        process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
        def stopped(*_args):
            process.returncode = -15
        with patch.object(asyncio, 'create_subprocess_exec', return_value=process), \
             patch.object(service.os, 'killpg', side_effect=stopped) as kill:
            with self.assertRaises(service.ImportCDError) as caught:
                await service._run_importer(self.root)
        self.assertEqual(caught.exception.code, 'import_timeout')
        kill.assert_called_once_with(99998, service.signal.SIGTERM)
