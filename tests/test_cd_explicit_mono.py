"""Explicit MIDI mono intent survives import, publication and playback."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/ttv-cd'))

import catalog_import
from cd_metadata import MetadataSearchError
from midi_extract import MONO_PLAYBACK_MARKER, extract_arrangement, extract_midi
from osaka_cd.arrangement import arrangement_performance_sha256, preview_arrangement
from osaka_cd.harmony import harmonize_monophonic
from osaka_cd import runtime
from osaka_cd.store import RuntimeStore
from cogs.tts import melodies


def _vlq(value):
    result = [value & 127]
    while value > 127:
        value >>= 7
        result.append(128 | (value & 127))
    return bytes(reversed(result))


def _meta(kind, data=b''):
    return bytes((255, kind)) + _vlq(len(data)) + data


def _track(events, *, end=1920):
    result, cursor = b'', 0
    for tick, payload in sorted(events, key=lambda item: item[0]):
        result += _vlq(tick - cursor) + payload
        cursor = tick
    result += _vlq(end - cursor) + _meta(47)
    return b'MTrk' + len(result).to_bytes(4, 'big') + result


def _midi(*tracks):
    header = (int(len(tracks) > 1).to_bytes(2, 'big')
              + len(tracks).to_bytes(2, 'big') + (480).to_bytes(2, 'big'))
    return b'MThd\x00\x00\x00\x06' + header + b''.join(tracks)


def _voice(pitches=(72, 74, 76, 72)):
    events = [(0, _meta(3, b'Lead Vocal'))]
    for index, pitch in enumerate(pitches):
        events.extend(((index * 480, bytes((144, pitch, 90))),
                       ((index + 1) * 480, bytes((128, pitch, 0)))))
    return events


class ExplicitMonoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cd-explicit-mono-')
        self.folder = Path(self.temporary.name)
        self.path = self.folder / 'Example Song.mid'

    def tearDown(self):
        self.temporary.cleanup()

    def arrangement(self, events=None, **kwargs):
        events = _voice() if events is None else events
        self.path.write_bytes(_midi(_track(events)))
        return extract_arrangement(self.path, **kwargs)['arrangement']

    def test_exact_marker_keeps_original_notes_through_preview_and_harmony(self):
        arrangement = self.arrangement([(0, _meta(1, MONO_PLAYBACK_MARKER))] + _voice())
        before = deepcopy(arrangement)
        self.assertIs(arrangement['source']['monophonic_playback'], True)
        effective = harmonize_monophonic(arrangement)
        self.assertEqual(arrangement, before)
        self.assertEqual(effective, arrangement)
        self.assertIsNot(effective, arrangement)
        excerpt = preview_arrangement(effective, start_ms=500, end_ms=1500)['arrangement']
        self.assertIs(excerpt['source']['monophonic_playback'], True)
        self.assertEqual(len(harmonize_monophonic(excerpt)['parts']), 1)
        self.assertEqual([note['midi_note'] for note in excerpt['parts'][0]['notes']], [74, 76])
        self.assertFalse(excerpt['selection']['generated_harmonies'])

    def test_unmarked_mono_still_receives_automatic_harmony(self):
        original = self.arrangement()
        effective = harmonize_monophonic(original)
        self.assertNotIn('monophonic_playback', original['source'])
        self.assertEqual(len(effective['parts']), 2)
        self.assertEqual(effective['parts'][0], original['parts'][0])
        self.assertTrue(effective['automatic_harmony']['generated'])

    def test_marker_must_be_exact_initial_text_meta_not_names_lyrics_or_sysex(self):
        variants = [(0, _meta(3, MONO_PLAYBACK_MARKER)),
                    (0, _meta(5, MONO_PLAYBACK_MARKER)),
                    (1, _meta(1, MONO_PLAYBACK_MARKER)),
                    (0, _meta(1, MONO_PLAYBACK_MARKER + b' ')),
                    (0, _meta(1, b'prefix ' + MONO_PLAYBACK_MARKER)),
                    (0, b'\xf0' + _vlq(len(MONO_PLAYBACK_MARKER)) + MONO_PLAYBACK_MARKER)]
        for event in variants:
            with self.subTest(event=event):
                arrangement = self.arrangement([event] + _voice())
                self.assertNotIn('monophonic_playback', arrangement['source'])
                self.assertEqual(len(harmonize_monophonic(arrangement)['parts']), 2)

    def test_marker_chooses_only_lead_but_retains_all_original_source_voices(self):
        conductor = _track([(0, _meta(1, MONO_PLAYBACK_MARKER))])
        lead = _track(_voice())
        backing = _track(_voice((60, 62, 64, 60)))
        self.path.write_bytes(_midi(conductor, lead, backing))
        arrangement = extract_arrangement(self.path, track=1)['arrangement']
        self.assertEqual(len(arrangement['parts']), 1)
        self.assertEqual(arrangement['parts'][0]['track_index'], 1)
        self.assertEqual(arrangement['source']['note_count'], 8)
        self.assertEqual(sum(len(stream['notes']) for stream in arrangement['source']['streams']), 8)
        self.assertEqual(len(harmonize_monophonic(arrangement)['parts']), 1)
        self.assertEqual(arrangement['selection']['max_parts'], 1)

    def test_mono_marker_keeps_legacy_score_but_changes_effective_performance_identity(self):
        plain = self.arrangement()
        plain_score = extract_midi(self.path)['score']
        marked = self.arrangement([(0, _meta(1, MONO_PLAYBACK_MARKER))] + _voice())
        self.assertEqual(plain_score, extract_midi(self.path)['score'])
        self.assertNotEqual(plain['source']['sha256'], marked['source']['sha256'])
        self.assertEqual(marked['source']['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertNotEqual(arrangement_performance_sha256(harmonize_monophonic(plain)),
                            arrangement_performance_sha256(harmonize_monophonic(marked)))

    def test_nonboolean_metadata_cannot_accidentally_disable_harmony(self):
        original = self.arrangement()
        for value in ('true', 'mono', 1, [], {}, False):
            with self.subTest(value=value):
                original['source']['monophonic_playback'] = value
                self.assertEqual(len(harmonize_monophonic(original)['parts']), 2)

    def test_no_json_import_roundtrip_keeps_mono_in_real_runtime_catalog(self):
        self.arrangement([(0, _meta(1, MONO_PLAYBACK_MARKER))] + _voice())
        self.assertFalse(self.path.with_suffix('.json').exists())
        local = self.folder / 'local'
        local.mkdir()
        (local / 'cd_song_scores.json').write_text('[]')
        (local / 'cd_song_videos.json').write_text('{"version":1,"songs":{}}')
        package = self.folder / 'export.zip'
        with patch.object(catalog_import, '_search', side_effect=MetadataSearchError('provider_offline')):
            result = asyncio.run(catalog_import.import_midi(self.path, root=local,
                cache_dir=self.folder / 'cache', render_sample=False, export_zip=package))
        self.assertEqual(result['parts'], 1)
        self.assertEqual(result['notes'], 4)
        stored = json.loads((local / 'arrangements/example-song.json').read_text())
        self.assertIs(stored['source']['monophonic_playback'], True)
        legacy = self.folder / 'legacy'
        legacy.mkdir()
        store = RuntimeStore(self.folder / 'runtime', legacy)
        receipt = store.receive(package)
        self.assertTrue(store.process(receipt['job_id'])['applied'])
        with runtime.unpin_snapshot(), runtime.pin_snapshot(store.snapshot()):
            song = melodies.get_melody('example-song')
            playback = melodies.arrangement_for_melody('example-song')
            self.assertEqual(len(playback['parts']), 1)
            self.assertEqual(playback['parts'][0]['notes'], stored['parts'][0]['notes'])
            self.assertEqual(song.arrangement_sha256, result['arrangement_sha256'])
            self.assertIs(playback['source']['monophonic_playback'], True)


if __name__ == '__main__':
    unittest.main()
