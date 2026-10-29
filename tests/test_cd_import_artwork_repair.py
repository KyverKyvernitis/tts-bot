"""Existing attachment imports recover artwork without source MIDI or music writes."""
from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image

from osaka_cd import discord_import as service
from osaka_cd import import_artwork_repair as repair
from osaka_cd.store import RuntimeStore, _contract


ROOT = Path(__file__).resolve().parents[1]
_PREVIOUS_VAMPIRE_MIDI = base64.b64decode('TVRoZAAAAAYAAAABAGBNVHJrAAADNQD/AQ9PU0FLQV9DRF9NT05PPTEA/wMcVGhlIFZhbXBpcmUgLSBERUNPKjI3IC0gTGVhZAD/UQMFlR4A/1gEBAIYCADAAACQRH8wgEQAAJBCfzCAQgAAkEB/MIBAAACQQn8wgEIAMJBAf2CAQAAAkEx/YIBMAACQRH9IgEQAAJBCfxiAQgAwkEB/MIBAADCQTH8YgEwASJBEfzCARAAAkEJ/SIBCAACQQH9IgEAAAJBCf2CAQgAAkEB/MIBAAACQQn8wgEIAAJBAfzCAQAAAkER/SIBEAACQQn8YgEIAMJBJf4EQgEkAAJBIfzCASAAAkEl/MIBJAACQS38wgEsAAJBEfzCARAAAkEl/MIBJAACQS38wgEsAAJBMf2CATAAAkER/MIBEAACQQn8wgEIAMJBMfxiATABIkE5/YIBOAACQTn8wgE4AAJBMfzCATAAAkE5/MIBOAACQUH9ggFAAAJBOfzCATgAAkEx/MIBMAACQTH8YgEwASJBQf2CAUAAAkE5/MIBOAACQTH8wgEwAAJBOfzCATgAAkEx/gUCATAAAkER/MIBEAACQRH8wgEQAAJBEfzCARAAAkER/MIBEAACQRX8wgEUAAJBEf2CARAAAkEJ/MIBCAACQQH8wgEAAAJBAfxiAQAAAkEB/GIBAAACQQH8YgEAAAJBAfxiAQAAYkD1/GIA9AACQPX8YgD0AAJA9fxiAPQAAkEB/GIBAABiQPX8YgD0AGJBAfzCAQAAAkEJ/MIBCAACQRH8YgEQASJBEfzCARAAAkEV/MIBFAACQR38YgEcAGJBEfzCARAAAkEJ/MIBCAACQQn9ggEIAAJBAfzCAQAAAkEB/MIBAAACQQH8wgEAAAJBJfzCASQAAkEl/MIBJAACQSX8wgEkAAJBHfzCARwAAkEd/gUCARwAAkER/YIBEAACQQn8wgEIAAJBAfzCAQAAwkEx/MIBMADCQTn+BEIBOAACQTH8wgEwAAJBOfzCATgAAkFB/YIBQAACQTn8wgE4AAJBMfzCATAAAkEx/GIBMAEiQUH9ggFAAAJBOfzCATgAAkEx/MIBMAACQTn8wgE4AAJBMf2CATAAA/y8A')


def _midi():
    events = bytes([0, 144, 60, 100, 131, 96, 128, 60, 0,
                    0, 144, 64, 100, 131, 96, 128, 64, 0, 0, 255, 47, 0])
    return b'MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk' + len(events).to_bytes(4, 'big') + events


def _image():
    output = io.BytesIO()
    Image.new('RGB', (48, 48), 'blue').save(output, format='JPEG')
    return output.getvalue()


class ImportArtworkRepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        before = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), before))
        sys.path.insert(0, str(ROOT / 'deploy/ttv-cd'))
        self.importer = importlib.import_module('catalog_import')
        self.metadata = importlib.import_module('cd_metadata')
        self.title_lookup = importlib.import_module('osaka_cd.title_metadata')
        self.legacy = self.folder / 'legacy'; self.legacy.mkdir()
        self.store = RuntimeStore(self.folder / 'runtime', self.legacy)
        self.image = _image()

    async def install(self, *, filename='Example Song.mid', data=None):
        source = self.folder / filename
        source.write_bytes(data if data is not None else _midi())
        local = self.folder / 'local'; local.mkdir()
        (local / 'cd_song_scores.json').write_text('[]')
        (local / 'cd_song_videos.json').write_text('{"version":1,"songs":{}}')
        package = self.folder / 'initial.zip'
        with patch.object(self.importer, '_search', side_effect=self.metadata.MetadataSearchError('offline')):
            await self.importer.import_midi(source, root=local, render_sample=False, export_zip=package)
        snapshot = self.store.snapshot(strict=True, advertise_legacy=True)
        service._publish(self.store, package, snapshot, request_hash='a' * 64)
        source.unlink()
        return self.store.snapshot(strict=True, advertise_legacy=True)

    def found(self):
        return {'title': 'Example Song', 'artist': 'Verified Composer',
                'aliases': ['原曲'], 'artist_aliases': [],
                'seed_video_ids': ['abcdefghijk'], 'search_query': 'Example Song Verified Composer'}

    def search(self):
        return {'selected': {'video_id': 'abcdefghijk', 'title': 'Verified Composer - 原曲',
                            'channel': 'Verified Composer', 'duration': 180, 'view_count': 12345},
                'audit': {'inspected_candidates': 1}}

    async def run_child(self, directory):
        self.assertFalse(list(directory.rglob('*.mid')))
        with patch.object(self.title_lookup, 'resolve_title_metadata', return_value=self.found()), \
                patch.object(self.importer, '_search', return_value=self.search()), \
                patch.object(self.importer, '_cover_for', AsyncMock(return_value=(
                    self.image, hashlib.sha256(self.image).hexdigest()))):
            await repair._prepare(directory)

    async def test_real_import_repair_preserves_english_name_notes_arrangement_and_history(self):
        before = await self.install()
        old_root = Path(before['root'])
        original = json.loads((old_root / _contract().SCORES).read_text())[0]
        arrangement = old_root / original['arrangement_path']
        raw_arrangement = arrangement.read_bytes()
        self.assertEqual(repair._targets(self.store), ['example-song'])
        with patch.object(repair, '_run_repair_child', self.run_child):
            self.assertEqual(await repair._repair_one(self.store, 'example-song'), 'repaired')
        after = self.store.snapshot(strict=True, advertise_legacy=True)
        root = Path(after['root'])
        repaired = json.loads((root / _contract().SCORES).read_text())[0]
        self.assertEqual(repaired['name'], 'Example Song')
        self.assertEqual(repaired['artist'], 'Verified Composer')
        self.assertEqual(repaired['notes'], original['notes'])
        self.assertEqual(repaired['revision'], original['revision'])
        self.assertEqual(repaired['source_sha256'], original['source_sha256'])
        self.assertEqual(repaired['score_sha256'], original['score_sha256'])
        self.assertEqual(repaired['arrangement_sha256'], original['arrangement_sha256'])
        self.assertEqual((root / original['arrangement_path']).read_bytes(), raw_arrangement)
        self.assertEqual(arrangement.read_bytes(), raw_arrangement)
        self.assertEqual(json.loads((old_root / _contract().SCORES).read_text())[0], original)
        video = json.loads((root / _contract().VIDEOS).read_text())['songs']['example-song']
        self.assertEqual(video['view_count'], 12345)
        self.assertIn('原曲', video['title_aliases'])
        self.assertEqual((root / 'cogs/tts/cd/thumbnails/abcdefghijk.jpg').read_bytes(), self.image)
        self.assertEqual(repair._targets(self.store), [])

    async def test_exact_previously_delivered_vampire_repairs_when_title_provider_is_offline(self):
        before = await self.install(filename='The Vampire.mid', data=_PREVIOUS_VAMPIRE_MIDI)
        score = json.loads((Path(before['root']) / _contract().SCORES).read_text())[0]
        self.assertEqual(score['source_sha256'], repair._PREVIOUS_VAMPIRE_SOURCE)
        selected = {'selected': {'video_id': 'e1xCOsgWG0M',
                    'title': 'DECO*27 - ヴァンパイア feat. 初音ミク',
                    'channel': 'DECO*27', 'duration': 180, 'view_count': 12345},
                    'audit': {'inspected_candidates': 1}}
        async def child(directory):
            with patch.object(self.title_lookup, 'resolve_title_metadata',
                    side_effect=self.title_lookup.TitleMetadataError()) as lookup, \
                    patch.object(self.importer, '_search', return_value=selected) as search, \
                    patch.object(self.importer, '_cover_for', AsyncMock(return_value=(
                        self.image, hashlib.sha256(self.image).hexdigest()))):
                await repair._prepare(directory)
            lookup.assert_not_called()
            metadata = search.call_args.args[0]
            self.assertEqual(metadata['title'], 'The Vampire')
            self.assertEqual(metadata['artist'], 'DECO*27')
            self.assertIn('ヴァンパイア', metadata['aliases'])
            self.assertEqual(metadata['seed_video_ids'], ['e1xCOsgWG0M'])
        with patch.object(repair, '_run_repair_child', child):
            await repair._repair_one(self.store, 'the-vampire')
        after = self.store.snapshot()
        repaired = json.loads((Path(after['root']) / _contract().SCORES).read_text())[0]
        for field in ('notes', 'revision', 'source_sha256', 'score_sha256', 'arrangement_sha256'):
            self.assertEqual(repaired[field], score[field])
        self.assertEqual(repaired['artist'], 'DECO*27')
        self.assertEqual(repair._targets(self.store), [])

    async def test_previous_attachment_hint_cannot_override_different_music_or_known_artist(self):
        await self.install(filename='The Vampire.mid', data=_PREVIOUS_VAMPIRE_MIDI)
        snapshot = self.store.snapshot()
        score = json.loads((Path(snapshot['root']) / _contract().SCORES).read_text())[0]
        self.assertIsNotNone(repair._previous_attachment_metadata(score, {}))
        self.assertIsNone(repair._previous_attachment_metadata(dict(score, source_sha256='0' * 64), {}))
        self.assertIsNone(repair._previous_attachment_metadata(dict(score, id='other-song'), {}))
        self.assertIsNone(repair._previous_attachment_metadata(dict(score, name='Other Song'), {}))
        self.assertIsNone(repair._previous_attachment_metadata(dict(score, artist='Owner Choice'), {}))
        self.assertIsNone(repair._previous_attachment_metadata(score, {'artist': 'Owner Choice'}))
        marker = dict(score['discord_import'], source_sha256='0' * 64)
        self.assertIsNone(repair._previous_attachment_metadata(dict(score, discord_import=marker), {}))
        marker = dict(score['discord_import'], title_lookup={'metadata': {'artist': 'Owner Choice'}})
        self.assertIsNone(repair._previous_attachment_metadata(dict(score, discord_import=marker), {}))

    async def test_unknown_ambiguous_identity_never_selects_first_candidate_or_writes(self):
        before = await self.install()
        error = self.title_lookup.TitleMetadataError('metadata_ambiguous')
        with patch.object(self.title_lookup, 'resolve_title_metadata', side_effect=error), \
                patch.object(self.importer, 'repair_metadata', AsyncMock()) as importer:
            async def ambiguous(directory):
                await repair._prepare(directory)
            with patch.object(repair, '_run_repair_child', ambiguous), self.assertRaises(type(error)):
                await repair._repair_one(self.store, 'example-song')
        importer.assert_not_awaited()
        self.assertEqual(self.store.snapshot()['generation'], before['generation'])

    async def test_background_repair_is_bounded_and_silent_on_provider_failure(self):
        await self.install()
        with patch.object(repair, '_PASSES', (0, 0, 0)), \
                patch.object(repair, '_repair_one', AsyncMock(side_effect=OSError('offline'))) as attempt:
            await repair.refresh_imported_cd_artwork(store=self.store)
        self.assertEqual(attempt.await_count, 3)

    async def test_ready_or_non_discord_and_retired_cds_are_skipped(self):
        before = await self.install()
        score = json.loads((Path(before['root']) / _contract().SCORES).read_text())[0]
        entries = []
        for identity, marker in [('manual', None), ('rolling-girl', {}), ('ghost-rule', {}), ('override', {})]:
            entry = dict(score, id=identity)
            entry.pop('arrangement_path', None)
            for field in [key for key in entry if key.startswith('arrangement') or key.startswith('preview_')]:
                entry.pop(field)
            if marker is None:
                entry.pop('discord_import')
            else:
                entry['discord_import'] = marker
            entries.append(entry)
        package = self.folder / 'other-cds.zip'
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr(_contract().SCORES, json.dumps(entries))
        job = self.store.receive(package)
        self.assertTrue(self.store.process(job['job_id'])['applied'])
        self.assertEqual(repair._targets(self.store), ['example-song'])

    async def test_concurrent_owner_change_wins_over_stale_repair(self):
        before = await self.install()
        original = json.loads((Path(before['root']) / _contract().SCORES).read_text())[0]

        async def raced(directory):
            await self.run_child(directory)
            package = self.folder / 'owner-change.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr(_contract().SCORES, json.dumps([dict(original, artist='Owner Choice')]))
            job = self.store.receive(package, expected_versions={'example-song': before['versions']['example-song']})
            self.assertTrue(self.store.process(job['job_id'])['applied'])

        with patch.object(repair, '_run_repair_child', raced), self.assertRaises(service.ImportCDError) as caught:
            await repair._repair_one(self.store, 'example-song')
        self.assertEqual(caught.exception.code, 'conflict')
        latest = self.store.snapshot()
        score = json.loads((Path(latest['root']) / _contract().SCORES).read_text())[0]
        self.assertEqual(score['artist'], 'Owner Choice')
        self.assertFalse((Path(latest['root']) / 'cogs/tts/cd/thumbnails/abcdefghijk.jpg').exists())

    async def test_cancellation_waits_for_physical_publication_before_removing_temp_files(self):
        await self.install()
        entered, finish = threading.Event(), threading.Event()
        directories = []
        publish = repair._verify_and_publish

        def slow_publish(store, package, snapshot, identity):
            directories.append(package.parent)
            entered.set()
            self.assertTrue(finish.wait(5))
            self.assertTrue(package.is_file())
            return publish(store, package, snapshot, identity)

        with patch.object(repair, '_run_repair_child', self.run_child), \
                patch.object(repair, '_verify_and_publish', slow_publish):
            task = asyncio.create_task(repair._repair_one(self.store, 'example-song'))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(directories[0].is_dir())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(directories[0].exists())
        self.assertEqual(repair._targets(self.store), [])


if __name__ == '__main__':
    unittest.main()
