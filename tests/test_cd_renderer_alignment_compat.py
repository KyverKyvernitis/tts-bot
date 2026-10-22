"""TTV timing compatibility across worker upgrades, with real PCM and FFmpeg.

No network, installed voicebank, or physical-phone synthesis is claimed here.
"""
from __future__ import annotations

import array
import copy
import hashlib
import io
import math
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from osaka_cd.render import RenderError, _preroll, _result_audio, render_arrangement


RENDERER = "speech-4g-cvvc-pauses"
MIDI_TIMELINE = "midi-vowel-continuous"


def score(duration=1000):
    return {"version": 1, "track_index": 0, "duration_ms": duration,
            "lyrics_source": "message", "notes": [
                {"midi_note": 69, "start_ms": 0, "duration_ms": duration, "velocity": 100}]}


def tone(value, preroll=125, frequency=440, *, silent=False):
    rate = 48000
    frames = round((value["duration_ms"] + preroll) * rate / 1000)
    samples = array.array("h", [0]) * frames
    if not silent:
        for note in value["notes"]:
            first = max(0, round((preroll + note["start_ms"]) * rate / 1000))
            end = min(frames, round((preroll + note["start_ms"] + note["duration_ms"]) * rate / 1000))
            for number in range(first, end):
                samples[number] = round(10000 * math.sin(2 * math.pi * frequency * (number - first) / rate))
    if sys.byteorder != "little":
        samples.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.tobytes())
    return output.getvalue()


def current_metadata(**values):
    return {"worker_version": "1.11.42", "renderer_version": RENDERER,
            "teto_timeline_mode": MIDI_TIMELINE, "cache_hit": False, **values}


class RendererAlignmentCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.score = score()
        self.audio = tone(self.score)

    def test_current_worker_raw_midi_infers_exact_original_pcm_preroll(self):
        self.assertAlmostEqual(_preroll(self.audio, current_metadata(), self.score), 125)

    def test_current_worker_cached_raw_omits_timeline_but_keeps_renderer_identity(self):
        for missing in (None, ""):
            with self.subTest(timeline=missing):
                meta = current_metadata(cache_hit=True, teto_timeline_mode=missing)
                self.assertAlmostEqual(_preroll(self.audio, meta, self.score), 125)

    def test_current_worker_json_aliases_preserve_the_same_contract(self):
        meta = {"worker_version": "1.11.42", "teto_renderer_version": RENDERER,
                "timeline_mode": MIDI_TIMELINE, "cache_hit": False}
        self.assertAlmostEqual(_preroll(self.audio, meta, self.score), 125)

    def test_empty_raw_renderer_field_falls_back_to_json_renderer_alias(self):
        meta = current_metadata(renderer_version="", teto_renderer_version=RENDERER)
        self.assertAlmostEqual(_preroll(self.audio, meta, self.score), 125)

    def test_control_plane_version_does_not_change_the_known_renderer_contract(self):
        for version in ("1.11.43", "2.0.0", "next-control-plane"):
            with self.subTest(version=version):
                meta = current_metadata(worker_version=version)
                self.assertAlmostEqual(_preroll(self.audio, meta, self.score), 125)

    def test_original_11141_timeline_only_wire_remains_compatible(self):
        meta = {"worker_version": "1.11.41", "teto_timeline_mode": MIDI_TIMELINE}
        self.assertAlmostEqual(_preroll(self.audio, meta, self.score), 125)

    def test_new_worker_missing_or_unknown_renderer_never_guesses_alignment(self):
        for renderer in (None, "", "unrecognized-renderer", "speech-4g-cvvc-pauses-modified"):
            with self.subTest(renderer=renderer):
                meta = current_metadata(renderer_version=renderer)
                with self.assertRaisesRegex(RenderError, "stem_alignment_metadata_missing"):
                    _preroll(self.audio, meta, self.score)

    def test_uncached_renderer_requires_the_midi_timeline(self):
        for timeline in (None, "", "serial", "phrase-effects-continuous", "worldline-phrase-continuous"):
            with self.subTest(timeline=timeline):
                with self.assertRaisesRegex(RenderError, "stem_alignment_metadata_missing"):
                    _preroll(self.audio, current_metadata(teto_timeline_mode=timeline), self.score)

    def test_cached_response_cannot_override_an_explicit_non_midi_timeline(self):
        for timeline in ("serial", "phrase-effects-continuous", "unknown-timeline"):
            with self.subTest(timeline=timeline):
                meta = current_metadata(cache_hit=True, teto_timeline_mode=timeline)
                with self.assertRaisesRegex(RenderError, "stem_alignment_metadata_missing"):
                    _preroll(self.audio, meta, self.score)

    def test_missing_timeline_requires_a_real_cache_hit_boolean(self):
        for cache_hit in (False, "true", 1, None):
            with self.subTest(cache_hit=cache_hit):
                meta = current_metadata(cache_hit=cache_hit, teto_timeline_mode=None)
                with self.assertRaisesRegex(RenderError, "stem_alignment_metadata_missing"):
                    _preroll(self.audio, meta, self.score)

    def test_explicit_exact_timing_still_supports_other_renderers(self):
        meta = {"renderer_version": "other-renderer", "midi_preroll_ms": 125.0}
        self.assertAlmostEqual(_preroll(self.audio, meta, self.score), 125)

    def test_explicit_malformed_timing_is_not_replaced_by_inference(self):
        for supplied in (True, False, "125", [], {}):
            with self.subTest(preroll=supplied):
                with self.assertRaisesRegex(RenderError, "invalid_stem_preroll"):
                    _preroll(self.audio, current_metadata(midi_preroll_ms=supplied), self.score)

    def test_explicit_nonfinite_timing_is_rejected(self):
        for supplied in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(preroll=supplied):
                with self.assertRaisesRegex(RenderError, "invalid_stem_preroll"):
                    _preroll(self.audio, current_metadata(midi_preroll_ms=supplied), self.score)

    def test_declared_timing_must_match_actual_pcm_duration(self):
        with self.assertRaisesRegex(RenderError, "stem_duration_mismatch"):
            _preroll(self.audio, current_metadata(midi_preroll_ms=100), self.score)

    def test_inferred_timing_remains_bounded_to_supported_preroll(self):
        for supplied in (-10, 351, 1000):
            with self.subTest(preroll=supplied):
                audio = tone(self.score, preroll=supplied)
                with self.assertRaisesRegex(RenderError, "invalid_stem_preroll"):
                    _preroll(audio, current_metadata(), self.score)

    def test_truncated_pcm_is_rejected_before_timing_inference(self):
        with self.assertRaisesRegex(RenderError, "truncated_stem_audio"):
            _preroll(self.audio[:-2], current_metadata(), self.score)

    def test_malformed_wav_is_rejected_before_timing_inference(self):
        with self.assertRaisesRegex(RenderError, "invalid_stem_audio"):
            _preroll(b"not a WAV" * 20, current_metadata(), self.score)

    def test_silent_pcm_is_rejected_before_timing_inference(self):
        with self.assertRaisesRegex(RenderError, "silent_stem_audio"):
            _preroll(tone(self.score, silent=True), current_metadata(), self.score)

    def test_renderer_hash_is_verified_before_timing_compatibility_is_used(self):
        with self.assertRaisesRegex(RenderError, "renderer_audio_hash_mismatch"):
            _result_audio((self.audio, current_metadata(sha256="0" * 64)))

    def test_verified_wav_bytes_are_preserved_by_the_metadata_adapter(self):
        meta = current_metadata(sha256=hashlib.sha256(self.audio).hexdigest())
        audio, received = _result_audio((self.audio, meta))
        self.assertEqual(audio, self.audio)
        self.assertEqual(received, meta)
        self.assertAlmostEqual(_preroll(audio, received, self.score), 125)


