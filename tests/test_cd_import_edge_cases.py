"""Independent publication regressions for the attachment import command."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from osaka_cd import discord_import as service
from osaka_cd.store import RuntimeStore, _contract


def _package(path, identity='song', *, image=False, audio=None):
    score = {
        'id': identity, 'name': identity, 'artist': 'Artist', 'excerpt': '',
        'source_url': '', 'source_sha256': 'a' * 64, 'revision': 1,
        'duration_ms': 500,
        'notes': [{'midi_note': 60, 'start_ms': 0, 'duration_ms': 500, 'velocity': 90}],
    }
    score['score_sha256'] = _contract().score_sha256(score)
    contents = {
        _contract().SCORES: json.dumps([score]).encode(),
        _contract().VIDEOS: b'{"version":1,"songs":{}}',
    }
    if image:
        output = io.BytesIO()
        Image.new('RGB', (32, 32), '#4499aa').save(output, 'JPEG')
        cover = output.getvalue()
        contents['cogs/tts/cd/thumbnails/abcdefghijk.jpg'] = cover
        contents[_contract().VIDEOS] = json.dumps({'version': 1, 'songs': {identity: {
            'video_id': 'abcdefghijk',
            'thumbnail_sha256': hashlib.sha256(cover).hexdigest(),
            'selection_audit': {'provider': 'test-only'},
        }}}).encode()
    if audio is not None:
        contents[f'cogs/tts/cd/previews/{identity}.mp3'] = audio
        contents[_contract().PREVIEWS] = json.dumps({identity: {
            'manifest_version': 2, 'id': identity, 'revision': 1,
            'source_sha256': score['source_sha256'],
            'score_sha256': score['score_sha256'],
            'mp3_sha256': hashlib.sha256(audio).hexdigest(),
            'voice_id': 'kasane-teto', 'language': 'pt-BR',
            'pitch_semitones': 0, 'speech_rate': 1.0,
        }}).encode()
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    return path


class ImportPublicationEdgeCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cd-import-edge-')
        self.root = Path(self.temporary.name)
        self.legacy = self.root / 'legacy'
        self.legacy.mkdir()
        self.store = RuntimeStore(self.root / 'runtime', self.legacy)
        # These isolated store tests have no running Discord catalog reader.
        self.activation_wait = patch.object(service, '_ACTIVATION_TIMEOUT', 0)
        self.activation_wait.start()

    def tearDown(self):
        self.activation_wait.stop()
        self.temporary.cleanup()

    def publish(self, path):
        receipt = self.store.receive(path)
        result = self.store.process(receipt['job_id'])
        self.assertTrue(result['applied'], result)
        return result

    async def import_package(self, path):
        return await service.import_cd(
            service.prepare_input(path.name, path.read_bytes()), store=self.store)

    async def test_repeating_incomplete_package_keeps_pending_artifacts(self):
        package = _package(self.root / 'pending.zip')
        first = await self.import_package(package)
        second = await self.import_package(package)
        self.assertTrue(second.already_installed)
        self.assertEqual(set(first.pending), {'metadata', 'thumbnail'})
        self.assertEqual(set(second.pending), set(first.pending))
        self.assertEqual(second.generation, first.generation)

    async def test_repeat_after_unrelated_publication_does_not_rewrite_catalog(self):
        package = _package(self.root / 'ready.zip', image=True)
        first = await self.import_package(package)
        self.publish(_package(self.root / 'unrelated.zip', 'other-song'))
        before = self.store.snapshot()
        self.assertNotEqual(before['generation'], first.generation)
        repeated = await self.import_package(package)
        after = self.store.snapshot()
        self.assertTrue(repeated.already_installed)
        self.assertEqual(after['generation'], before['generation'])
        self.assertEqual(after['versions'], before['versions'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_cover_import_preserves_ready_sample_bytes_and_original_generation(self):
        mp3 = self.root / 'sample.mp3'
        subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5',
            '-codec:a', 'libmp3lame', '-b:a', '96k', '-y', str(mp3),
        ], check=True, capture_output=True)
        audio = mp3.read_bytes()
        self.publish(_package(self.root / 'with-sample.zip', audio=audio))
        old_snapshot = self.store.snapshot()
        old_root = Path(old_snapshot['root'])
        old_audio = old_root / 'cogs/tts/cd/previews/song.mp3'
        old_preview = (old_root / _contract().PREVIEWS).read_bytes()
        # The new attachment supplies a cover but no MP3 or preview manifest.
        incoming = _package(self.root / 'cover.zip', image=True)
        result = await self.import_package(incoming)
        self.assertTrue(result.applied)
        current_root = Path(self.store.snapshot()['root'])
        self.assertEqual((current_root / 'cogs/tts/cd/previews/song.mp3').read_bytes(), audio)
        self.assertEqual((current_root / _contract().PREVIEWS).read_bytes(), old_preview)
        self.assertEqual(old_audio.read_bytes(), audio)
        self.assertNotEqual(str(current_root), str(old_root))
        report = _contract().validate_cd_tree(current_root, ['song'], audio_probe=True)
        self.assertEqual(report['artifacts']['song']['sample'], 'ready')
        self.assertEqual(report['artifacts']['song']['thumbnail'], 'ready')
