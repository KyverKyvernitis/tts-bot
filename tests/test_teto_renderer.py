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
            self.assertEqual(result["renderer_version"], "speech-3b-natural-safe")
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
