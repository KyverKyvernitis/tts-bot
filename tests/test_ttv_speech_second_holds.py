"""One-second speech holds through the real planner, resampler and PCM mixer."""
from __future__ import annotations

import array
import copy
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/termux/phone-worker"))
from teto_renderer.errors import TetoSynthesisError
from teto_renderer.prosody import speech_effect_segments, validate_performance
from teto_renderer.renderer import TetoRenderer


def segment(text, **effects):
    return {"text": text, "fade_in": False, "fade_out": False,
            "hold": False, "pause_after_ms": 0, **effects}


def pcm(raw):
    with wave.open(io.BytesIO(raw), "rb") as audio:
        return array.array("h", audio.readframes(audio.getnframes())), audio.getframerate()


class SpeechHoldPlanningTests(unittest.TestCase):
    def test_repeated_holds_preserve_pause_fade_and_request_identity(self):
        source = [segment("a", hold=True, fade_out=True), segment("~", hold=True),
                  segment("~", hold=True, pause_after_ms=180), segment("b")]
        original = copy.deepcopy(source)
        planned = speech_effect_segments(source)
        self.assertEqual(source, original)
        self.assertEqual([item["text"] for item in planned], ["a", "b"])
        self.assertEqual(planned[0]["hold_count"], 3)
        self.assertEqual(planned[0]["pause_after_ms"], 180)
        self.assertTrue(planned[0]["fade_out"])
        self.assertEqual(planned[1]["hold_count"], 0)
        validate_performance("a ~ ~ b", segments=source)

    def test_neutral_literal_symbols_do_not_add_holds_or_create_boundaries(self):
        source = [segment("a"), segment("~"), segment("|"), segment("~", hold=True)]
        planned = speech_effect_segments(source)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["hold_count"], 1)

    def test_holds_require_previous_syllable_without_crossing_explicit_pause(self):
        for text, source in (
            ("~ a", [segment("~", hold=True), segment("a")]),
            ("a ~", [segment("a", pause_after_ms=180), segment("~", hold=True)]),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "depois de uma sílaba"):
                validate_performance(text, segments=source)


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required by the real renderer")
class SpeechHoldPCMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="ttv-second-holds-")
        cls.root = Path(cls.temporary.name)
        bank = cls.root / "bank"
        bank.mkdir()
        with wave.open(str(bank / "vowel.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(44100)
            audio.writeframes(array.array("h", [1000] * 44100).tobytes())
        aliases = ["- a", "a", "A", "a a", "- A", "- V", "V", "V V", "a V"]
        (bank / "oto.ini").write_text("\n".join(
            f"vowel.wav={alias},0,30,-600,20,10" for alias in aliases))
        executable = cls.root / "resampler.py"
        executable.write_text(
            "import array,sys,wave\n"
            "with wave.open(sys.argv[2],'wb') as out:\n"
            " out.setnchannels(1);out.setsampwidth(2);out.setframerate(44100)\n"
            " out.writeframes(array.array('h',[1000]*round(float(sys.argv[7])*44.1)).tobytes())\n")
        cls.renderers = {}
        for voice in ("kasane-teto", "hatsune-miku"):
            environment = {
                "PHONE_WORKER_TETO_ENABLED": "true",
                "PHONE_WORKER_TETO_VOICEBANK_DIR": str(bank),
                "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(bank),
                "PHONE_WORKER_TETO_VOICEBANK_MODE": "english",
                "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "1",
                "PHONE_WORKER_TETO_MIN_ALIASES": "1",
                "PHONE_WORKER_TETO_RESAMPLER_COMMAND": f"{sys.executable} {executable}",
                "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(cls.root / (voice + "-cache")),
                "PHONE_WORKER_TETO_LENGTH_MODE": "total",
            }
            cls.renderers[voice] = TetoRenderer(environment=environment, voice_id=voice)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def render(self, source, *, rate=1.0, voice="kasane-teto"):
        text = " ".join(item["text"] for item in source)
        return self.renderers[voice].synthesize_performance(text, segments=source, speech_rate=rate)

    def test_each_hold_adds_one_second_of_non_silent_pcm(self):
        plain, _ = self.render([segment("a")])
        before, rate = pcm(plain)
        for count in (1, 3):
            source = [segment("a", hold=True)] + [segment("~", hold=True)] * (count - 1)
            held, _ = self.render(source)
            after, _ = pcm(held)
            self.assertLessEqual(abs(len(after) - len(before) - count * rate), 1)
            self.assertTrue(any(after[len(before):]))
        repeated, metadata = self.render([segment("a", hold=True)])
        self.assertGreaterEqual(metadata["fragment_cache_hits"], 1)
        self.assertEqual(metadata["resampler_calls"], 0)

    def test_hold_seconds_do_not_follow_speech_rate(self):
        for speed in (0.5, 1.5):
            with self.subTest(speed=speed):
                plain, _ = self.render([segment("a")], rate=speed)
                held, _ = self.render([segment("a", hold=True)], rate=speed)
                before, rate = pcm(plain)
                after, _ = pcm(held)
                self.assertLessEqual(abs(len(after) - len(before) - rate), 1)

    def test_miku_uses_the_same_second_based_hold(self):
        plain, _ = self.render([segment("a")], voice="hatsune-miku")
        held, metadata = self.render([segment("a"), segment("~", hold=True)], voice="hatsune-miku")
        before, rate = pcm(plain)
        after, _ = pcm(held)
        self.assertLessEqual(abs(len(after) - len(before) - rate), 1)
        self.assertEqual(metadata["voice_id"], "hatsune-miku")

    def test_trailing_punctuation_keeps_its_pause_after_the_full_hold(self):
        plain, _ = self.render([segment("a!")])
        held, _ = self.render([segment("a!", hold=True, fade_out=True)])
        before, rate = pcm(plain)
        after, _ = pcm(held)
        self.assertLessEqual(abs(len(after) - len(before) - rate), 1)
        self.assertTrue(any(after[round(rate * .8):round(rate * 1.0)]))

    def test_pause_after_last_continuation_remains_silent(self):
        raw, _ = self.render([segment("a"), segment("~", hold=True, pause_after_ms=180),
                              segment("|"), segment("a")])
        samples, rate = pcm(raw)
        longest = current = 0
        for sample in samples:
            current = current + 1 if sample == 0 else 0
            longest = max(longest, current)
        self.assertGreaterEqual(longest, round(rate * .179))

    def test_internal_midi_continuation_keeps_its_original_note_duration(self):
        score = {"version": 1, "track_index": 0, "duration_ms": 800, "lyrics_source": "message",
                 "notes": [{"midi_note": pitch, "start_ms": i * 400,
                            "duration_ms": 400, "velocity": 100} for i, pitch in enumerate((60, 64))]}
        raw, metadata = self.renderers["kasane-teto"].synthesize_performance("a ~", score=score)
        samples, rate = pcm(raw)
        self.assertEqual(len(samples), round(rate * (800 + metadata["midi_preroll_ms"]) / 1000))
        self.assertEqual(metadata["melisma_notes"], 1)

    def test_repeated_holds_still_respect_the_existing_speech_audio_budget(self):
        with self.assertRaisesRegex(TetoSynthesisError, "20 segundos"):
            self.render([segment("a", hold=True)] + [segment("~", hold=True)] * 19)


if __name__ == "__main__":
    unittest.main()
