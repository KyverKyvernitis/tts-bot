"""Recovery regression using the supplied MIDI and MP3, without synthesis.

Set OSAKA_RECOVERY_STATE and OSAKA_RECOVERY_MIDI when fixtures are elsewhere.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
KIT = PROJECT / 'deploy/ttv-cd'
sys.path.insert(0, str(KIT))
SPEC = importlib.util.spec_from_file_location('rolling_recovery_tests', KIT / 'recover-rolling-girl.py')
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RollingRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workspace = PROJECT.parents[1]
        cls.state = Path(os.environ.get('OSAKA_RECOVERY_STATE', str(workspace / 'rolling-girl-state')))
        cls.midi = Path(os.environ.get('OSAKA_RECOVERY_MIDI', str(workspace / 'recovery/midi/wowaka - Rolling Girl.mid')))
        if not cls.state.is_dir() and "OSAKA_RECOVERY_STATE" not in os.environ:
            cls.state = PROJECT / "recovery/state"
        if not cls.midi.is_file() and "OSAKA_RECOVERY_MIDI" not in os.environ:
            cls.midi = PROJECT / "examples/rolling-girl/wowaka - Rolling Girl.mid"
        if not cls.state.is_dir() or not cls.midi.is_file():
            raise unittest.SkipTest('Os artefatos reais fornecidos são necessários para este teste de recuperação.')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='rolling-recovery-regression-')
        self.folder = Path(self.temporary.name)
        self.data = self.folder / 'data'
        self.data.mkdir()
        self.output = self.folder / 'patch.zip'
        self.original = json.loads((self.state / 'cd_song_scores.json').read_text())[0]
        unrelated = {**self.original, 'id': 'preserved-song', 'name': 'Preserved Song'}
        (self.data / 'cd_song_scores.json').write_text(json.dumps([unrelated]))
        (self.data / 'cd_song_videos.json').write_text('{"version":1,"songs":{}}')

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_supplied_audio_migrates_and_pending_cover_keeps_other_ids(self):
        result = recovery.recover(self.state, self.midi, self.data, self.output)
        self.assertTrue(result['ok'])
        self.assertFalse(result['complete'])
        self.assertFalse(result['resynthesized'])
        self.assertEqual(result['mp3_sha256'], recovery.MP3_SHA256)
        self.assertEqual(result['score_sha256'], 'ecb0a351f73a9051e548ab160c1b6fe590893aa0807df9ed37f23dd4434de35d')
        self.assertEqual(result['validation']['artifacts']['rolling-girl']['audio']['verified'], 'decoded')
        self.assertEqual({row['id'] for row in json.loads((self.data / 'cd_song_scores.json').read_text())},
                         {'preserved-song', 'rolling-girl'})
        manifest = json.loads((self.data / 'previews/index.json').read_text())['rolling-girl']
        self.assertFalse(manifest['migration']['resynthesized'])
        self.assertEqual(manifest['migration']['legacy_score_sha256'], recovery.MIDI_SHA256)
        self.assertTrue(json.loads((self.data / 'pending_artifacts.json').read_text())[self.midi.name]['artifacts'])
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual([row['id'] for row in json.loads(archive.read('cogs/tts/cd_song_scores.json'))], ['rolling-girl'])
            self.assertEqual(hashlib.sha256(archive.read('cogs/tts/cd/previews/rolling-girl.mp3')).hexdigest(), recovery.MP3_SHA256)

    def test_existing_newer_revision_is_preserved_without_export(self):
        newer = {**self.original, 'revision': 2}
        file = self.data / 'cd_song_scores.json'
        file.write_text(json.dumps([newer]))
        before = file.read_bytes()
        with self.assertRaisesRegex(ValueError, 'local_newer_revision_preserved'):
            recovery.recover(self.state, self.midi, self.data, self.output)
        self.assertEqual(file.read_bytes(), before)
        self.assertFalse(self.output.exists())

    def test_changed_supplied_mp3_is_rejected_before_local_commit(self):
        state = self.folder / 'damaged-state'
        shutil.copytree(self.state, state)
        (state / 'previews/rolling-girl.mp3').write_bytes(b'changed')
        before = (self.data / 'cd_song_scores.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'bundled_mp3_or_manifest_hash_mismatch'):
            recovery.recover(state, self.midi, self.data, self.output)
        self.assertEqual((self.data / 'cd_song_scores.json').read_bytes(), before)
        self.assertFalse(self.output.exists())

    def test_same_revision_different_score_is_preserved(self):
        other = json.loads(json.dumps(self.original))
        other['notes'][0]['midi_note'] += 1
        file = self.data / 'cd_song_scores.json'
        file.write_text(json.dumps([other]))
        before = file.read_bytes()
        with self.assertRaisesRegex(ValueError, 'local_different_score_preserved'):
            recovery.recover(self.state, self.midi, self.data, self.output)
        self.assertEqual(file.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
