"""Stored arrangement and real worker-request boundaries (stdlib runner)."""
from __future__ import annotations

import copy
import unittest

from pathlib import Path as _CDTestPath
import sys as _CDTestSys
_CDTestSys.path.insert(0, str(_CDTestPath(__file__).resolve().parents[1] / 'deploy/ttv-cd'))

from osaka_cd.arrangement import (
    ArrangementError, arrangement_performance_sha256, arrangement_render_plan,
    arrangement_sha256, minimum_syllables, preview_arrangement, validate_arrangement,
)


def note(pitch=60, start=0, duration=500, lyric="a"):
    return {"midi_note": pitch, "start_ms": start, "duration_ms": duration,
            "velocity": 100, "lyric": lyric}


def arrangement(parts=None, duration=4000):
    return {"score_schema": 2, "duration_ms": duration,
            "parts": parts or [{"id": "lead", "role": "lead", "voice_id": "kasane-teto",
                                "lyrics_source": "message", "track_index": 0,
                                "notes": [note(60, 0, 1000), note(64, 2000, 1000)]}],
            "source": {}, "warnings": []}


class ArrangementTests(unittest.TestCase):
    def test_canonical_snapshot_is_independent_and_hash_is_deterministic(self):
        original = arrangement()
        snapshot = validate_arrangement(original)
        self.assertEqual(arrangement_sha256(snapshot), arrangement_sha256(copy.deepcopy(original)))
        snapshot["parts"][0]["notes"][0]["midi_note"] = 66
        self.assertEqual(original["parts"][0]["notes"][0]["midi_note"], 60)
        self.assertNotEqual(arrangement_sha256(original), arrangement_sha256(snapshot))

    def test_provenance_change_does_not_invalidate_musical_identity(self):
        original = arrangement()
        changed = copy.deepcopy(original)
        changed["source"]["sha256"] = "a" * 64
        changed["warnings"] = ["source_metadata_changed"]
        self.assertNotEqual(arrangement_sha256(original), arrangement_sha256(changed))
        self.assertEqual(arrangement_performance_sha256(original), arrangement_performance_sha256(changed))
        changed["parts"][0]["notes"][0]["duration_ms"] = 999
        self.assertNotEqual(arrangement_performance_sha256(original), arrangement_performance_sha256(changed))

    def test_explicit_default_mix_gains_have_the_same_musical_identity(self):
        value = arrangement()
        explicit = copy.deepcopy(value)
        explicit["parts"][0]["gain"] = .85
        explicit["parts"][0]["pan"] = 0.0
        self.assertEqual(arrangement_performance_sha256(value), arrangement_performance_sha256(explicit))

    def test_rejects_overlap_boolean_nan_percussion_and_false_confirmation(self):
        mutations = [lambda a: a["parts"][0]["notes"][1].update(start_ms=500),
                     lambda a: a.update(duration_ms=True),
                     lambda a: a["parts"][0].update(gain=float("nan")),
                     lambda a: a["parts"][0].update(channel=9),
                     lambda a: a["parts"][0].update(confidence={"vocal_transcription_confirmed": True}),
                     lambda a: a["source"].update(streams=[{"notes": None}])]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                value = arrangement()
                mutation(value)
                with self.assertRaises(ArrangementError):
                    validate_arrangement(value)

    def test_common_preview_keeps_relative_offsets_in_all_parts(self):
        value = arrangement([
            {"id": "lead", "role": "lead", "notes": [note(72, 1000, 500), note(74, 3000, 500)]},
            {"id": "harmony-1", "role": "harmony", "notes": [note(67, 1200, 500), note(69, 3200, 500)]},
        ], duration=5000)
        preview = preview_arrangement(value, start_ms=1000, end_ms=4000)
        self.assertEqual((preview["start_ms"], preview["end_ms"]), (1000, 4000))
        self.assertEqual(preview["arrangement"]["parts"][0]["notes"][0]["start_ms"], 0)
        self.assertEqual(preview["arrangement"]["parts"][1]["notes"][0]["start_ms"], 200)
        self.assertEqual(value["parts"][1]["notes"][0]["start_ms"], 1200)

    def test_dense_harmony_shortens_one_preview_window_for_every_part(self):
        value = arrangement([
            {"id": "lead", "role": "lead", "notes": [note(72, i * 1000, 900) for i in range(20)]},
            {"id": "harmony-1", "role": "harmony", "notes": [note(67, i * 100, 80) for i in range(200)]},
        ], duration=20_000)
        preview = preview_arrangement(value, start_ms=0, end_ms=20_000)
        self.assertEqual(preview["end_ms"], 12_800)
        self.assertEqual(len(preview["arrangement"]["parts"][1]["notes"]), 128)
        self.assertTrue(all(len(p["notes"]) <= 128 for p in preview["arrangement"]["parts"]))

    def test_subminimum_preview_edge_is_disclosed_and_original_retained(self):
        value = arrangement([{"id": "lead", "role": "lead", "notes": [
            note(60, 0, 1000), note(62, 1020, 980)]}], duration=2000)
        preview = preview_arrangement(value, start_ms=0, end_ms=1030)
        self.assertTrue(preview["boundary_notes_clipped"])
        self.assertEqual(preview["arrangement"]["preview"]["boundary_notes_omitted"], 1)
        self.assertIn("preview_boundary_notes_omitted", preview["arrangement"]["warnings"])
        self.assertEqual(value["parts"][0]["notes"][1]["duration_ms"], 980)

    def test_full_song_requests_preserve_every_selected_note_and_original_time(self):
        value = arrangement([
            {"id": "lead", "role": "lead", "lyrics_source": "vowel",
             "notes": [note(72 + i % 5, i * 500, 500) for i in range(400)]},
            {"id": "harmony-1", "role": "harmony", "lyrics_source": "vowel",
             "notes": [note(60 + i % 3, i * 1000 + 125, 700) for i in range(200)]},
        ], duration=200_000)
        plan = arrangement_render_plan(value)
        coverage = {p["id"]: {} for p in value["parts"]}
        for chunk in plan["chunks"]:
            self.assertLessEqual(chunk["duration_ms"], 18_000)
            for part in chunk["parts"]:
                score = part["score"]
                self.assertLessEqual(len(score["notes"]), 128)
                self.assertLessEqual(score["duration_ms"], 60_000)
                self.assertEqual(score["lyrics_source"], "midi")
                for index, note_value in zip(part["source_note_indices"], score["notes"]):
                    coverage[part["id"]].setdefault(index, []).append((chunk["start_ms"] + note_value["start_ms"],
                                                                      note_value["duration_ms"]))
        for part in value["parts"]:
            for index, original in enumerate(part["notes"]):
                pieces = coverage[part["id"]][index]
                self.assertEqual(pieces[0][0], original["start_ms"])
                self.assertEqual(sum(length for _, length in pieces), original["duration_ms"])
                for previous, current in zip(pieces, pieces[1:]):
                    self.assertEqual(previous[0] + previous[1], current[0])

    def test_guide_attacks_respect_pitch_and_rest_constraints_without_added_silence(self):
        value = arrangement([{"id": "lead", "role": "lead", "lyrics_source": "vowel",
                              "notes": [note(36, 0, 500), note(96, 500, 500), note(60, 1500, 500)]}], 2000)
        request = arrangement_render_plan(value)["chunks"][0]["parts"][0]
        self.assertEqual(request["minimum_syllables"], 3)
        self.assertEqual([n["lyric"] for n in request["score"]["notes"]], ["a", "a", "a"])
        self.assertEqual([n["start_ms"] for n in request["score"]["notes"]], [0, 500, 1500])

    def test_individual_long_note_fails_explicitly_and_retains_full_source(self):
        value = arrangement([{"id": "lead", "role": "lead", "notes": [note(60, 0, 20_000)]}], 20_000)
        self.assertEqual(validate_arrangement(value)["duration_ms"], 20_000)
        with self.assertRaises(ArrangementError) as raised:
            arrangement_render_plan(value)
        self.assertEqual(raised.exception.code, "note_exceeds_vowel_limit")
        self.assertEqual(value["parts"][0]["notes"][0]["duration_ms"], 20_000)

    def test_midi_hold_at_chunk_start_carries_original_syllable_and_discloses_attack(self):
        value = arrangement([{"id": "lead", "role": "lead", "lyrics_source": "midi",
                              "notes": [note(60 + i % 3, i * 500, 500, "mor" if i == 0 else "~")
                                        for i in range(40)]}], 20_000)
        plan = arrangement_render_plan(value)
        request = plan["chunks"][1]["parts"][0]
        self.assertEqual(request["carried_lyric"], "mor")
        self.assertTrue(request["lyrics_reattacked"])
        self.assertEqual(request["score"]["notes"][0]["lyric"], "mor")
        self.assertEqual(value["parts"][0]["notes"][36]["lyric"], "~")
        self.assertIn("chunk_boundary_midi_syllable_reattacked", plan["warnings"])

    def test_midi_hold_wide_pitch_restarts_known_syllable_with_warning(self):
        value = arrangement([{"id": "lead", "role": "lead", "lyrics_source": "midi",
                              "notes": [note(36, 0, 500, "mor"), note(96, 500, 500, "~")]}], 1000)
        plan = arrangement_render_plan(value)
        self.assertEqual([n["lyric"] for n in plan["chunks"][0]["parts"][0]["score"]["notes"]],
                         ["mor", "mor"])
        self.assertIn("midi_continuation_reattacked_for_worker_limit", plan["warnings"])
        self.assertEqual(value["parts"][0]["notes"][1]["lyric"], "~")

    def test_midi_rest_hold_retains_existing_worker_steady_vowel_behavior(self):
        value = arrangement([{"id": "lead", "role": "lead", "lyrics_source": "midi",
                              "notes": [note(36, 0, 500, "mor"), note(96, 1000, 500, "~")]}], 1500)
        plan = arrangement_render_plan(value)
        self.assertEqual(plan["chunks"][0]["parts"][0]["score"]["notes"][1]["lyric"], "~")
        self.assertEqual(plan["warnings"], [])

    def test_very_short_final_silence_is_retained_without_invalid_worker_request(self):
        value = arrangement([{"id": "lead", "role": "lead", "notes": [note(60, 0, 18_000)]}], 18_020)
        plan = arrangement_render_plan(value)
        self.assertEqual(plan["duration_ms"], 18_020)
        self.assertEqual(plan["chunks"][-1]["parts"], [])


if __name__ == "__main__":
    unittest.main()
