"""Executable without Discord or pytest: real MP3 and score identity checks."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cogs.tts import cd_previews, melodies


class OfflineCDArtifacts(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(melodies, '_SCORE_FILE', self.root / 'cd_song_scores.json'))
        self.stack.enter_context(patch.object(melodies, '_CATALOG_SIGNATURE', (-1, -1)))
        self.stack.enter_context(patch.object(melodies, '_MELODIES', (melodies._DEFAULT_MELODY,)))
        self.stack.enter_context(patch.object(cd_previews, '_ROOT', self.root / 'cd' / 'previews'))
        self.song = {'id': 'test-song', 'name': 'Test Song', 'artist': 'Test Composer', 'excerpt': '',
                     'source_url': 'https://example.org/score.mid', 'source_sha256': 'a' * 64,
                     'duration_ms': 1400, 'notes': [
                         {'midi_note': 60, 'start_ms': 100, 'duration_ms': 500, 'velocity': 100},
                         {'midi_note': 64, 'start_ms': 800, 'duration_ms': 500, 'velocity': 100}]}
        self.preferences = {'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR',
                            'ttv_pitch_semitones': 0, 'ttv_speech_rate': 1}
        self.write_score(self.song)

    def write_score(self, song):
        melodies._SCORE_FILE.write_text(json.dumps([song]), encoding='utf-8')
        return melodies.get_melody(song['id'])

    def install_audio(self, audio):
        cd_previews._ROOT.mkdir(parents=True)
        (cd_previews._ROOT / 'test-song.mp3').write_bytes(audio)
        melody = melodies.get_melody('test-song')
        self.index = {'test-song': {'manifest_version': 3,
            'score_sha256': melodies.score_sha256(melodies.catalog_score(self.song)),
            'arrangement_sha256': melody.arrangement_preview_sha256,
            'source_arrangement_sha256': melody.arrangement_sha256,
            'arrangement_performance_sha256': melody.arrangement_preview_performance_sha256,
            'rendering_profile': {'mode': 'multipart-utau',
                'configuration_fingerprint': 'b' * 64, 'render_fingerprint': 'c' * 64},
            'mp3_sha256': hashlib.sha256(audio).hexdigest(), 'voice_id': 'kasane-teto',
            'language': 'pt-BR', 'pitch_semitones': 0, 'speech_rate': 1}}
        self.write_index()

    def write_index(self):
        (cd_previews._ROOT / 'index.json').write_text(json.dumps(self.index), encoding='utf-8')

    def real_audio(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg unavailable')
        return subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=1.4', '-c:a', 'libmp3lame', '-f', 'mp3', 'pipe:1'],
            capture_output=True, check=True, timeout=15).stdout

    def test_real_mp3_is_checked_and_decodes(self):
        audio = self.real_audio()
        self.install_audio(audio)
        accepted = cd_previews.prepared_sample('test-song', self.preferences)
        self.assertEqual(accepted, audio)
        decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-f', 's16le', 'pipe:1'],
                                 input=accepted, capture_output=True, timeout=15)
        self.assertEqual(decoded.returncode, 0)
        self.assertGreater(len(decoded.stdout), 44100)

    def test_score_edit_invalidates_mp3_with_unchanged_midi_source(self):
        self.install_audio(self.real_audio())
        self.song['notes'][0]['midi_note'] = 62
        self.write_score(self.song)
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))

    def test_speech_pitch_and_rate_do_not_change_absolute_score_preview(self):
        self.install_audio(self.real_audio())
        preferences = {**self.preferences, 'ttv_pitch_semitones': '-2.0', 'ttv_speech_rate': 1.25}
        self.assertIsNotNone(cd_previews.prepared_sample('test-song', preferences))

    def test_provenance_or_cover_edit_does_not_change_musical_identity(self):
        self.install_audio(self.real_audio())
        self.song['name'] = 'Updated display title'
        self.write_score(self.song)
        (self.root / 'cd_song_videos.json').write_text('{"songs":{}}')
        self.assertIsNotNone(cd_previews.prepared_sample('test-song', self.preferences))

    def test_legacy_mono_audio_requires_a_new_harmonized_sample(self):
        self.install_audio(self.real_audio())
        meta = self.index['test-song']
        digest = meta['score_sha256']
        meta.pop('manifest_version')
        meta['score_sha256'] = self.song['source_sha256']
        self.write_index()
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))
        meta['score_content_sha256'] = digest
        self.write_index()
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))
        meta.update(manifest_version=3, score_sha256=digest)
        self.write_index()
        self.assertIsNotNone(cd_previews.prepared_sample('test-song', self.preferences))

    def test_corrupt_audio_and_profile_are_refused(self):
        self.install_audio(b'ID3' + b'x' * 1024)
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))
        self.index['test-song']['pitch_semitones'] = 1
        self.write_index()
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))
        self.assertIsNone(cd_previews.prepared_sample('../test-song', self.preferences))

    def test_invalid_catalog_preserves_complete_snapshot(self):
        before = melodies.get_melody('test-song')
        for mutation in ('overlap', 'end', 'hash', 'bool'):
            with self.subTest(mutation=mutation):
                bad = deepcopy(self.song)
                if mutation == 'overlap':
                    bad['notes'][1]['start_ms'] = 200
                elif mutation == 'end':
                    bad['duration_ms'] = 1000
                elif mutation == 'hash':
                    bad['score_sha256'] = '0' * 64
                else:
                    bad['notes'][0]['midi_note'] = True
                self.assertEqual(self.write_score(bad), before)

    def test_watcher_signature_includes_audio_arrival(self):
        before = melodies.catalog_signature()
        self.install_audio(self.real_audio())
        self.assertNotEqual(before, melodies.catalog_signature())

    def test_pauses_and_octaves_preserve_actual_timeline(self):
        song = melodies.get_melody('test-song')
        self.assertEqual(melodies.format_score_notes(song), 'C4 · E4')
        self.assertEqual(melodies.format_score_rhythm(song), '𝄽0,1s · 0,5s · 𝄽0,2s · 0,5s · 𝄽0,1s')


if __name__ == '__main__':
    unittest.main()
