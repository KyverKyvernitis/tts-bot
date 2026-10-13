"""Real JPEG/manifest/diagnostic integration without a Discord SDK or network."""
from contextlib import redirect_stdout
from pathlib import Path
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from cogs.tts import cd_covers
from cogs.tts.interface.application_emojis import emoji_name, square_emoji

ROOT = Path(__file__).resolve().parents[1]


class OfflineCoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.covers = self.root / 'cd/thumbnails'
        self.covers.mkdir(parents=True)
        self.path = self.covers / 'vnw8zURAxkU.jpg'
        self.data = self.jpeg('gray')
        self.write_manifest(self.data)
        patcher = patch.object(cd_covers, '_DATA', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        environ = patch.dict('os.environ', {'TTV_CD_THUMBNAIL_CACHE_DIR': ''})
        environ.start()
        self.addCleanup(environ.stop)

    def jpeg(self, color):
        output = io.BytesIO()
        Image.new('RGB', (480, 360), color).save(output, format='JPEG')
        return output.getvalue()

    def write_manifest(self, data):
        (self.root / 'cd_song_videos.json').write_text(json.dumps({'songs': {'rolling-girl': {
            'video_id': 'vnw8zURAxkU', 'thumbnail_sha256': hashlib.sha256(data).hexdigest()}}}))

    def test_visible_remote_thumbnail_does_not_prove_local_emoji_ready(self):
        self.assertTrue(cd_covers.remote_thumbnail_url('rolling-girl'))
        self.assertTrue(cd_covers.cover_identity('rolling-girl'))
        self.assertEqual(cd_covers.cover_revision('rolling-girl'), '')
        self.assertIsNone(cd_covers._cover_bytes('rolling-girl'))

    def test_approved_cover_is_reused_without_redecode_and_converts_to_emoji(self):
        self.path.write_bytes(self.data)
        self.assertEqual(cd_covers._cover_bytes('rolling-girl'), self.data)
        with patch.object(cd_covers, '_valid_jpeg', side_effect=AssertionError('warm cover decoded again')):
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('warm cover read again')):
                self.assertEqual(cd_covers._cover_bytes('rolling-girl'), self.data)
                self.assertEqual(cd_covers.cover_revision('rolling-girl'), cd_covers.cover_identity('rolling-girl'))
        with Image.open(io.BytesIO(square_emoji(self.data))) as result:
            result.load()
            self.assertEqual(result.size, (128, 128))
            self.assertEqual(result.format, 'JPEG')

    def test_cover_change_changes_emoji_identity_and_refuses_old_bytes(self):
        self.path.write_bytes(self.data)
        old = emoji_name('osakacd', 'rolling-girl:' + cd_covers.cover_revision('rolling-girl'))
        new_data = self.jpeg('blue')
        self.write_manifest(new_data)
        self.assertIsNone(cd_covers._cover_bytes('rolling-girl'))
        self.path.write_bytes(new_data)
        revision = cd_covers.cover_revision('rolling-girl')
        self.assertEqual(revision, hashlib.sha256(new_data).hexdigest()[:12])
        self.assertNotEqual(old, emoji_name('osakacd', 'rolling-girl:' + revision))

    def test_local_diagnostic_decodes_real_cover_without_discord_request(self):
        self.path.write_bytes(self.data)
        spec = importlib.util.spec_from_file_location('diagnose_assets', ROOT / 'scripts/diagnose-cd-assets.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with patch('sys.argv', ['diagnose-cd-assets.py', 'rolling-girl']), redirect_stdout(output):
            module.main()
        report = json.loads(output.getvalue())
        self.assertTrue(report['local_cover_ready'])
        self.assertTrue(report['emoji_image']['valid'])
        self.assertEqual(report['emoji_image']['size'], [128, 128])
        self.assertFalse(report['discord_api_called'])
        self.assertEqual(report['local_cover_sha256'], hashlib.sha256(self.data).hexdigest())

    def test_diagnostic_without_pillow_distinguishes_valid_bytes_from_missing_file(self):
        self.path.write_bytes(self.data)
        spec = importlib.util.spec_from_file_location('diagnose_assets_no_pillow', ROOT / 'scripts/diagnose-cd-assets.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with patch('sys.argv', ['diagnose-cd-assets.py', 'rolling-girl']), redirect_stdout(output), \
             patch.object(module.importlib.util, 'find_spec', return_value=None), \
             patch.object(cd_covers, '_cover_bytes', return_value=None), \
             patch.object(cd_covers, 'cover_revision', return_value=''):
            module.main()
        report = json.loads(output.getvalue())
        self.assertIsNone(report['local_cover_ready'])
        self.assertFalse(report['pillow_available'])
        self.assertEqual(report['reason_code'], 'pillow_unavailable_in_this_python')
        self.assertTrue(report['local_cover_candidates'][0]['hash_matches'])
        self.assertEqual(report['local_cover_candidates'][0]['state'], 'hash_ready_decode_unavailable')

    def test_diagnostic_pinpoints_missing_hash_missing_file_and_changed_bytes(self):
        spec = importlib.util.spec_from_file_location('diagnose_assets_states', ROOT / 'scripts/diagnose-cd-assets.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        expected = hashlib.sha256(self.data).hexdigest()
        result = module.inspect_cover(self.path, expected, pillow_available=True)
        self.assertEqual(result['state'], 'missing_file')
        self.path.write_bytes(b'changed')
        result = module.inspect_cover(self.path, expected, pillow_available=True)
        self.assertEqual(result['state'], 'hash_mismatch')
        (self.root / 'cd_song_videos.json').write_text(json.dumps({'songs': {'rolling-girl': {'video_id':'vnw8zURAxkU'}}}))
        output = io.StringIO()
        with patch('sys.argv', ['diagnose-cd-assets.py', 'rolling-girl']), redirect_stdout(output):
            module.main()
        self.assertEqual(json.loads(output.getvalue())['reason_code'], 'thumbnail_hash_missing')


if __name__ == '__main__':
    unittest.main()
