"""The bundled artwork adapter runs the existing importer and owns its process."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import hashlib
import importlib
import io
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from PIL import Image

from osaka_cd import bundled_artwork as artwork


ROOT = Path(__file__).resolve().parents[1]


def cover():
    output = io.BytesIO()
    Image.new('RGB', (96, 72), 'teal').save(output, format='JPEG')
    return output.getvalue()


class ArtworkAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.saved_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), self.saved_path))
        sys.path.insert(0, str(ROOT / 'deploy/ttv-cd'))
        self.importer = importlib.import_module('catalog_import')
        self.image = cover()

    def inputs(self, directory=None):
        destination = directory or self.directory
        destination.mkdir(parents=True, exist_ok=True)
        artwork._copy_verified_inputs(destination)
        return destination

    def search(self):
        return {'selected': {'video_id': 'abcdefghijk',
                    'title': 'Ievan Polkka - Otomania feat. Hatsune Miku',
                    'channel': 'Otomania', 'duration': 150, 'view_count': 1234,
                    'source': 'verified-provider-fixture'},
                'audit': {'inspected_candidates': 1}}

    async def completed_package(self, directory=None):
        destination = self.inputs(directory)
        with patch.object(self.importer, '_search', return_value=self.search()) as search, \
                patch.object(self.importer, '_cover_for', AsyncMock(return_value=(
                    self.image, hashlib.sha256(self.image).hexdigest()))) as importer_cover, \
                patch.object(self.importer, '_sample', side_effect=AssertionError('no synthesis')):
            await artwork._prepare_importer(destination)
        search.assert_called_once()
        importer_cover.assert_awaited_once()
        return destination / 'artwork.zip'

    async def test_actual_importer_selects_metadata_and_prepares_cover_without_sample(self):
        result = await self.completed_package()
        report = artwork._validate_imported_package(result)
        self.assertEqual(report['songs'], ['ievan-polkka'])
        self.assertEqual(report['artifacts']['ievan-polkka']['thumbnail'], 'ready')
        self.assertEqual(report['artifacts']['ievan-polkka']['sample'], 'pending')
        with zipfile.ZipFile(result) as archive:
            video = json.loads(archive.read('cogs/tts/cd_song_videos.json'))['songs']['ievan-polkka']
            self.assertEqual(video['view_count'], 1234)
            self.assertEqual(video['selection_audit'], {'inspected_candidates': 1})
            self.assertEqual(archive.read('cogs/tts/cd/thumbnails/abcdefghijk.jpg'), self.image)
            self.assertFalse(any(name.endswith('.mp3') for name in archive.namelist()))

    async def test_real_cover_importer_downloads_and_converts_provider_image(self):
        destination = self.inputs()
        output = io.BytesIO()
        Image.new('RGB', (64, 48), 'turquoise').save(output, format='PNG')
        provider_image = output.getvalue()
        requested = []

        class Response:
            status = 200
            headers = {'Content-Type': 'image/png'}

            def __init__(self):
                self.content = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def iter_chunked(self, size):
                for offset in range(0, len(provider_image), 41):
                    yield provider_image[offset:offset + 41]

        class Session:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, **kwargs):
                requested.append(url)
                return Response()

        with patch.object(self.importer, '_search', return_value=self.search()), \
                patch('aiohttp.ClientSession', Session), \
                patch.object(self.importer, '_sample', side_effect=AssertionError('no synthesis')):
            # _cover_for, cache-thumbnails and ArchiveMixin all run unchanged;
            # only the external metadata/image responses are controlled.
            await artwork._prepare_importer(destination)
        self.assertTrue(requested)
        self.assertTrue(all('abcdefghijk' in url for url in requested))
        with zipfile.ZipFile(destination / 'artwork.zip') as archive:
            image = archive.read('cogs/tts/cd/thumbnails/abcdefghijk.jpg')
            metadata = json.loads(archive.read('cogs/tts/cd_song_videos.json'))['songs']['ievan-polkka']
        self.assertTrue(image.startswith(b'\xff\xd8\xff'))
        self.assertNotEqual(image, provider_image)
        self.assertEqual(metadata['thumbnail_sha256'], hashlib.sha256(image).hexdigest())
        with Image.open(io.BytesIO(image)) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertLessEqual(max(decoded.size), 768)

    async def test_pending_metadata_is_retryable_and_never_yields_artwork(self):
        destination = self.inputs()
        from cd_metadata import MetadataSearchError
        with patch.object(self.importer, '_search', side_effect=MetadataSearchError('unavailable')):
            with self.assertRaisesRegex(artwork.ArtworkImportError, 'import_pending'):
                await artwork._prepare_importer(destination)

    async def test_pending_cover_is_retryable_without_substituting_an_image(self):
        destination = self.inputs()
        with patch.object(self.importer, '_search', return_value=self.search()), \
                patch.object(self.importer, '_cover_for', AsyncMock(side_effect=OSError('offline'))):
            with self.assertRaisesRegex(artwork.ArtworkImportError, 'import_pending'):
                await artwork._prepare_importer(destination)

    async def test_both_bundled_inputs_are_checksum_verified_before_child_launch(self):
        for filename in artwork._INPUT_HASHES:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                sources = Path(folder)
                for name in artwork._INPUT_HASHES:
                    shutil.copy2(artwork._SOURCES / name, sources / name)
                (sources / filename).write_bytes(b'changed input')
                with patch.object(artwork, '_SOURCES', sources), \
                        patch.object(asyncio, 'create_subprocess_exec', AsyncMock()) as launch:
                    with self.assertRaisesRegex(artwork.ArtworkImportError, 'source_checksum'):
                        async with artwork.imported_artwork_package():
                            self.fail('unverified input entered the importer')
                launch.assert_not_called()

    async def test_context_owns_validated_package_until_consumer_finishes(self):
        fixture = await self.completed_package(self.directory / 'fixture')
        original = asyncio.create_subprocess_exec
        launched, commands = [], []

        async def launch(*command, **kwargs):
            commands.append(command)
            code = 'from pathlib import Path;import shutil,sys;shutil.copyfile(sys.argv[1],Path(sys.argv[2])/"artwork.zip")'
            process = await original(sys.executable, '-c', code, str(fixture), command[-1], **kwargs)
            launched.append(process)
            return process

        with patch.object(asyncio, 'create_subprocess_exec', side_effect=launch):
            async with artwork.imported_artwork_package() as package:
                self.assertTrue(package.is_file())
                self.assertEqual(commands[0][1:3], (str(Path(artwork.__file__).resolve()), '--prepare'))
                self.assertEqual(launched[0].returncode, 0)
                owned_directory = package.parent
                self.assertTrue((owned_directory / 'ievan-polkka.mid').is_file())
        self.assertFalse(owned_directory.exists())

    async def test_cancellation_terminates_and_reaps_the_real_importer_process(self):
        original = asyncio.create_subprocess_exec
        started = asyncio.Event()
        launched, directories = [], []

        async def launch(*command, **kwargs):
            process = await original(sys.executable, '-c', 'import time;time.sleep(60)', **kwargs)
            launched.append(process)
            directories.append(Path(command[-1]))
            started.set()
            return process

        async def use():
            async with artwork.imported_artwork_package():
                self.fail('unfinished import was published')

        with patch.object(asyncio, 'create_subprocess_exec', side_effect=launch):
            task = asyncio.create_task(use())
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertIn(launched[0].returncode, (-signal.SIGTERM, -signal.SIGKILL))
        self.assertFalse(directories[0].exists())

    async def test_timeout_reaps_child_before_removing_workspace(self):
        original = asyncio.create_subprocess_exec
        launched = []

        async def launch(*command, **kwargs):
            process = await original(sys.executable, '-c', 'import time;time.sleep(60)', **kwargs)
            launched.append((process, Path(command[-1])))
            return process

        with patch.object(asyncio, 'create_subprocess_exec', side_effect=launch):
            with self.assertRaisesRegex(artwork.ArtworkImportError, 'import_timeout'):
                async with artwork.imported_artwork_package(timeout_seconds=.03):
                    self.fail('timed out import yielded a package')
        self.assertIn(launched[0][0].returncode, (-signal.SIGTERM, -signal.SIGKILL))
        self.assertFalse(launched[0][1].exists())

    async def test_child_error_does_not_expose_provider_diagnostics(self):
        original = asyncio.create_subprocess_exec

        async def launch(*command, **kwargs):
            return await original(sys.executable, '-c',
                'import sys;print("SECRET_PROVIDER_KEY",file=sys.stderr);sys.exit(2)', **kwargs)

        with patch.object(asyncio, 'create_subprocess_exec', side_effect=launch):
            with self.assertRaisesRegex(artwork.ArtworkImportError, '^bundled_artwork_import_unavailable$'):
                async with artwork.imported_artwork_package():
                    self.fail('failed import yielded a package')


if __name__ == '__main__':
    unittest.main()
