from __future__ import annotations

import os
import io
import array
import stat
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer import TetoRenderer
from teto_renderer.errors import TetoResourceError
from phone_worker_runtime import tts_policy


def _write_wav(path: Path, *, frames: int = 2205, rate: int = 22050, sample: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(array.array("h", [sample] * frames).tobytes())


class TetoRendererTests(unittest.TestCase):
    def _assets(self, root: Path) -> tuple[Path, Path]:
        voicebank = root / "voicebank"
        voicebank.mkdir()
        _write_wav(voicebank / "te.wav")
        _write_wav(voicebank / "to.wav")
        (voicebank / "oto.ini").write_text(
            "te.wav=て,0,20,0,0,0\n"
            "to.wav=と,0,20,0,0,0\n",
            encoding="utf-8",
        )
        (voicebank / "character.txt").write_text("name=Kasane Teto Test\n", encoding="utf-8")

        resampler = root / "fake_resampler.py"
        resampler.write_text(
            "#!/usr/bin/env python3\n"
            "import shutil, sys\n"
            "shutil.copyfile(sys.argv[1], sys.argv[2])\n",
            encoding="utf-8",
        )
        resampler.chmod(resampler.stat().st_mode | stat.S_IXUSR)
        return voicebank, resampler

    def _env(self, voicebank: Path, resampler: Path, cache: Path) -> dict[str, str]:
        return {
            "PHONE_WORKER_TETO_ENABLED": "true",
            "PHONE_WORKER_TETO_VOICEBANK_DIR": str(voicebank),
            "PHONE_WORKER_TETO_RESAMPLER_COMMAND": str(resampler),
            "PHONE_WORKER_TETO_MIN_ALIASES": "1",
            "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(cache),
            "PHONE_WORKER_TETO_MAX_CHARACTERS": "180",
            "PHONE_WORKER_TETO_MAX_PHONEMES": "32",
            "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "5",
            "PHONE_WORKER_TETO_SPEECH_RATE": "1.0",
            "PHONE_WORKER_TETO_MODULATION": "15",
            "PHONE_WORKER_TETO_VELOCITY": "100",
            "PHONE_WORKER_TETO_LENGTH_MODE": "auto",
            "PHONE_WORKER_TETO_RENDER_THREADS": "2",
        }

    def test_status_and_render_with_external_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            voicebank, resampler = self._assets(root)
            with patch.dict(os.environ, self._env(voicebank, resampler, root / "cache"), clear=False):
                renderer = TetoRenderer(resource_guard=lambda: {"ok": True})
                status = renderer.status(force=True)
                self.assertTrue(status["ready"])
                self.assertEqual(status["aliases"], 2)

                result = renderer.synthesize("teto", timeout_seconds=10)

                self.assertEqual(result["audio_format"], "wav")
                self.assertEqual(result["voicebank"], "Kasane Teto Test")
                self.assertGreater(result["rendered_phonemes"], 0)
                self.assertTrue(bytes(result["audio"]).startswith(b"RIFF"))
                self.assertLessEqual(len(result["audio"]), 8 * 1024 * 1024)

    def test_voicebank_keeps_xsampa_case_distinctions(self):
        from teto_renderer.voicebank import VoicebankIndex

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_wav(root / "upper.wav")
            _write_wav(root / "lower.wav")
            (root / "oto.ini").write_text(
                "upper.wav=E,0,20,0,0,0\n"
                "lower.wav=e,0,20,0,0,0\n",
                encoding="utf-8",
            )
            index = VoicebankIndex.load(root, minimum_aliases=1)
            self.assertEqual(index.resolve(("E",)).alias, "E")
            self.assertEqual(index.resolve(("e",)).alias, "e")

    def test_auto_prefers_english_cvvc_bank_and_reports_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            standard, resampler = self._assets(root)
            english = root / "english"
            english.mkdir()
            _write_wav(english / "start.wav", rate=44100, sample=800)
            _write_wav(english / "vcv.wav", rate=44100, sample=-800)
            (english / "oto.ini").write_text(
                "start.wav=- te,0,20,0,15,8\n"
                "vcv.wav=e tu,0,20,0,15,8\n",
                encoding="utf-8",
            )
            (english / "nested").mkdir()
            (english / "nested" / "character.txt").write_text("name=Kasane Teto English Test\n", encoding="utf-8")
            env = self._env(standard, resampler, root / "cache")
            env.update({
                "PHONE_WORKER_TETO_VOICEBANK_MODE": "auto",
                "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(english),
                "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "1",
            })
            with patch.dict(os.environ, env, clear=False):
                renderer = TetoRenderer(resource_guard=lambda: {"ok": True})
                status = renderer.status(force=True)
                self.assertTrue(status["ready"])
                self.assertEqual(status["voicebank_profile"], "english-cvvc")
                self.assertEqual(status["voice"], "kasane-teto-english-cvvc")
                self.assertEqual(status["name"], "Kasane Teto English Test")
                result = renderer.synthesize("teto", timeout_seconds=10)
            self.assertEqual(result["voicebank_profile"], "english-cvvc")
            self.assertEqual(result["renderer_version"], "speech-4d-texttoteto-pitch")
            self.assertEqual(result["missing_phonemes"], [])
            self.assertGreaterEqual(result["cvvc_direct"], 2)
            self.assertGreater(result["coverage_percent"], 90.0)
            self.assertEqual(result["epenthetic_phonemes"], 0)
            self.assertEqual(result["timeline_mode"], "oto-continuous")

    def test_english_cvvc_timeline_uses_oto_preutterance_instead_of_serial_aux_notes(self):
        from teto_renderer.prosody import RenderNote
        from teto_renderer.voicebank import OtoEntry

        renderer = TetoRenderer()
        renderer._index_profile = "english-cvvc"
        notes = [
            RenderNote(("- br",), "C4", 46, 0, role="cluster", coverage="cluster-hit"),
            RenderNote(("ra",), "C4", 120, 0, role="nucleus", coverage="cvvc-direct"),
            RenderNote(("i w",), "C4", 50, 0, role="glide", coverage="cvvc-transition", word_end=True),
        ]
        entries = [
            OtoEntry("- br", Path("cluster.wav"), 0, 32, 0, 45, 18),
            OtoEntry("ra", Path("ra.wav"), 0, 38, 0, 70, 18),
            OtoEntry("i w", Path("iw.wav"), 0, 26, 0, 45, 18),
        ]
        placements, _ = renderer._english_timeline_placements(notes, entries)

        # The consonant cluster starts first and converges on the lexical /ra/
        # boundary instead of consuming a standalone 46 ms block.
        self.assertLess(placements[0]["start_ms"], placements[1]["start_ms"])
        self.assertLess(placements[0]["anchor_ms"], placements[1]["anchor_ms"])
        # Both the lexical VCV and final glide include their OTO preutterance
        # before the anchor, which is the core UTAU/CVVC timing invariant.
        self.assertAlmostEqual(placements[1]["anchor_ms"] - placements[1]["start_ms"], 70.0)
        self.assertAlmostEqual(placements[2]["anchor_ms"] - placements[2]["start_ms"], 45.0)
        # The coda anchor follows one lexical nucleus duration, not cluster +
        # nucleus + coda serialized durations.
        self.assertAlmostEqual(placements[2]["anchor_ms"] - placements[1]["anchor_ms"], 120.0)

    def test_english_cvvc_resampler_budget_includes_preutterance(self):
        from teto_renderer.prosody import RenderNote
        from teto_renderer.voicebank import OtoEntry

        renderer = TetoRenderer()
        note = RenderNote(("a zi",), "C4", 120, 0, role="nucleus", coverage="cvvc-direct")
        entry = OtoEntry("a zi", Path("azi.wav"), 0, 40, 0, 75, 20)
        with patch.dict(os.environ, {"PHONE_WORKER_TETO_LENGTH_MODE": "total"}, clear=False):
            renderer._index_profile = "english-cvvc"
            self.assertEqual(renderer._resampler_length(entry, note), 195.0)
            renderer._index_profile = "standard"
            self.assertEqual(renderer._resampler_length(entry, note), 120.0)

    def test_english_cvvc_realistic_fragments_overlap_without_hard_internal_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            standard, _ = self._assets(root)
            english = root / "english"
            english.mkdir()
            for name, sample in (("cluster.wav", 500), ("ra.wav", 900), ("azi.wav", -700), ("iw.wav", 450)):
                _write_wav(english / name, rate=44100, sample=sample)
            (english / "oto.ini").write_text(
                "cluster.wav=- br,0,32,0,45,18\n"
                "ra.wav=ra,0,38,0,70,18\n"
                "azi.wav=a zi,0,36,0,70,18\n"
                "iw.wav=i w,0,26,0,45,18\n",
                encoding="utf-8",
            )
            resampler = root / "duration_resampler.py"
            resampler.write_text(
                "#!/usr/bin/env python3\n"
                "import array, sys, wave\n"
                "name=sys.argv[1]\n"
                "length=float(sys.argv[7]); consonant=float(sys.argv[8])\n"
                "ms=max(5.0, length + consonant)\n"
                "sample={'cluster.wav':500,'ra.wav':900,'azi.wav':-700,'iw.wav':450}.get(name.rsplit('/',1)[-1],300)\n"
                "frames=max(1, round(ms*44.1))\n"
                "with wave.open(sys.argv[2],'wb') as w:\n"
                " w.setparams((1,2,44100,0,'NONE','not compressed'))\n"
                " w.writeframes(array.array('h',[sample]*frames).tobytes())\n",
                encoding="utf-8",
            )
            resampler.chmod(0o755)
            env = self._env(standard, resampler, root / "cache")
            env.update({
                "PHONE_WORKER_TETO_VOICEBANK_MODE": "english",
                "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(english),
                "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "1",
                "PHONE_WORKER_TETO_LENGTH_MODE": "post-consonant",
            })
            with patch.dict(os.environ, env, clear=False):
                result = TetoRenderer().synthesize("Brasil", timeout_seconds=10)

            self.assertEqual(result["timeline_mode"], "oto-continuous")
            self.assertGreaterEqual(result["timeline_aux_overlays"], 2)
            self.assertGreater(result["timeline_max_preutterance_ms"], 40)
            self.assertLess(result["timeline_audio_ms"], 520)
            with wave.open(io.BytesIO(result["audio"]), "rb") as audio:
                samples = array.array("h", audio.readframes(audio.getnframes()))
            longest_zero = current = 0
            for sample in samples:
                if sample == 0:
                    current += 1
                    longest_zero = max(longest_zero, current)
                else:
                    current = 0
            self.assertLess(longest_zero, round(0.010 * 44100))

    def test_auto_falls_back_to_standard_bank_when_english_is_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            standard, resampler = self._assets(root)
            env = self._env(standard, resampler, root / "cache")
            env.update({
                "PHONE_WORKER_TETO_VOICEBANK_MODE": "auto",
                "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(root / "missing-english"),
            })
            with patch.dict(os.environ, env, clear=False):
                status = TetoRenderer().status(force=True)
            self.assertTrue(status["ready"])
            self.assertEqual(status["voicebank_profile"], "standard")
            self.assertEqual(status["voice"], "kasane-teto-standard")

    def test_resource_guard_blocks_without_starting_resampler(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            voicebank, resampler = self._assets(root)
            with patch.dict(os.environ, self._env(voicebank, resampler, root / "cache"), clear=False):
                renderer = TetoRenderer(resource_guard=lambda: {"ok": False, "reason": "build ativo"})
                self.assertTrue(renderer.status(force=True)["ready"])
                with self.assertRaisesRegex(TetoResourceError, "build ativo"):
                    renderer.synthesize("teto")

    def test_native_audio_does_not_need_a_conversion_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank, resampler = self._assets(root)
            _write_wav(bank / "te.wav", rate=44100, sample=1000)
            _write_wav(bank / "to.wav", rate=44100, sample=-1000)
            with patch.dict(os.environ, self._env(bank, resampler, root / "cache")):
                with patch("teto_renderer.renderer.subprocess.run", wraps=subprocess.run) as run:
                    result = TetoRenderer().synthesize("teto")
                self.assertEqual(len(run.call_args_list), 2)
                self.assertTrue(all(call.args[0][0] == str(resampler) for call in run.call_args_list))
                self.assertTrue(all(call.args[0][-1] != "AA" for call in run.call_args_list))
                with wave.open(io.BytesIO(result["audio"]), "rb") as audio:
                    self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 44100))
                    samples = array.array("h", audio.readframes(audio.getnframes()))
                self.assertGreater(max(samples), 0)
                self.assertLess(min(samples), 0)

    def test_parallel_completion_preserves_audio_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank, resampler = self._assets(root)
            _write_wav(bank / "te.wav", rate=44100, sample=1000)
            _write_wav(bank / "to.wav", rate=44100, sample=-1000)
            resampler.write_text(resampler.read_text().replace(
                "import shutil, sys\n", "import shutil, sys, time\ntime.sleep(0.08 if sys.argv[1].endswith('te.wav') else 0)\n"
            ))
            outputs = []
            for workers in (1, 2):
                env = self._env(bank, resampler, root / f"cache-{workers}")
                env["PHONE_WORKER_TETO_RENDER_THREADS"] = str(workers)
                with patch.dict(os.environ, env):
                    outputs.append(TetoRenderer().synthesize("tetoteto")["audio"])
            self.assertEqual(outputs[0], outputs[1])

    def test_profile_changes_invalidate_audio_caches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank, resampler = self._assets(root)
            env = self._env(bank, resampler, root / "cache")
            with patch.dict(os.environ, env):
                renderer = TetoRenderer()
                initial = renderer.status(force=True)
                with patch.dict(os.environ, {"PHONE_WORKER_TETO_SPEECH_RATE": "1.25"}):
                    faster = renderer.status(force=True)
                with patch.dict(os.environ, {"PHONE_WORKER_TETO_MODULATION": "25"}):
                    expressive = renderer.status(force=True)
            self.assertEqual(initial["voicebank_fingerprint"], faster["voicebank_fingerprint"])
            self.assertEqual(initial["voicebank_fingerprint"], expressive["voicebank_fingerprint"])
            self.assertEqual(len({s["fingerprint"] for s in (initial, faster, expressive)}), 3)
            keys = [tts_policy.standard_cache_key(
                {"engine": "teto", "text": "teto"}, engine="teto",
                sanitize_key=tts_policy.sanitize_cache_key,
                normalize_rate=tts_policy.normalize_edge_rate,
                normalize_pitch=tts_policy.normalize_edge_pitch,
                normalize_language=tts_policy.normalize_gtts_language,
                teto_fingerprint=s["fingerprint"], teto_base_pitch="C4",
            ) for s in (initial, faster, expressive)]
            self.assertEqual(len(set(keys)), 3)

    def test_straycat_duration_includes_the_consonant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank, original = self._assets(root)
            resampler = root / "straycat-rs-test"
            resampler.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, wave, math, array\n"
                "stretch = 2 ** (1 - float(sys.argv[4]) / 100)\n"
                "ms = float(sys.argv[7]) + math.floor(float(sys.argv[8]) * stretch / 5) * 5\n"
                "with wave.open(sys.argv[2], 'wb') as w:\n"
                "    w.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))\n"
                "    w.writeframes(array.array('h', [1000] * round(ms * 44.1)).tobytes())\n"
            )
            resampler.chmod(0o755)
            (bank / "oto.ini").write_text("te.wav=て,0,62,0,0,0\nto.wav=と,0,59,0,0,0\n")
            with patch.dict(os.environ, self._env(bank, resampler, root / "cache")):
                result = TetoRenderer().synthesize("teto")
                with wave.open(io.BytesIO(result["audio"]), "rb") as w:
                    duration = w.getnframes() / w.getframerate()
            # Prosody 3B keeps the stressed mora longer and the unstressed
            # one shorter, but the tiny lexical separator is no longer emitted
            # as hard silence. Consonants must still stay inside the budget.
            self.assertAlmostEqual(duration, 0.264, delta=0.003)

    def test_quality_pitchbend_failure_retries_neutral_without_killing_teto(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank, resampler = self._assets(root)
            _write_wav(bank / "te.wav", rate=44100, sample=900)
            _write_wav(bank / "to.wav", rate=44100, sample=-900)
            resampler.write_text(
                "#!/usr/bin/env python3\n"
                "import shutil, sys\n"
                "if sys.argv[-1] != 'AA':\n"
                "    print('pitch curve rejected', file=sys.stderr)\n"
                "    raise SystemExit(23)\n"
                "shutil.copyfile(sys.argv[1], sys.argv[2])\n",
                encoding="utf-8",
            )
            resampler.chmod(0o755)
            with patch.dict(os.environ, self._env(bank, resampler, root / "cache")):
                result = TetoRenderer().synthesize("teto")
            self.assertEqual(result["renderer_version"], "speech-4d-texttoteto-pitch")
            self.assertEqual(result["phonemizer_version"], "ptbr-g2p-xsampa-cvvc-v1")
            self.assertEqual(result["pitchbend_fallbacks"], 2)
            self.assertTrue(bytes(result["audio"]).startswith(b"RIFF"))

    def test_speech3_fragment_cache_is_reused_after_quality_revision(self):
        from teto_renderer.phonemizer import phonemize
        from teto_renderer.prosody import build_notes

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank, resampler = self._assets(root)
            _write_wav(bank / "te.wav", rate=44100, sample=700)
            _write_wav(bank / "to.wav", rate=44100, sample=-700)
            resampler.write_text(
                "#!/usr/bin/env python3\nimport sys\nraise SystemExit(91)\n",
                encoding="utf-8",
            )
            resampler.chmod(0o755)
            with patch.dict(os.environ, self._env(bank, resampler, root / "cache")):
                renderer = TetoRenderer()
                index = renderer._load_index()
                notes = build_notes(phonemize("teto"))
                entries = [index.resolve(note.candidates) for note in notes]
                for note, entry in zip(notes, entries):
                    self.assertIsNotNone(entry)
                    legacy = renderer._fragment_cache_payload(
                        fingerprint=renderer._render_fingerprint_for_version(
                            index, renderer.LEGACY_FRAGMENT_RENDER_VERSION
                        ),
                        entry=entry,
                        note=note,
                    )
                    source = entry.wav_path
                    renderer._cache.put(renderer._cache.key(legacy), source)
                result = renderer.synthesize("teto")
            self.assertEqual(result["legacy_fragment_hits"], 2)
            self.assertEqual(result["pitchbend_fallbacks"], 0)

    def test_oto_preutterance_contributes_to_join_without_unbounded_overlap(self):
        from teto_renderer.voicebank import OtoEntry

        base = OtoEntry("a", Path("a.wav"), 0, 40, 0, 0, 20)
        early = OtoEntry("a", Path("a.wav"), 0, 40, 0, 80, 20)
        extreme = OtoEntry("a", Path("a.wav"), 0, 40, 0, 1000, 20)
        self.assertEqual(TetoRenderer._oto_join_ms(base), 20.0)
        self.assertEqual(TetoRenderer._oto_join_ms(early), 36.0)
        self.assertEqual(TetoRenderer._oto_join_ms(extreme), 55.0)


if __name__ == "__main__":
    unittest.main()
