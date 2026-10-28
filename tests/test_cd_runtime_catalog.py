"""Real immutable artifact generations with only the store pointer substituted."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import asyncio
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from PIL import Image
from osaka_cd import events, runtime, store
from osaka_cd.arrangement import arrangement_sha256, preview_arrangement
from cogs.tts import cd_covers, cd_previews, melodies


class RuntimeCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg'):
            raise unittest.SkipTest('FFmpeg is required for real audio verification')
        cls.audio = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=1', '-c:a', 'libmp3lame', '-f', 'mp3', 'pipe:1'],
            capture_output=True, check=True, timeout=10).stdout

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict('os.environ', {'OSAKA_CD_DATA_ROOT': str(self.temp),
                                                         'TTV_CD_THUMBNAIL_CACHE_DIR': ''}))
        self.stack.enter_context(patch.object(melodies, '_MELODIES', (melodies._DEFAULT_MELODY,)))
        self.stack.enter_context(patch.object(melodies, '_CATALOG_SIGNATURE', (-1,)))
        self.stack.enter_context(patch.object(melodies, '_FAILED_CATALOG_SIGNATURE', None))
        self.pointer = {'generation': None, 'root': '', 'initialized': False}
        self.reads = Mock(side_effect=lambda: dict(self.pointer))
        self.stack.enter_context(patch.object(store, 'runtime_snapshot', self.reads))
        self.ack = Mock(return_value=True)
        self.stack.enter_context(patch.object(store, 'get_store', return_value=type('Store', (), {'acknowledge': self.ack})()))
        with runtime._LOCK:
            runtime._CONFIG = None
            runtime._CACHE_AT = 0
            runtime._CACHE = runtime._LAST_GOOD = None
            runtime._VALIDATED.clear()
        self.preferences = {'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR'}

    def generation(self, name, pitch, color, arrangement=None):
        root = self.temp / name
        data = root / 'cogs/tts'
        (data / 'cd/previews').mkdir(parents=True)
        (data / 'cd/thumbnails').mkdir()
        score = {'id': 'test-song', 'name': name, 'artist': 'Test', 'excerpt': '',
                 'source_url': '', 'source_sha256': 'a' * 64, 'duration_ms': 1000,
                 'notes': [{'midi_note': pitch, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 90}]}
        preview = {'manifest_version': 2, 'id': 'test-song',
                   'score_sha256': melodies.score_sha256(melodies.catalog_score(score)),
                   'voice_id': 'kasane-teto', 'language': 'pt-BR', 'pitch_semitones': 0,
                   'speech_rate': 1, 'mp3_sha256': hashlib.sha256(self.audio).hexdigest()}
        if arrangement is not None:
            (data / 'cd/arrangements').mkdir()
            (data / 'cd/arrangements/test-song.json').write_text(json.dumps(arrangement))
            score.update(arrangement_path='cogs/tts/cd/arrangements/test-song.json',
                         arrangement_sha256=arrangement_sha256(arrangement),
                         arrangement_preview_start_ms=0, arrangement_preview_end_ms=1000,
                         preview_arrangement_sha256=arrangement_sha256(
                             preview_arrangement(arrangement, start_ms=0, end_ms=1000)['arrangement']))
        (data / 'cd_song_scores.json').write_text(json.dumps([score]))
        if arrangement is None:
            effective = melodies._read_song_melodies(data / 'cd_song_scores.json')[0]
            preview.update(manifest_version=3,
                arrangement_sha256=effective.arrangement_preview_sha256,
                source_arrangement_sha256=effective.arrangement_sha256,
                arrangement_performance_sha256=effective.arrangement_preview_performance_sha256,
                rendering_profile={'mode': 'multipart-utau',
                    'configuration_fingerprint': 'b' * 64, 'render_fingerprint': 'c' * 64})
        output = io.BytesIO()
        Image.new('RGB', (64, 64), color).save(output, format='JPEG')
        cover = output.getvalue()
        (data / 'cd/thumbnails/vnw8zURAxkU.jpg').write_bytes(cover)
        (data / 'cd_song_videos.json').write_text(json.dumps({'songs': {'test-song': {
            'video_id': 'vnw8zURAxkU', 'thumbnail_sha256': hashlib.sha256(cover).hexdigest()}}}))
        (data / 'cd/previews/test-song.mp3').write_bytes(self.audio)
        (data / 'cd/previews/index.json').write_text(json.dumps({'test-song': preview}))
        return {'generation': name, 'root': str(root), 'initialized': True}, cover

    def activate(self, pointer):
        self.pointer = dict(pointer)
        events.publish(pointer)
        runtime.invalidate()

    def test_pin_keeps_score_cover_and_audio_together_during_publication(self):
        first, gray = self.generation('first', 60, 'gray')
        second, blue = self.generation('second', 67, 'blue')
        self.activate(first)
        with runtime.pin_snapshot() as selected:
            queued = melodies.get_melody('test-song')
            self.activate(second)
            self.assertEqual(melodies.get_melody('test-song').name, 'first')
            self.assertEqual(cd_covers._cover_bytes('test-song'), gray)
            self.assertEqual(cd_previews.prepared_sample('test-song', self.preferences), self.audio)
            self.assertEqual(selected['generation'], 'first')
        self.assertEqual(melodies.get_melody('test-song').name, 'second')
        self.assertEqual(cd_covers._cover_bytes('test-song'), blue)
        self.assertEqual(json.loads(queued.score_json)['notes'][0]['midi_note'], 60)
        self.assertEqual(melodies.score_for_melody('test-song')['notes'][0]['midi_note'], 67)

    def test_corrupt_new_metadata_cannot_mix_into_a_valid_catalog(self):
        first, gray = self.generation('first', 60, 'gray')
        second, _ = self.generation('second', 67, 'blue')
        self.activate(first)
        self.assertEqual(melodies.get_melody('test-song').name, 'first')
        (Path(second['root']) / 'cogs/tts/cd_song_videos.json').write_text('{"songs":{"test-song":{"video_id":"bad"}}}')
        self.activate(second)
        self.assertEqual(melodies.get_melody('test-song').name, 'first')
        self.assertEqual(cd_covers._cover_bytes('test-song'), gray)
        runtime.acknowledge()
        self.ack.assert_called_once_with('first')

    def test_explicit_pinned_corrupt_generation_cannot_acknowledge_installed(self):
        candidate, _ = self.generation('corrupt', 60, 'gray')
        (Path(candidate['root']) / 'cogs/tts/cd_song_scores.json').write_text('[] invalid')
        with runtime.pin_snapshot(candidate):
            self.assertFalse(runtime.acknowledge())
        self.ack.assert_not_called()

    def test_real_store_activation_and_rollback_reload_without_restart(self):
        legacy = self.temp / 'legacy'
        legacy.mkdir()
        actual_store = store.RuntimeStore(self.temp / 'store', legacy)
        self.reads.side_effect = actual_store.snapshot
        self.stack.enter_context(patch.object(store, 'get_store', return_value=actual_store))
        def publish(name, pitch):
            selected, _ = self.generation(name, pitch, 'gray')
            root = Path(selected['root'])
            if name == 'second':
                path = root / 'cogs/tts/cd_song_scores.json'
                scores = json.loads(path.read_text())
                scores[0]['revision'] = 2
                path.write_text(json.dumps(scores))
            package = self.temp / (name + '.zip')
            with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
            received = actual_store.receive(package)
            applied = actual_store.process(received['job_id'])
            self.assertEqual(applied['state'], 'applied', applied)
            self.assertFalse(applied['installed'])
            return applied
        first = publish('first', 60)
        frozen = melodies.get_melody('test-song')
        self.assertTrue(runtime.acknowledge())
        self.assertTrue(actual_store.job(first['job_id'])['installed'])
        second = publish('second', 67)
        self.assertEqual(melodies.score_for_melody('test-song')['notes'][0]['midi_note'], 67)
        self.assertTrue(runtime.acknowledge())
        self.assertTrue(actual_store.job(second['job_id'])['installed'])
        rolled = actual_store.rollback('test-song', generation=first['generation'])
        self.assertEqual(rolled['state'], 'applied', rolled)
        self.assertEqual(melodies.score_for_melody('test-song')['notes'][0]['midi_note'], 60)
        self.assertEqual(json.loads(frozen.score_json)['notes'][0]['midi_note'], 60)
        self.assertTrue(runtime.acknowledge())

    def test_hot_reads_reuse_pointer_and_event_invalidates_immediately(self):
        first, _ = self.generation('first', 60, 'gray')
        second, _ = self.generation('second', 67, 'blue')
        self.activate(first)
        self.reads.reset_mock()
        for _ in range(30):
            self.assertEqual(melodies.get_melody('test-song').name, 'first')
            cd_covers.cover_identity('test-song')
        self.assertEqual(self.reads.call_count, 1)
        self.activate(second)
        self.assertEqual(melodies.get_melody('test-song').name, 'second')
        self.assertEqual(self.reads.call_count, 2)

    def test_threads_with_distinct_pins_never_cross_catalog_generations(self):
        first, gray = self.generation('first', 60, 'gray')
        second, blue = self.generation('second', 67, 'blue')
        def inspect(pointer, cover):
            with runtime.pin_snapshot(pointer):
                for _ in range(60):
                    self.assertEqual(melodies.get_melody('test-song').name, pointer['generation'])
                    self.assertEqual(cd_covers._cover_bytes('test-song'), cover)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(inspect, first, gray), executor.submit(inspect, second, blue)]
            for result in results:
                result.result(timeout=5)

    def test_background_task_does_not_inherit_the_request_generation(self):
        first, _ = self.generation('first', 60, 'gray')
        second, _ = self.generation('second', 67, 'blue')
        self.activate(first)
        async def observe():
            await asyncio.sleep(0)
            return melodies.get_melody('test-song').name
        async def run():
            with runtime.pin_snapshot():
                with runtime.unpin_snapshot():
                    background = asyncio.create_task(observe())
                self.activate(second)
                self.assertEqual(melodies.get_melody('test-song').name, 'first')
                self.assertEqual(await background, 'second')
        asyncio.run(run())

    def test_polyphonic_audio_requires_exact_full_and_preview_bindings(self):
        arrangement = {'score_schema': 2, 'duration_ms': 1000, 'source': {'sha256': 'a' * 64},
            'parts': [{'id': 'lead', 'role': 'lead', 'voice_id': 'kasane-teto', 'notes': [
                {'midi_note': 60, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 90}]},
                {'id': 'harmony-1', 'role': 'harmony', 'voice_id': 'kasane-teto', 'notes': [
                {'midi_note': 64, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 90}]}]}
        pointer, _ = self.generation('poly', 60, 'gray', arrangement)
        self.activate(pointer)
        melody = melodies.get_melody('test-song')
        self.assertEqual(melody.arrangement_version, 2)
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))
        path = Path(pointer['root']) / 'cogs/tts/cd/previews/index.json'
        index = json.loads(path.read_text())
        index['test-song'].update(manifest_version=3, arrangement_sha256=melody.arrangement_preview_sha256,
            source_arrangement_sha256=melody.arrangement_sha256,
            arrangement_performance_sha256=melody.arrangement_preview_performance_sha256,
            rendering_profile={'mode': 'multipart-utau', 'configuration_fingerprint': 'b' * 64,
                               'render_fingerprint': 'c' * 64})
        path.write_text(json.dumps(index))
        runtime.invalidate()
        self.assertEqual(cd_previews.prepared_sample('test-song', self.preferences), self.audio)
        own = melodies.arrangement_for_melody({'ttv_melody_id': 'test-song'})
        own['parts'][0]['notes'][0]['midi_note'] = 72
        self.assertEqual(melodies.arrangement_for_melody('test-song')['parts'][0]['notes'][0]['midi_note'], 60)
        index['test-song']['arrangement_sha256'] = 'f' * 64
        path.write_text(json.dumps(index))
        runtime.invalidate()
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))

    def test_polyphonic_profile_cannot_claim_legacy_monophonic_audio(self):
        arrangement = {'score_schema': 2, 'duration_ms': 1000, 'source': {'sha256': 'a' * 64},
            'parts': [{'id': 'lead', 'role': 'lead', 'notes': [
                {'midi_note': 60, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 90}]},
                {'id': 'harmony', 'role': 'harmony', 'notes': [
                {'midi_note': 64, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 90}]}]}
        pointer, _ = self.generation('poly-profile', 60, 'gray', arrangement)
        self.activate(pointer)
        melody = melodies.get_melody('test-song')
        path = Path(pointer['root']) / 'cogs/tts/cd/previews/index.json'
        index = json.loads(path.read_text())
        item = index['test-song']
        item.update(manifest_version=3, arrangement_sha256=melody.arrangement_preview_sha256,
                    source_arrangement_sha256=melody.arrangement_sha256,
                    arrangement_performance_sha256=melody.arrangement_preview_performance_sha256,
                    rendering_profile={'mode': 'legacy-monophonic-migration'})
        path.write_text(json.dumps(index))
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))
        item['rendering_profile'] = {'mode': 'multipart-utau'}
        path.write_text(json.dumps(index))
        self.assertIsNone(cd_previews.prepared_sample('test-song', self.preferences))


class ArrangementLyricsTests(unittest.TestCase):
    def test_dense_full_lead_counts_each_physical_chunk_vowel_attack(self):
        notes = [{'midi_note': 60, 'start_ms': index * 50, 'duration_ms': 50, 'velocity': 90}
                 for index in range(400)]
        arrangement = {'score_schema': 2, 'duration_ms': 20_000,
                       'parts': [{'id': 'lead', 'role': 'lead', 'lyrics_source': 'message', 'notes': notes}]}
        self.assertEqual(melodies.minimum_singing_syllables({'notes': notes}), 2)
        self.assertEqual(melodies.fit_arrangement_lyrics('a a', None, arrangement), ('a a',None))
        self.assertEqual(melodies.fit_arrangement_lyrics('a a a a', None, arrangement), ('a a a a', None))

    def test_written_words_are_preserved_for_real_phonemizer(self):
        notes = [{'midi_note': 60, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 90},
                 {'midi_note': 64, 'start_ms': 2000, 'duration_ms': 1000, 'velocity': 90}]
        arrangement = {'score_schema': 2, 'duration_ms': 3000,
                       'parts': [{'id': 'lead', 'role': 'lead', 'lyrics_source': 'message', 'notes': notes}]}
        self.assertEqual(melodies.fit_arrangement_lyrics('amanheceu', None, arrangement), ('amanheceu', None))


if __name__ == '__main__':
    unittest.main()
