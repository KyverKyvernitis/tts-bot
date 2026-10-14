"""Polyphonic MIDI fixtures plus the provided original Rolling Girl source."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

from pathlib import Path as _CDTestPath
import sys as _CDTestSys
_CDTestSys.path.insert(0, str(_CDTestPath(__file__).resolve().parents[1] / 'deploy/ttv-cd'))

from osaka_cd.arrangement import arrangement_render_plan, arrangement_sha256, preview_arrangement

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("cd_midi_arrangement_module", _ROOT / "deploy/ttv-cd/midi_extract.py")
module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = module
_SPEC.loader.exec_module(module)


def vlq(value):
    result = [value & 127]
    while value > 127:
        value >>= 7
        result.append(128 | (value & 127))
    return bytes(reversed(result))


def meta(kind, payload=b""):
    return bytes((255, kind)) + vlq(len(payload)) + payload


def track(events=(), name="", end=None):
    events = list(events)
    if name:
        events.insert(0, (0, meta(3, name.encode())))
    events.sort(key=lambda e: e[0])
    result, cursor = b"", 0
    for tick, event in events:
        result += vlq(tick - cursor) + event
        cursor = tick
    result += vlq((cursor if end is None else end) - cursor) + meta(47)
    return b"MTrk" + len(result).to_bytes(4, "big") + result


def midi(*tracks, ppq=480):
    header = (0 if len(tracks) == 1 else 1).to_bytes(2, "big") + len(tracks).to_bytes(2, "big") + ppq.to_bytes(2, "big")
    return b"MThd" + len(header).to_bytes(4, "big") + header + b"".join(tracks)


def note(pitch, start=0, end=480, channel=0, velocity=100):
    return [(start, bytes((144 | channel, pitch, velocity))), (end, bytes((128 | channel, pitch, 0)))]


class MidiArrangementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.mid"

    def tearDown(self):
        self.temp.cleanup()

    def extract(self, data, **kwargs):
        self.path.write_bytes(data)
        return module.extract_arrangement(self.path, **kwargs)

    def test_polyphonic_triads_keep_real_notes_in_three_synchronized_parts(self):
        pitches = [(72, 64, 48), (74, 65, 50), (71, 62, 47), (72, 64, 48)]
        events = sum((note(p, i * 480, (i + 1) * 480) for i, chord in enumerate(pitches) for p in chord), [])
        result = self.extract(midi(track(events, name="Piano arrangement")))
        arrangement = result["arrangement"]
        self.assertEqual(len(arrangement["parts"]), 3)
        self.assertEqual(sum(len(p["notes"]) for p in arrangement["parts"]), 12)
        self.assertEqual(arrangement["source"]["note_count"], 12)
        original_pitches = {p for chord in pitches for p in chord}
        self.assertTrue(all(n["midi_note"] in original_pitches for p in arrangement["parts"] for n in p["notes"]))
        self.assertFalse(arrangement["selection"]["generated_harmonies"])
        self.assertFalse(arrangement["selection"]["vocal_transcription_confirmed"])
        self.assertTrue(all([n["start_ms"] for n in p["notes"]] == [0, 500, 1000, 1500]
                            for p in arrangement["parts"]))

    def test_monophonic_source_does_not_create_invented_harmony(self):
        result = self.extract(midi(track(note(60) + note(64, 480, 960), name="Vocal")))
        self.assertEqual(len(result["arrangement"]["parts"]), 1)
        confidence = result["arrangement"]["parts"][0]["confidence"]
        self.assertEqual(confidence["vocal_status"], "midi_indicated")
        self.assertFalse(confidence["vocal_transcription_confirmed"])

    def test_alternating_phrases_without_simultaneous_notes_are_not_harmonies(self):
        lead = track(sum((note(72, start, start + 96) for start in (0, 480, 960)), []),
                     name="Lead Vocal", end=1056)
        harmony = track(sum((note(67, start, start + 96) for start in (192, 768)), []),
                        name="Harmony", end=1056)
        result = self.extract(midi(lead, harmony))
        self.assertEqual(len(result["arrangement"]["parts"]), 1)
        self.assertEqual(result["arrangement"]["source"]["note_count"], 5)

    def test_notes_held_over_lower_attacks_remain_in_independent_voices(self):
        events = note(72, 0, 1440) + note(60, 0, 480) + note(62, 480, 960) + note(64, 960, 1440)
        result = self.extract(midi(track(events, name="Vocal arrangement")))
        parts = result["arrangement"]["parts"]
        held = next(n for p in parts for n in p["notes"] if n["midi_note"] == 72)
        self.assertEqual((held["start_ms"], held["duration_ms"]), (0, 1500))
        self.assertEqual(sum(len(p["notes"]) for p in parts), 4)
        self.assertTrue(all(p["confidence"]["vocal_status"] == "ambiguous" for p in parts))

    def test_pedal_repeated_keys_program_and_key_metadata_are_retained(self):
        events = [(0, b"\xc0\x03"), (0, b"\xb0\x40\x7f"),
                  (0, meta(89, bytes((254, 1)))), (0, meta(88, bytes((3, 2, 24, 8)))),
                  (0, b"\x90\x3c\x64"), (120, b"\x90\x3c\x50"),
                  (480, b"\x80\x3c\x00"), (600, b"\x80\x3c\x00"),
                  (960, b"\xb0\x40\x00"), (960, b"\xc0\x05")]
        events += note(64, 960, 1440)
        result = self.extract(midi(track(events)))
        source = result["arrangement"]["source"]
        notes = source["streams"][0]["notes"]
        self.assertEqual([(n["start_tick"], n["end_tick"]) for n in notes], [(0, 480), (120, 600), (960, 1440)])
        self.assertEqual([n["sustain_end_ms"] for n in notes[:2]], [1000, 1000])
        self.assertEqual([n["program"] for n in notes], [3, 3, 5])
        self.assertEqual(source["tracks"][0]["key_signatures"], [{"tick": 0, "accidentals": -2, "minor": True}])
        self.assertEqual(source["tracks"][0]["time_signatures"][0]["denominator"], 4)
        self.assertEqual(result["extraction"]["original_notes_retained"], 3)
        self.assertIn("sustain_preserved_in_source_key_durations_used_for_singing", result["extraction"]["warnings"])

    def test_percussion_out_of_range_and_reused_channels_remain_only_in_source(self):
        lead = track(note(72, channel=0) + note(20, 0, 480, channel=0) + note(38, channel=9), name="Vocal")
        harmony = track(note(67, channel=0), name="Harmony")
        result = self.extract(midi(lead, harmony))
        arrangement = result["arrangement"]
        self.assertEqual(arrangement["source"]["note_count"], 4)
        self.assertEqual(len(arrangement["source"]["streams"]), 3)
        self.assertEqual([p["track_index"] for p in arrangement["parts"]], [0, 1])
        self.assertTrue(all(p["channel"] != 9 for p in arrangement["parts"]))
        self.assertEqual({n["midi_note"] for p in arrangement["parts"] for n in p["notes"]}, {72, 67})

    def test_global_tempo_map_keeps_chords_aligned_across_tempo_change(self):
        conductor = track([(0, meta(81, (500_000).to_bytes(3, "big"))),
                           (480, meta(81, (1_000_000).to_bytes(3, "big")))])
        events = note(72, 0, 960) + note(64, 0, 960)
        result = self.extract(midi(conductor, track(events, name="Vocal")))
        self.assertEqual(result["arrangement"]["duration_ms"], 1500)
        self.assertTrue(all(p["notes"][0]["duration_ms"] == 1500 for p in result["arrangement"]["parts"]))
        self.assertEqual(result["arrangement"]["source"]["tempo_map"][1]["start_ms"], 500)
        self.assertFalse(result["extraction"]["tempo_changed"])

    def test_explicit_selection_keeps_original_other_tracks_in_provenance(self):
        result = self.extract(midi(track(note(60), name="Vocal"), track(note(67), name="Harmony")), track=1)
        self.assertEqual(len(result["arrangement"]["parts"]), 1)
        self.assertEqual(result["arrangement"]["parts"][0]["track_index"], 1)
        self.assertEqual(result["extraction"]["source_note_count"], 2)

    def test_legacy_rolling_preview_contract_does_not_change(self):
        original = Path("/workspace/recovery/midi/wowaka - Rolling Girl.mid")
        if not original.exists():
            original = ROOT / "examples/rolling-girl/wowaka - Rolling Girl.mid"
        if not original.exists():
            self.skipTest("Provided simplified MIDI is not installed in this checkout")
        legacy = module.extract_midi(original)
        self.assertEqual(len(legacy["score"]["notes"]), 72)
        self.assertEqual(legacy["score"]["duration_ms"], 13_413)
        self.assertEqual(legacy["extraction"]["score_sha256"], "cccd93fb0087f7cb19ee003368ef1dbceb8b4921571fb51ab9358dd252ec2a86")

    def test_real_original_is_complete_deterministic_and_worker_requests_cover_all_selected_notes(self):
        original = Path("/workspace/recovery/midi/108297-original.mid")
        if not original.exists():
            original = ROOT / "examples/polyphonic/wowaka - Rolling Girl original.mid"
        if not original.exists():
            self.skipTest("Provided original MIDI is not installed in this checkout")
        before = original.read_bytes()
        result = module.extract_arrangement(original)
        arrangement = result["arrangement"]
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(arrangement["duration_ms"], 188_663)
        self.assertEqual(arrangement["source"]["note_count"], 2880)
        self.assertEqual(sum(len(s["notes"]) for s in arrangement["source"]["streams"]), 2880)
        self.assertEqual(arrangement["source"]["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(arrangement_sha256(arrangement), result["extraction"]["arrangement_sha256"])
        self.assertEqual(arrangement, module.extract_arrangement(original)["arrangement"])
        self.assertEqual([p["role"] for p in arrangement["parts"]], ["lead", "harmony", "bass"])
        self.assertEqual([len(p["notes"]) for p in arrangement["parts"]], [680, 447, 697])
        preview = preview_arrangement(arrangement)
        self.assertLessEqual(preview["arrangement"]["duration_ms"], 60_000)
        self.assertTrue(all(len(p["notes"]) <= 128 for p in preview["arrangement"]["parts"]))
        plan = arrangement_render_plan(arrangement)
        self.assertEqual(plan["duration_ms"], 188_663)
        self.assertEqual(plan["warnings"], [])
        self.assertEqual(sum(len(p["score"]["notes"]) for c in plan["chunks"] for p in c["parts"]), 1824)
        by_part = {p["id"]: [] for p in arrangement["parts"]}
        for chunk in plan["chunks"]:
            for part in chunk["parts"]:
                for n in part["score"]["notes"]:
                    by_part[part["id"]].append(dict(n, start_ms=n["start_ms"] + chunk["start_ms"]))
        for part in arrangement["parts"]:
            self.assertEqual([(n["midi_note"], n["start_ms"], n["duration_ms"]) for n in part["notes"]],
                             [(n["midi_note"], n["start_ms"], n["duration_ms"]) for n in by_part[part["id"]]])


if __name__ == "__main__":
    unittest.main()
