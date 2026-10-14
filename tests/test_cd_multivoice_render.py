"""Real PCM/FFmpeg composition tests; no claim of a physical voicebank render."""
from pathlib import Path
import array
import copy
import hashlib
import io
import math
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pathlib import Path as _CDTestPath
import sys as _CDTestSys
_CDTestSys.path.insert(0, str(_CDTestPath(__file__).resolve().parents[1] / 'deploy/ttv-cd'))

from osaka_cd.render import render_arrangement, RenderError


def tone(score, frequency, preroll, rate=48000):
    frames = round((score["duration_ms"] + preroll) * rate / 1000)
    samples = array.array("h", [0]) * frames
    for note in score["notes"]:
        first = round((preroll + note["start_ms"]) * rate / 1000)
        end = round((preroll + note["start_ms"] + note["duration_ms"]) * rate / 1000)
        for number in range(first, min(end, frames)):
            samples[number] = round(10000 * math.sin(2 * math.pi * frequency * (number - first) / rate))
    if sys.byteorder != "little":
        samples.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setnchannels(1); destination.setsampwidth(2); destination.setframerate(rate)
        destination.writeframes(samples.tobytes())
    return output.getvalue()


def arrangement(duration=1000):
    return {"score_schema": 2, "duration_ms": duration, "parts": [
        {"id": "lead", "role": "lead", "voice_id": "kasane-teto", "lyrics_source": "message",
         "notes": [{"midi_note": 69, "start_ms": 0, "duration_ms": duration, "velocity": 100}]},
        {"id": "harm", "role": "harmony", "voice_id": "kasane-teto", "lyrics_source": "vowel",
         "notes": [{"midi_note": 76, "start_ms": 0, "duration_ms": duration, "velocity": 100}]}]}


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for real composition")
class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cd-composition-test-")
        self.cache = Path(self.temp.name)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def render(self, **request):
        self.calls.append(request)
        lead = request["part_id"] == "lead"
        preroll = 100 if lead else 200
        raw = tone(request["score"], 440 if lead else 660, preroll)
        return raw, {"midi_preroll_ms": preroll, "sha256": hashlib.sha256(raw).hexdigest()}

    def compose(self, value=None, **kwargs):
        return render_arrangement(value or arrangement(), "la", self.render,
            cache_dir=self.cache, render_profile_fingerprint="bank-renderer-real-identity", **kwargs)

    def test_distinct_frequencies_share_vowel_onset_despite_different_preroll(self):
        result = self.compose()
        with wave.open(io.BytesIO(result["audio"]), "rb") as output:
            self.assertEqual((output.getnchannels(), output.getframerate()), (2, 48000))
            self.assertEqual(output.getnframes(), 57600)
            samples = array.array("h", output.readframes(output.getnframes()))
        before = samples[:9600 * 2]
        self.assertFalse(any(before))
        left = samples[9600 * 2::2]
        def energy(frequency):
            count = 4800
            real = sum(left[i] * math.cos(2 * math.pi * frequency * i / 48000) for i in range(count))
            imag = sum(left[i] * math.sin(2 * math.pi * frequency * i / 48000) for i in range(count))
            return math.hypot(real, imag) / count
        self.assertGreater(energy(440), 1500)
        self.assertGreater(energy(660), 1000)
        self.assertLess(energy(550), 10)
        self.assertLess(max(map(abs, samples)), 32767)
        self.assertEqual(result["metadata"]["midi_preroll_ms"], 200)
        self.assertFalse(result["metadata"]["chunk_continuations_reattacked"])

    def test_cold_warm_mix_and_gain_change_reuse_independent_stems(self):
        first = self.compose()
        second = self.compose()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(second["mix_cache_hit"])
        self.assertEqual(first["sha256"], second["sha256"])
        changed = arrangement()
        changed["parts"][1]["gain"] = .2
        third = self.compose(changed)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(third["mix_cache_hit"])
        self.assertNotEqual(first["sha256"], third["sha256"])

    def test_changed_harmony_only_renders_its_stem(self):
        self.compose()
        changed = arrangement()
        changed["parts"][1]["notes"][0]["midi_note"] = 77
        self.compose(changed)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]["part_id"], "harm")

    def test_zero_gain_mutes_one_part_but_all_muted_fail_explicitly(self):
        value = arrangement()
        value["parts"][1]["gain"] = 0
        result = self.compose(value)
        self.assertGreater(len(result["audio"]), 44)
        value["parts"][0]["gain"] = 0
        with self.assertRaisesRegex(RenderError, "silent_composition_gain"):
            self.compose(value)

    def test_empty_message_wide_pitch_uses_explicit_guide_attacks(self):
        value = arrangement()
        value["parts"] = value["parts"][:1]
        value["parts"][0]["notes"] = [{"midi_note": pitch, "start_ms": start,
            "duration_ms": 500, "velocity": 100} for pitch, start in ((36, 0), (96, 500))]
        result = render_arrangement(value, "", self.render)
        self.assertGreater(len(result["audio"]), 44)
        self.assertEqual(self.calls[0]["score"]["lyrics_source"], "midi")
        self.assertEqual([n["lyric"] for n in self.calls[0]["score"]["notes"]], ["a", "a"])

    def test_explicit_harmony_message_is_sung_instead_of_silently_replaced(self):
        value = arrangement()
        value["parts"][1]["lyrics_source"] = "message"
        render_arrangement(value, "my words", self.render)
        self.assertEqual([call["text"] for call in self.calls], ["my words", "my words"])

    def test_optional_cache_write_failure_keeps_completed_audio(self):
        with patch("osaka_cd.render._atomic", side_effect=OSError("disk full")):
            result = self.compose()
        self.assertGreater(len(result["audio"]), 44)

    def test_effects_are_partitioned_once_and_modifiers_preserved(self):
        value = arrangement(36000)
        for part in value["parts"]:
            part["notes"] = [{"midi_note": 69, "start_ms": start, "duration_ms": 9000,
                              "velocity": 100} for start in range(0, 36000, 9000)]
        segments = [{"text": "one two three four", "fade_in": True, "fade_out": True,
                     "hold": True, "pause_after_ms": 180}]
        render_arrangement(value, "one two three four", self.render, segments=segments)
        leads = [call for call in self.calls if call["part_id"] == "lead"]
        self.assertEqual(" ".join(call["text"] for call in leads), "one two three four")
        self.assertTrue(leads[0]["segments"][0]["fade_in"])
        self.assertFalse(leads[1]["segments"][0]["fade_in"])
        self.assertTrue(leads[-1]["segments"][-1]["fade_out"])
        self.assertTrue(leads[-1]["segments"][-1]["hold"])
        self.assertEqual(leads[-1]["segments"][-1]["pause_after_ms"], 180)
        self.assertTrue(all("segments" not in call for call in self.calls if call["part_id"] != "lead"))
        sys.path.insert(0, str(ROOT / "deploy/termux/phone-worker"))
        try:
            from teto_renderer.prosody import validate_performance
        except ImportError:
            self.skipTest("Existing worker validator source is unavailable; PCM composition remains tested separately")
        for call in leads:
            validate_performance(call["text"], score=call["score"], segments=call["segments"])

    def test_effect_change_invalidates_only_message_stem_not_harmony_guide(self):
        segment = {"text": "la", "fade_in": False, "fade_out": False, "hold": False, "pause_after_ms": 0}
        self.compose(segments=[segment])
        self.compose(segments=[dict(segment, fade_in=True)])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]["part_id"], "lead")

    def test_standalone_hold_keeps_its_anchor_across_chunks(self):
        from osaka_cd.render import _message_allocation
        requests = [{"minimum_syllables": 1, "score": {"notes": [{}]}}] * 2
        plain = {"fade_in": False, "fade_out": False, "hold": False, "pause_after_ms": 0}
        segments = [dict(plain, text="one"), dict(plain, text="~", hold=True),
                    dict(plain, text="two")]
        result = _message_allocation("one ~ two", requests, segments)
        self.assertEqual([words for words, _ in result], ["one ~", "two"])
        with self.assertRaisesRegex(RenderError, "insufficient_text_for_chunks"):
            _message_allocation("one ~", requests, segments[:2])

    def test_segments_mismatch_fails_before_any_physical_request(self):
        segment = {"text": "other", "fade_in": False, "fade_out": False, "hold": False, "pause_after_ms": 0}
        with self.assertRaisesRegex(RenderError, "composition_segments_text_mismatch"):
            self.compose(segments=[segment])
        self.assertEqual(self.calls, [])

    def test_nonfinite_and_boolean_timeouts_fail_before_physical_request(self):
        for value in (True, float("nan"), float("inf"), 0):
            with self.subTest(timeout=value):
                with self.assertRaisesRegex(RenderError, "invalid_render_timeout"):
                    self.compose(timeout_seconds=value)
        self.assertEqual(self.calls, [])

    def test_changed_renderer_profile_invalidates_all_stems(self):
        self.compose()
        render_arrangement(arrangement(), "la", self.render, cache_dir=self.cache,
                           render_profile_fingerprint="different-voicebank")
        self.assertEqual(len(self.calls), 4)

    def test_unsafe_alignment_is_rejected_and_no_mix_is_cached(self):
        def missing(**request):
            return tone(request["score"], 440, 100), {}
        with self.assertRaisesRegex(RenderError, "stem_alignment_metadata_missing"):
            render_arrangement(arrangement(), "la", missing, cache_dir=self.cache,
                               render_profile_fingerprint="opaque-bank")
        self.assertFalse((self.cache / "mixes").exists())

    def test_raw_1_11_41_and_cached_raw_preroll_are_derived_from_exact_pcm_frames(self):
        def existing(**request):
            return tone(request["score"], 440, 125), {"worker_version": "1.11.41",
                "renderer_version": "speech-4g-cvvc-pauses", "cache_hit": True}
        result = render_arrangement(arrangement(), "la", existing)
        self.assertEqual(result["metadata"]["midi_preroll_ms"], 125)

    def test_cancel_after_first_physical_part_does_not_render_second(self):
        with self.assertRaisesRegex(RenderError, "render_cancelled"):
            self.compose(cancelled=lambda: bool(self.calls))
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.cache / "mixes").exists())

    def test_encoded_mp3_decodes_and_long_song_text_is_not_repeated(self):
        value = arrangement(36000)
        for part in value["parts"]:
            part["notes"] = [{"midi_note": 69, "start_ms": start, "duration_ms": 9000,
                              "velocity": 100} for start in range(0, 36000, 9000)]
        result = render_arrangement(value, "one two three four", self.render, output_format="mp3")
        leads = [call["text"] for call in self.calls if call["part_id"] == "lead"]
        self.assertEqual(" ".join(leads), "one two three four")
        self.assertEqual(len(leads), 2)
        target = self.cache / "long.mp3"
        target.write_bytes(result["audio"])
        import subprocess
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(target),
                                  "-f", "null", "-"], capture_output=True, timeout=30)
        self.assertEqual(decoded.returncode, 0)
        self.assertEqual(result["metadata"]["chunks"], 2)

    def test_all_requests_obey_unchanged_worker_score_validator(self):
        self.compose()
        sys.path.insert(0, str(ROOT / "deploy/termux/phone-worker"))
        try:
            from teto_renderer.prosody import validate_performance
        except ImportError:
            self.skipTest("Existing worker validator source is unavailable; PCM composition remains tested separately")
        for request in self.calls:
            validate_performance(request["text"], score=request["score"])

    def test_three_parts_full_song_exceeding_both_legacy_limits_preserves_clock(self):
        value = arrangement(75000)
        value["parts"].append({"id": "bass", "role": "bass", "voice_id": "kasane-teto",
            "lyrics_source": "vowel", "notes": []})
        for index, part in enumerate(value["parts"]):
            part["notes"] = [{"midi_note": 60 + index * 7, "start_ms": start,
                              "duration_ms": 250, "velocity": 100}
                             for start in range(0, 75000, 250)]
        result = render_arrangement(value, "", self.render, cache_dir=self.cache,
                                   render_profile_fingerprint="physical-bank-render-settings")
        with wave.open(io.BytesIO(result["audio"]), "rb") as output:
            self.assertEqual(output.getnframes(), round(75200 * 48))
        self.assertEqual(result["metadata"]["parts"], 3)
        self.assertGreater(result["metadata"]["chunks"], 1)
        self.assertTrue(all(call["score"]["duration_ms"] <= 60000
                            and len(call["score"]["notes"]) <= 128 for call in self.calls))


if __name__ == "__main__":
    unittest.main()
