"""Catalog/runtime integration for generated harmony over immutable mono CDs."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack
from copy import deepcopy
import array
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from cogs.tts import cd_previews, melodies
from cogs.tts.arrangement_audio import execution_arrangement
from cogs.tts.performance import parse_performance_text
from osaka_cd import runtime
from osaka_cd.arrangement import (
    arrangement_performance_sha256, arrangement_sha256,
    preview_arrangement, validate_arrangement,
)
from osaka_cd.lyrics import prepare_message_arrangement
from osaka_cd.progressive import stream_arrangement


ROOT = Path(__file__).resolve().parents[1]
TIMING = ('midi_note', 'start_ms', 'duration_ms', 'velocity')


def note(pitch, start, duration=500):
    return {'midi_note': pitch, 'start_ms': start,
            'duration_ms': duration, 'velocity': 90}


def source_arrangement(parts=1):
    value = {'score_schema': 2, 'duration_ms': 4000,
             'source': {'sha256': 'a' * 64,
                        'tracks': [{'key_signatures': [
                            {'tick': 0, 'accidentals': 0, 'minor': False}]}]},
             'parts': [{'id': 'lead', 'role': 'lead', 'voice_id': 'kasane-teto',
                        'lyrics_source': 'message', 'gain': .81, 'pan': -.07,
                        'notes': [note(60, 0), note(64, 500),
                                  note(67, 1500, 700), note(65, 2700)]}]}
    if parts >= 2:
        value['parts'].append({'id': 'authored-harmony', 'role': 'harmony',
            'voice_id': 'hatsune-miku', 'lyrics_source': 'midi', 'gain': .43,
            'pan': -.25, 'notes': [{**note(72, 650, 2000), 'lyric': 'a'}]})
    if parts >= 3:
        value['parts'].append({'id': 'authored-bass', 'role': 'bass',
            'voice_id': 'kasane-teto', 'lyrics_source': 'vowel', 'gain': .28,
            'pan': .21, 'notes': [note(48, 500, 2700)]})
    return validate_arrangement(value)


def source_entry(arrangement=None, *, sidecar=False):
    if arrangement is None:
        notes, duration = [note(60, 0), note(64, 1000), note(67, 2000)], 3000
    else:
        selected = preview_arrangement(arrangement, start_ms=500,
                                       end_ms=3500)['arrangement']
        notes, duration = selected['parts'][0]['notes'], selected['duration_ms']
    entry = {'id': 'test-song', 'name': 'Test Song', 'artist': 'Composer',
             'excerpt': 'Selected phrase', 'source_url': '',
             'source_sha256': 'a' * 64, 'revision': 3,
             'duration_ms': duration, 'notes': deepcopy(notes)}
    entry['score_sha256'] = melodies.score_sha256(melodies.catalog_score(entry))
    if arrangement is not None:
        entry.update(arrangement_sha256=arrangement_sha256(arrangement),
            arrangement_preview_start_ms=500, arrangement_preview_end_ms=3500,
            preview_arrangement_sha256=arrangement_sha256(selected),
            preview_performance_sha256=arrangement_performance_sha256(selected))
        if sidecar:
            entry['arrangement_path'] = 'cogs/tts/cd/arrangements/test-song.json'
        else:
            entry['arrangement'] = deepcopy(arrangement)
    return entry


class HarmonizedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.catalog = self.root / 'cd_song_scores.json'
        self.stack.enter_context(runtime.pin_snapshot({'initialized': False}))
        for name, value in (
                ('_SCORE_FILE', self.catalog),
                ('_MELODIES', (melodies._DEFAULT_MELODY,)),
                ('_CATALOG_SIGNATURE', (-1,)),
                ('_FAILED_CATALOG_SIGNATURE', None),
                ('_LOOKUP_SNAPSHOT', None), ('_LOOKUP', {}),
                ('_SCORE_SNAPSHOTS', OrderedDict()), ('_SCORE_CACHE_BYTES', 0)):
            self.stack.enter_context(patch.object(melodies, name, value))
        self.stack.enter_context(patch.object(cd_previews, '_ROOT', self.root / 'cd/previews'))
        cd_previews._index.cache_clear()
        self.addCleanup(cd_previews._index.cache_clear)

    def install(self, entry, arrangement=None, *, root=None):
        root = root or self.root
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'cd_song_scores.json'
        path.write_text(json.dumps([entry]), encoding='utf-8')
        if entry.get('arrangement_path'):
            sidecar = root / 'cd/arrangements/test-song.json'
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(arrangement), encoding='utf-8')
        return path

    def assert_exact_lead(self, arrangement, notes):
        lead = next(part for part in arrangement['parts'] if part['role'] == 'lead')
        self.assertEqual([{key: n[key] for key in TIMING} for n in lead['notes']],
                         [{key: n[key] for key in TIMING} for n in notes])

    def test_every_repository_and_installation_cd_has_harmony_without_rewriting_music(self):
        paths = (ROOT / 'cogs/tts/cd_song_scores.json',
                 ROOT / 'deploy/ttv-cd/catalog/cd_song_scores.json')
        for path in paths:
            before = path.read_bytes()
            raw = json.loads(before)
            loaded = melodies._read_song_melodies(path)
            self.assertEqual(len(loaded), len(raw))
            self.assertGreaterEqual(len(loaded), 10)
            for entry, melody in zip(raw, loaded):
                with self.subTest(catalog=path.name, song=melody.id):
                    full = json.loads(melody.arrangement_json)
                    preview = json.loads(melody.arrangement_preview_json)
                    self.assertEqual(len(full['parts']), 2)
                    self.assertEqual(len(preview['parts']), 2)
                    self.assert_exact_lead(full, entry['notes'])
                    self.assert_exact_lead(preview, entry['notes'])
                    self.assertEqual(full['duration_ms'], entry['duration_ms'])
                    self.assertEqual(json.loads(melody.score_json), melodies.catalog_score(entry))
                    self.assertEqual(full['source']['sha256'], entry['source_sha256'])
            self.assertEqual(path.read_bytes(), before)

    def test_default_cd_is_harmonized_with_identical_lead_and_duration(self):
        default = melodies._DEFAULT_MELODY
        full = json.loads(default.arrangement_json)
        score = json.loads(default.score_json)
        self.assertEqual(len(full['parts']), 2)
        self.assert_exact_lead(full, score['notes'])
        self.assertEqual(full['duration_ms'], score['duration_ms'])

    def test_imported_inline_and_sidecar_mono_receive_harmony_after_validation(self):
        original = source_arrangement()
        for sidecar in (False, True):
            with self.subTest(sidecar=sidecar):
                directory = self.root / str(sidecar)
                entry = source_entry(original, sidecar=sidecar)
                path = self.install(entry, original, root=directory)
                before = {p: p.read_bytes() for p in directory.rglob('*') if p.is_file()}
                melody, = melodies._read_song_melodies(path)
                effective = json.loads(melody.arrangement_json)
                self.assertEqual(len(effective['parts']), 2)
                self.assertEqual(effective['parts'][0], original['parts'][0])
                self.assertEqual(effective['source'], original['source'])
                self.assertNotEqual(melody.arrangement_sha256, entry['arrangement_sha256'])
                self.assertNotEqual(melody.arrangement_preview_sha256, entry['preview_arrangement_sha256'])
                self.assert_exact_lead(json.loads(melody.arrangement_preview_json), entry['notes'])
                self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_corrupt_source_and_preview_hashes_are_rejected_before_overlay(self):
        for sidecar in (False, True):
            for field in ('score_sha256', 'arrangement_sha256',
                          'preview_arrangement_sha256', 'preview_performance_sha256'):
                with self.subTest(sidecar=sidecar, field=field):
                    original = source_arrangement()
                    entry = source_entry(original, sidecar=sidecar)
                    entry[field] = 'f' * 64
                    path = self.install(entry, original)
                    with self.assertRaisesRegex(ValueError, 'invalid_cd_'):
                        melodies._read_song_melodies(path)

    def test_authored_two_and_three_voice_arrangements_and_identities_are_exact(self):
        for parts in (2, 3):
            for sidecar in (False, True):
                with self.subTest(parts=parts, sidecar=sidecar):
                    original = source_arrangement(parts)
                    entry = source_entry(original, sidecar=sidecar)
                    path = self.install(entry, original)
                    melody, = melodies._read_song_melodies(path)
                    preview = preview_arrangement(original, start_ms=500, end_ms=3500)['arrangement']
                    self.assertEqual(json.loads(melody.arrangement_json), original)
                    self.assertEqual(json.loads(melody.arrangement_preview_json), preview)
                    self.assertEqual(melody.arrangement_sha256, entry['arrangement_sha256'])
                    self.assertEqual(melody.arrangement_preview_sha256, entry['preview_arrangement_sha256'])
                    self.assertEqual(melody.arrangement_preview_performance_sha256,
                                     entry['preview_performance_sha256'])

    def test_returned_compositions_are_independent_of_reader_and_each_other(self):
        entry = source_entry()
        self.install(entry)
        first = melodies.arrangement_for_melody('test-song', preview=True)
        pristine = deepcopy(first)
        first['parts'][0]['notes'][0]['midi_note'] = 96
        first['parts'][1]['notes'].clear()
        self.assertEqual(melodies.arrangement_for_melody('test-song', preview=True), pristine)
        self.assertEqual(melodies.score_for_melody('test-song'), melodies.catalog_score(entry))

    def test_generated_harmony_obeys_selected_voice_and_timed_holds_without_filling_rests(self):
        self.install(source_entry(source_arrangement()))
        source = melodies.arrangement_for_melody('test-song', preview=True)
        template = execution_arrangement(source, 'hatsune-miku')
        for text, holds in (('a~', 1), ('a~~~', 3), ('a ~ ~ ~', 3), ('a~mor~', 2)):
            with self.subTest(text=text):
                clean, segments = parse_performance_text(text, singing=True)
                prepared = prepare_message_arrangement(template, clean, segments)
                self.assertEqual(len(prepared['parts']), 2)
                self.assertTrue(all(part['voice_id'] == 'hatsune-miku' for part in prepared['parts']))
                self.assertEqual(prepared['lyrics_adaptation']['requested_hold_ms'], holds * 1000)
                lead, harmony = prepared['parts']
                ending = lead['notes'][-1]['start_ms'] + lead['notes'][-1]['duration_ms']
                self.assertEqual(prepared['duration_ms'], ending)
                self.assertLessEqual(harmony['notes'][-1]['start_ms'] + harmony['notes'][-1]['duration_ms'], ending)
                # The generated voice repeats exactly the same rhythm as the
                # source; no vowel is allowed to bridge one of its rests.
                period = template['duration_ms']
                source_notes = template['parts'][1]['notes']
                for emitted in harmony['notes']:
                    relative = emitted['start_ms'] % period
                    candidates = [n for n in source_notes if n['start_ms'] == relative]
                    self.assertEqual(len(candidates), 1)
                    self.assertLessEqual(emitted['duration_ms'], candidates[0]['duration_ms'])
                self.assertEqual(template, execution_arrangement(source, 'hatsune-miku'))

    def test_old_mono_sample_rejected_and_effective_harmonized_manifest_accepted(self):
        entry = source_entry()
        self.install(entry)
        melody = melodies.get_melody('test-song')
        audio = b'checked-mp3-fixture' * 30
        folder = self.root / 'cd/previews'
        folder.mkdir(parents=True)
        (folder / 'test-song.mp3').write_bytes(audio)
        metadata = {'manifest_version': 2, 'id': 'test-song', 'revision': entry['revision'],
                    'score_sha256': entry['score_sha256'], 'source_sha256': entry['source_sha256'],
                    'voice_id': 'kasane-teto', 'language': 'pt-BR',
                    'pitch_semitones': 0, 'speech_rate': 1,
                    'mp3_sha256': hashlib.sha256(audio).hexdigest()}
        index = folder / 'index.json'
        index.write_text(json.dumps({'test-song': metadata}), encoding='utf-8')
        preferences = {'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR'}
        with patch.object(cd_previews, '_has_mp3_frames', return_value=True):
            self.assertIsNone(cd_previews.prepared_sample('test-song', preferences))
            metadata.update(manifest_version=3,
                arrangement_sha256=melody.arrangement_preview_sha256,
                source_arrangement_sha256=melody.arrangement_sha256,
                arrangement_performance_sha256=melody.arrangement_preview_performance_sha256,
                rendering_profile={'mode': 'multipart-utau',
                    'configuration_fingerprint': 'b' * 64, 'render_fingerprint': 'c' * 64})
            index.write_text(json.dumps({'test-song': metadata}), encoding='utf-8')
            self.assertEqual(cd_previews.prepared_sample('test-song', preferences), audio)
            metadata['rendering_profile']['mode'] = 'legacy-monophonic-migration'
            index.write_text(json.dumps({'test-song': metadata}), encoding='utf-8')
            self.assertIsNone(cd_previews.prepared_sample('test-song', preferences))

    def test_generated_voices_reach_real_progressive_stereo_mix_before_final_request(self):
        entry = source_entry()
        entry.update(duration_ms=8000, notes=[note(69, index * 500) for index in range(16)])
        entry['score_sha256'] = melodies.score_sha256(melodies.catalog_score(entry))
        self.install(entry)
        template = melodies.arrangement_for_melody('test-song', preview=True)
        text = ' '.join(['a'] * 16)
        arranged = prepare_message_arrangement(template, text)
        calls, chunks, emitted_after_calls = [], [], []
        rate = 48000

        def render(**request):
            calls.append(deepcopy(request))
            score = request['score']
            samples = array.array('h', [0]) * round(score['duration_ms'] * rate / 1000)
            for item in score['notes']:
                first = round(item['start_ms'] * rate / 1000)
                stop = round((item['start_ms'] + item['duration_ms']) * rate / 1000)
                frequency = 440 * 2 ** ((item['midi_note'] - 69) / 12)
                for frame in range(first, stop):
                    samples[frame] = round(6000 * math.sin(2 * math.pi * frequency * (frame-first) / rate))
            if sys.byteorder != 'little':
                samples.byteswap()
            target = io.BytesIO()
            with wave.open(target, 'wb') as output:
                output.setparams((1, 2, rate, 0, 'NONE', ''))
                output.writeframes(samples.tobytes())
            raw = target.getvalue()
            return raw, {'midi_preroll_ms': 0, 'normalization_gain': 1.0,
                         'sha256': hashlib.sha256(raw).hexdigest()}

        def emit(pcm):
            chunks.append(pcm)
            emitted_after_calls.append(len(calls))

        result = stream_arrangement(arranged, text, render, emit,
                                    require_normalization=True)
        self.assertLess(emitted_after_calls[0], len(calls))
        self.assertEqual({call['role'] for call in calls[:emitted_after_calls[0]]},
                         {'lead', 'harmony'})
        lead_pitches = {n['midi_note'] for call in calls if call['role'] == 'lead'
                        for n in call['score']['notes']}
        harmony_pitches = {n['midi_note'] for call in calls if call['role'] == 'harmony'
                           for n in call['score']['notes']}
        self.assertEqual(lead_pitches, {69})
        self.assertTrue(lead_pitches.isdisjoint(harmony_pitches))
        pcm = b''.join(chunks)
        self.assertEqual(result['pcm_sha256'], hashlib.sha256(pcm).hexdigest())
        self.assertEqual(len(pcm), round((arranged['duration_ms'] + 350) * rate / 1000) * 4)
        target = io.BytesIO()
        with wave.open(target, 'wb') as output:
            output.setparams((2, 2, rate, 0, 'NONE', ''))
            output.writeframes(pcm)
        with wave.open(io.BytesIO(target.getvalue()), 'rb') as decoded:
            self.assertEqual((decoded.getnchannels(), decoded.getsampwidth(), decoded.getframerate()),
                             (2, 2, rate))
            samples = array.array('h', decoded.readframes(decoded.getnframes()))
        if sys.byteorder != 'little':
            samples.byteswap()
        first = round(.350 * rate) * 2
        self.assertFalse(any(samples[:first]))
        left = samples[first:first + 4800 * 2:2]
        right = samples[first + 1:first + 4800 * 2:2]
        self.assertNotEqual(left, right)

        def energy(frequency):
            return abs(sum(value * complex(math.cos(2*math.pi*frequency*frame/rate),
                                           math.sin(2*math.pi*frequency*frame/rate))
                           for frame, value in enumerate(left)))

        harmony_frequency = 440 * 2 ** ((next(iter(harmony_pitches)) - 69) / 12)
        noise = max(energy(100), energy(200), energy(800))
        self.assertGreater(energy(440), noise * 10)
        self.assertGreater(energy(harmony_frequency), noise * 10)

    def test_published_runtime_generation_receives_overlay_without_republication(self):
        generation = self.root / 'published-generation'
        data = generation / 'cogs/tts'
        entry = source_entry(source_arrangement(), sidecar=True)
        self.install(entry, source_arrangement(), root=data)
        (data / 'cd/previews').mkdir(parents=True)
        (data / 'cd/previews/index.json').write_text('{}', encoding='utf-8')
        (data / 'cd_song_videos.json').write_text('{"songs": {}}', encoding='utf-8')
        before = {p: p.read_bytes() for p in data.rglob('*') if p.is_file()}
        pointer = {'generation': 'already-published', 'initialized': True,
                   'root': str(generation)}
        with runtime.unpin_snapshot(), runtime.pin_snapshot(pointer):
            melody = melodies.get_melody('test-song')
            self.assertEqual(melodies._score_path(), data / 'cd_song_scores.json')
            self.assertEqual(len(json.loads(melody.arrangement_json)['parts']), 2)
            self.assert_exact_lead(melodies.arrangement_for_melody('test-song', preview=True), entry['notes'])
            self.assertNotEqual(melody.arrangement_sha256, entry['arrangement_sha256'])
            self.assertEqual(runtime.snapshot()['generation'], 'already-published')
        self.assertEqual({p: p.read_bytes() for p in before}, before)


if __name__ == '__main__':
    unittest.main()
