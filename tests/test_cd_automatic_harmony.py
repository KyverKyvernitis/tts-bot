"""Generated accompaniment preserves the original song and its provenance."""
from copy import deepcopy
import unittest

from osaka_cd.arrangement import arrangement_performance_sha256, validate_arrangement
from osaka_cd.harmony import HARMONY_POLICY, HARMONY_PHRASE_MS, harmonize_monophonic


def mono(pitches=(60, 62, 64, 65, 67, 69, 71, 72), *, signature=None):
    notes = [{"midi_note": pitch, "start_ms": number * 600,
              "duration_ms": 400, "velocity": 70 + number % 40, "lyric": "la"}
             for number, pitch in enumerate(pitches)]
    source = {"sha256": "a" * 64, "streams": [], "note_count": 0}
    if signature is not None:
        source["tracks"] = [{"key_signatures": [signature]}]
    return {"score_schema": 2, "duration_ms": len(notes) * 600,
            "parts": [{"id": "lead", "role": "lead", "voice_id": "hatsune-miku",
                       "lyrics_source": "message", "notes": notes}],
            "source": source, "selection": {"generated_harmonies": False}}


class AutomaticHarmonyTests(unittest.TestCase):
    def test_lead_rests_clock_and_source_remain_unchanged(self):
        original = mono()
        before = deepcopy(original)
        result = harmonize_monophonic(original)
        self.assertEqual(original, before)
        self.assertEqual(result["parts"][0], original["parts"][0])
        self.assertEqual(result["source"], original["source"])
        self.assertEqual(result["duration_ms"], original["duration_ms"])
        harmony = result["parts"][1]
        self.assertEqual(harmony["voice_id"], "hatsune-miku")
        self.assertEqual(harmony["lyrics_source"], "midi")
        self.assertNotIn("source_note_ids", harmony)
        for lead, other in zip(original["parts"][0]["notes"], harmony["notes"]):
            for field in ("start_ms", "duration_ms", "velocity"):
                self.assertEqual(lead[field], other[field])
            self.assertIn(lead["midi_note"] - other["midi_note"], (3, 4))
            self.assertIn(other["lyric"], {"a", "~"})
        self.assertTrue(result["selection"]["generated_harmonies"])
        self.assertTrue(result["automatic_harmony"]["generated"])
        self.assertEqual(result["automatic_harmony"]["policy"], HARMONY_POLICY)
        self.assertEqual(result["automatic_harmony"]["phrase_target_ms"], HARMONY_PHRASE_MS)

    def test_declared_major_scale_uses_thirds_not_one_fixed_transposition(self):
        value = mono(signature={"tick": 0, "accidentals": 0, "minor": False})
        result = harmonize_monophonic(value)
        self.assertEqual([note["midi_note"] for note in result["parts"][1]["notes"]],
                         [57, 59, 60, 62, 64, 65, 67, 69])
        self.assertEqual(result["automatic_harmony"]["key_source"], "midi_key_signature")

    def test_declared_minor_key_keeps_minor_scale(self):
        value = mono((69, 71, 72, 74, 76, 77, 79, 81),
                     signature={"tick": 0, "accidentals": 0, "minor": True})
        result = harmonize_monophonic(value)
        self.assertEqual([note["midi_note"] for note in result["parts"][1]["notes"]],
                         [65, 67, 69, 71, 72, 74, 76, 77])
        self.assertEqual(result["automatic_harmony"]["mode"], "minor")
        self.assertEqual(result["automatic_harmony"]["tonic_midi_class"], 9)

    def test_long_tonic_chord_infers_major_and_minor(self):
        for pitches, tonic, mode in (((60, 64, 67, 60, 60), 0, "major"),
                                     ((69, 72, 76, 69, 69), 9, "minor")):
            with self.subTest(mode=mode):
                result = harmonize_monophonic(mono(pitches))
                self.assertEqual(result["automatic_harmony"]["tonic_midi_class"], tonic)
                self.assertEqual(result["automatic_harmony"]["mode"], mode)

    def test_chromatic_notes_and_both_worker_pitch_edges_are_consonant(self):
        value = mono(tuple(range(36, 97)),
                     signature={"tick": 0, "accidentals": 0, "minor": False})
        result = harmonize_monophonic(value)
        for lead, harmony in zip(value["parts"][0]["notes"], result["parts"][1]["notes"]):
            self.assertTrue(36 <= harmony["midi_note"] <= 96)
            self.assertIn(abs(harmony["midi_note"] - lead["midi_note"]), (3, 4))

    def test_modulating_or_untrusted_key_metadata_uses_inference(self):
        for signatures in ([{"tick": 200, "accidentals": 0, "minor": False}],
                           [{"tick": 0, "accidentals": True, "minor": False}],
                           [{"tick": 0, "accidentals": 0, "minor": False},
                            {"tick": 1000, "accidentals": 2, "minor": False}]):
            value = mono()
            value["source"]["tracks"] = [{"key_signatures": signatures}]
            self.assertEqual(harmonize_monophonic(value)["automatic_harmony"]["key_source"],
                             "duration_profile")

    def test_authored_multipart_passes_through_and_repeated_conversion_is_idempotent(self):
        source = mono()
        source["parts"].append({"id": "real-harmony", "role": "harmony", "notes": [
            {"midi_note": 55, "start_ms": 0, "duration_ms": 400, "velocity": 80}]})
        result = harmonize_monophonic(source)
        self.assertEqual(result, source)
        self.assertIsNot(result, source)
        self.assertNotIn("automatic_harmony", result)
        converted = harmonize_monophonic(mono())
        self.assertEqual(harmonize_monophonic(converted), converted)
        self.assertNotEqual(arrangement_performance_sha256(mono()),
                            arrangement_performance_sha256(converted))

    def test_generated_snapshot_does_not_alias_the_input(self):
        value = mono()
        converted = harmonize_monophonic(value)
        converted["parts"][0]["notes"][0]["midi_note"] = 40
        converted["source"]["sha256"] = "b" * 64
        self.assertEqual(value["parts"][0]["notes"][0]["midi_note"], 60)
        self.assertEqual(value["source"]["sha256"], "a" * 64)

    def test_dense_source_keeps_every_lead_note_and_caps_total_notes(self):
        value = mono((60,))
        value["duration_ms"] = 600000
        value["parts"][0]["notes"] = [
            {"midi_note": 60 + number % 8, "start_ms": number * 40,
             "duration_ms": 40, "velocity": 90} for number in range(15000)]
        result = harmonize_monophonic(value)
        self.assertEqual(result["parts"][0], value["parts"][0])
        self.assertEqual(len(result["parts"][1]["notes"]), 9000)
        self.assertEqual(result["automatic_harmony"]["omitted_notes"], 6000)
        self.assertEqual(validate_arrangement(result), result)

    def test_custom_lead_identity_does_not_collide_with_generated_voice(self):
        value = mono()
        value["parts"][0]["id"] = "auto-harmony"
        result = harmonize_monophonic(value)
        self.assertEqual(result["parts"][1]["id"], "auto-harmony-2")

    def test_short_generated_phrases_fit_first_progressive_window_without_retiming(self):
        from osaka_cd.lyrics import accompaniment_render_chunks
        value = mono(tuple(60 + number % 8 for number in range(32)))
        value["duration_ms"] = 8000
        for number, note in enumerate(value["parts"][0]["notes"]):
            note.update(start_ms=number * 250, duration_ms=250)
        result = harmonize_monophonic(value)
        guide = result["parts"][1]["notes"]
        self.assertEqual([note["lyric"] for note in guide[:8]],
                         ["a", "~", "~", "~", "a", "~", "~", "~"])
        chunks = accompaniment_render_chunks(result, first_request_ms=1000, request_ms=8000)
        self.assertEqual(chunks[0]["duration_ms"], 1000)
        self.assertEqual(result["parts"][0], value["parts"][0])
        notes = [note for chunk in chunks for note in chunk["parts"][0]["score"]["notes"]]
        self.assertEqual(len(notes), len(guide))

    def test_long_single_note_keeps_its_timing_and_restarts_next_note(self):
        from osaka_cd.lyrics import accompaniment_render_chunks
        value = mono((60, 62, 64))
        value["duration_ms"] = 2500
        for note, start, duration in zip(value["parts"][0]["notes"], (0, 1500, 2000), (1500, 500, 500)):
            note.update(start_ms=start, duration_ms=duration)
        result = harmonize_monophonic(value)
        guide = result["parts"][1]["notes"]
        self.assertEqual([note["lyric"] for note in guide], ["a", "a", "~"])
        chunks = accompaniment_render_chunks(result, first_request_ms=1000, request_ms=8000)
        self.assertEqual(chunks[0]["duration_ms"], 1500)
        self.assertEqual(guide[0]["duration_ms"], 1500)

    def test_rests_restart_even_inside_one_second(self):
        value = mono((60, 62))
        result = harmonize_monophonic(value)
        self.assertEqual([note["lyric"] for note in result["parts"][1]["notes"]], ["a", "a"])


if __name__ == "__main__":
    unittest.main()