@unittest.skipUnless(shutil.which("ffmpeg"), "native FFmpeg required")
class CurrentWorkerCompositionTests(unittest.TestCase):
    def test_current_raw_worker_parts_align_with_real_ffmpeg_and_reuse_verified_cache(self):
        arrangement = {"score_schema": 2, "duration_ms": 1000, "parts": [
            {"id": "lead", "role": "lead", "voice_id": "kasane-teto", "lyrics_source": "message",
             "notes": [{"midi_note": 69, "start_ms": 0, "duration_ms": 1000, "velocity": 100}]},
            {"id": "harmony", "role": "harmony", "voice_id": "kasane-teto", "lyrics_source": "vowel",
             "notes": [{"midi_note": 76, "start_ms": 0, "duration_ms": 1000, "velocity": 100}]}]}
        requests = []
        def render_part(**request):
            requests.append(request)
            lead = request["part_id"] == "lead"
            audio = tone(request["score"], preroll=100 if lead else 200,
                         frequency=440 if lead else 660)
            return audio, current_metadata(sha256=hashlib.sha256(audio).hexdigest())

        with tempfile.TemporaryDirectory(prefix="ttv-alignment-compat-") as directory:
            options = {"cache_dir": Path(directory), "render_profile_fingerprint": "verified-bank-renderer-identity"}
            first = render_arrangement(arrangement, "la", render_part, **options)
            self.assertEqual(len(requests), 2)
            self.assertEqual(first["metadata"]["midi_preroll_ms"], 200)
            with wave.open(io.BytesIO(first["audio"]), "rb") as output:
                self.assertEqual((output.getnchannels(), output.getsampwidth(), output.getframerate()), (2, 2, 48000))
                self.assertEqual(output.getnframes(), 57600)
                samples = array.array("h", output.readframes(output.getnframes()))
            if sys.byteorder != "little":
                samples.byteswap()
            self.assertFalse(any(samples[:9600 * 2]))
            self.assertTrue(any(samples[9600 * 2:10560 * 2]))
            self.assertEqual(first["sha256"], hashlib.sha256(first["audio"]).hexdigest())

            second = render_arrangement(arrangement, "la", render_part, **options)
            self.assertEqual(len(requests), 2)
            self.assertTrue(second["mix_cache_hit"])
            self.assertEqual(second["audio"], first["audio"])

            changed = copy.deepcopy(arrangement)
            changed["parts"][1]["gain"] = .2
            third = render_arrangement(changed, "la", render_part, **options)
            self.assertEqual(len(requests), 2)
            self.assertFalse(third["mix_cache_hit"])
            self.assertTrue(all(stem["cache_hit"] for stem in third["stems"]))
            self.assertNotEqual(third["sha256"], first["sha256"])
            self.assertEqual(third["metadata"]["midi_preroll_ms"], 200)


if __name__ == "__main__":
    unittest.main()
