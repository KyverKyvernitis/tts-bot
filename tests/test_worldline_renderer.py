from __future__ import annotations

import array
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "deploy" / "termux" / "phone-worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer import WorldlineRenderer
from teto_renderer import worldline
from teto_renderer.errors import TetoConfigurationError, TetoResourceError, TetoSynthesisError
from teto_renderer.prosody import RenderNote
from teto_renderer.voicebank import OtoEntry


def wav(path: Path, *, seconds=0.5, rate=44100):
    pcm = array.array("h", (int(6000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(round(rate * seconds))))
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        target.writeframes(pcm.tobytes())


def native_envelope_weight(request, frame):
    """Pinned phrase_synth.cpp envelope on its actual integer frame grid."""
    rounded = lambda value: math.floor(value / 10 + 0.5)
    p0 = max(0, rounded(request["position_ms"]))
    p4 = rounded(request["position_ms"] + request["length_ms"])
    p1 = max(p0 + 1, rounded(request["position_ms"] + request["fade_in_ms"]))
    p3 = min(p4 - 1, rounded(request["position_ms"] + request["length_ms"] - request["fade_out_ms"]))
    if not p0 <= frame < p4:
        return 0
    if frame < p1:
        return (frame - p0) / (p1 - p0)
    if frame >= p3:
        return (p4 - frame) / (p4 - p3)
    return 1


class WorldlineRendererTests(unittest.TestCase):
    def fixture(self, root: Path):
        bank = root / "voicebank"
        bank.mkdir()
        wav(bank / "te.wav")
        wav(bank / "to.wav", rate=22050)
        (bank / "oto.ini").write_text("te.wav=て,0,20,0,0,0\nto.wav=と,0,20,0,0,0\n", encoding="utf-8")
        (bank / "character.txt").write_text("name=Teto protocol fixture\n", encoding="utf-8")
        library = root / "libworldline.so"
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 183)
        library.write_bytes(header)
        digest = hashlib.sha256(header).hexdigest()
        env = {"PHONE_WORKER_TETO_ENABLED": "true", "PHONE_WORKER_TETO_VOICEBANK_MODE": "standard",
               "PHONE_WORKER_TETO_VOICEBANK_DIR": str(bank), "PHONE_WORKER_TETO_MIN_ALIASES": "1",
               "PHONE_WORKER_WORLDLINE_LIBRARY": str(library), "PHONE_WORKER_TETO_RESAMPLER_COMMAND": "/missing/resampler",
               "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "5"}
        return bank, library, digest, env

    def fake_guest(self, root: Path, digest: str):
        script = root / "guest.py"
        script.write_text(
            "import array,json,math,pathlib,sys,wave\n"
            f"proof={{'ok':True,'api_verified':True,'abi_verified':True,'library_hash_verified':True,'library_sha256':{digest!r},'native_architecture':'arm64','source_commit':{worldline.SOURCE_COMMIT!r},'source_version':{worldline.SOURCE_VERSION!r}}}\n"
            "if len(sys.argv)>1:\n"
            " directory=pathlib.Path(sys.argv[1]);job=json.loads((directory/'job.json').read_text())\n"
            " frames=round(max(r['position_ms']+r['length_ms'] for r in job['requests'])*44.1)+1\n"
            " pcm=array.array('h',(int(4000*math.sin(2*math.pi*220*i/44100)) for i in range(frames)))\n"
            " for begin,end in job['silence_intervals_ms']:\n"
            "  for i in range(min(frames,round(begin*44.1)),min(frames,round(end*44.1))):pcm[i]=0\n"
            " with wave.open(str(directory/'output.wav'),'wb') as w:\n"
            "  w.setparams((1,2,44100,0,'NONE','not compressed'));w.writeframes(pcm.tobytes())\n"
            " proof.update(phrase_render_verified=True,frames=frames)\n"
            "print(json.dumps(proof))\n", encoding="utf-8")
        return lambda library, workdir=None: [sys.executable, str(script)] + ([str(workdir)] if workdir else [])

    def test_status_and_synthesis_use_phrase_backend_without_resampler_or_fragment_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank, library, digest, env = self.fixture(root)
            before = {p.name: p.read_bytes() for p in bank.iterdir()}
            with patch.dict(os.environ, env), patch.object(worldline, "ARM64_LIBRARY_SHA256", digest):
                renderer = WorldlineRenderer()
                self.assertFalse(hasattr(renderer, "_cache"))
                with patch.object(renderer, "_guest_command", side_effect=self.fake_guest(root, digest)) as commands:
                    status = renderer.status(force=True)
                    self.assertTrue(status["ready"], status)
                    self.assertEqual(status["backend"], "worldline-r")
                    self.assertFalse(status["box64_required"])
                    self.assertEqual(renderer.status(), status)
                    self.assertEqual(commands.call_count, 1)
                    result = renderer.synthesize("teto, teto", timeout_seconds=10)
            self.assertEqual(before, {p.name: p.read_bytes() for p in bank.iterdir()})
            self.assertEqual(result["backend"], "worldline-r")
            self.assertEqual(result["missing_phonemes"], [])
            self.assertTrue(result["native_phrase_render_verified"])
            self.assertFalse(result["portuguese_speech_verified"])
            self.assertEqual(result["pitch_alignment_mode"], "absolute-timeline")
            self.assertEqual(result["envelope_mode"], "complementary-native-envelopes")
            with wave.open(io.BytesIO(result["audio"]), "rb") as output:
                self.assertEqual((output.getnchannels(), output.getsampwidth(), output.getframerate()), (1, 2, 44100))
                samples = array.array("h", output.readframes(output.getnframes()))
            self.assertTrue(any(samples))
            longest = current = 0
            for sample in samples:
                current = current + 1 if sample == 0 else 0
                longest = max(current, longest)
            self.assertGreater(longest, 44100 * 0.07)

    def test_production_pin_and_architecture_are_required_before_any_guest_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, library, _, env = self.fixture(root)
            with patch.dict(os.environ, env):
                renderer = WorldlineRenderer()
                with patch.object(renderer, "_guest_command") as command:
                    status = renderer.status(force=True)
                    self.assertFalse(status["ready"])
                    self.assertIn("SHA-256", status["last_error"])
                    with self.assertRaises(TetoConfigurationError):
                        renderer.synthesize("teto")
                    command.assert_not_called()
                data = bytearray(library.read_bytes())
                struct.pack_into("<H", data, 18, 62)
                library.write_bytes(data)
                self.assertIn("ARM64", renderer.status(force=True)["last_error"])

    def test_resource_guard_and_busy_renderer_block_before_source_decode(self):
        renderer = WorldlineRenderer(resource_guard=lambda: {"ok": False, "reason": "build ativo"})
        with patch.dict(os.environ, {"PHONE_WORKER_TETO_ENABLED": "true"}), patch.object(renderer, "_decode_sources") as decode:
            with self.assertRaisesRegex(TetoResourceError, "build ativo"):
                renderer.synthesize("teto")
            decode.assert_not_called()
        renderer = WorldlineRenderer()
        renderer._lock.acquire()
        try:
            with patch.dict(os.environ, {"PHONE_WORKER_TETO_ENABLED": "true"}):
                with self.assertRaisesRegex(TetoResourceError, "ocupado"):
                    renderer.synthesize("teto")
        finally:
            renderer._lock.release()

    def test_backend_fingerprint_ignores_straycat_flags_and_changes_for_native_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, digest, env = self.fixture(root)
            with patch.dict(os.environ, env), patch.object(worldline, "ARM64_LIBRARY_SHA256", digest):
                renderer = WorldlineRenderer()
                index = renderer._load_index()
                initial = renderer._render_fingerprint(index)
                with patch.dict(os.environ, {"PHONE_WORKER_TETO_FLAGS": "g99Mt50"}):
                    self.assertEqual(initial, renderer._render_fingerprint(index))
                with patch.dict(os.environ, {"PHONE_WORKER_TETO_SPEECH_RATE": "1.2"}):
                    self.assertNotEqual(initial, renderer._render_fingerprint(index))
                with patch.dict(os.environ, {"PHONE_WORKER_TETO_VELOCITY": "140"}):
                    self.assertNotEqual(initial, renderer._render_fingerprint(index))

    def test_global_f0_has_one_nucleus_contour_and_zero_punctuation_pause(self):
        renderer = WorldlineRenderer()
        notes = [RenderNote(("a",), "C4", 100, 100, phrase_end=".", pitch_start_cents=0, pitch_peak_cents=20, pitch_end_cents=0),
                 RenderNote(("a",), "C4", 100, 0, pitch_start_cents=0, pitch_peak_cents=20, pitch_end_cents=0)]
        entry = OtoEntry("a", Path("a.wav"), 0, 20, 0, 0, 0)
        job = renderer._job(notes, [entry, entry], {entry.wav_path: "source_0.wav"}, 5)
        self.assertEqual(job["requests"][1]["position_ms"], 200)
        self.assertEqual(job["silence_intervals_ms"], [[100, 200]])
        self.assertEqual(job["curves"]["voicing"][10:20], [0.0] * 10)
        self.assertEqual(job["curves"]["f0"][10:20], [0.0] * 10)
        self.assertGreater(job["curves"]["f0"][5], job["curves"]["f0"][0])
        self.assertGreater(job["curves"]["f0"][20], 200)

    def test_paired_40ms_native_crossfade_weights_sum_to_one(self):
        entry = OtoEntry("a", Path("a.wav"), 0, 20, 0, 80, 40)
        notes = [RenderNote(("a",), "C4", 100, 0) for _ in range(2)]
        job = WorldlineRenderer()._job(notes, [entry, entry], {entry.wav_path: "source_0.wav"}, 5)
        left, right = job["requests"]
        self.assertEqual((left["position_ms"], left["length_ms"], right["position_ms"], right["length_ms"]),
                         (0, 100, 60, 100))
        self.assertEqual(left["fade_out_ms"], 40)
        self.assertEqual(right["fade_in_ms"], 40)
        self.assertEqual(right["fade_out_ms"], 10)
        self.assertEqual(job["crossfade_pairs"], 1)
        self.assertEqual(job["duration_ms"], 170)
        self.assertEqual(job["silence_intervals_ms"], [])
        self.assertEqual(job["envelope_mode"], "complementary-native-envelopes")
        for frame in range(6, 10):
            self.assertAlmostEqual(native_envelope_weight(left, frame) + native_envelope_weight(right, frame), 1)
        # This is the original excess spectral energy, not just a flag change.
        previous = {**left, "fade_out_ms": 10}
        self.assertAlmostEqual(native_envelope_weight(previous, 9) + native_envelope_weight(right, 9), 1.75)

    def test_adjacent_pair_balancing_preserves_sources_consonants_pitch_pauses_and_duration(self):
        class PreviousRenderer(WorldlineRenderer):
            def _balance_crossfades(self, requests):
                return 0
        entry = OtoEntry("a", Path("a.wav"), 10, 20, -300, 80, 40)
        notes = [RenderNote(("a",), "C4", 100, 0), RenderNote(("a",), "C4", 100, 80, phrase_end="."),
                 RenderNote(("a",), "C4", 100, 0)]
        previous = PreviousRenderer()._job(notes, [entry] * 3, {entry.wav_path: "source_0.wav"}, 5)
        current = WorldlineRenderer()._job(notes, [entry] * 3, {entry.wav_path: "source_0.wav"}, 5)
        for key in ("curves", "silence_intervals_ms", "duration_ms", "max_output_seconds"):
            self.assertEqual(current[key], previous[key])
        self.assertEqual(current["silence_intervals_ms"], [[160, 240]])
        for before, after in zip(previous["requests"], current["requests"]):
            self.assertEqual({key: value for key, value in before.items() if key not in ("fade_in_ms", "fade_out_ms")},
                             {key: value for key, value in after.items() if key not in ("fade_in_ms", "fade_out_ms")})
        self.assertEqual(current["crossfade_pairs"], 1)
        self.assertEqual(current["requests"][1]["fade_out_ms"], 10)
        self.assertEqual(current["requests"][2]["fade_in_ms"], 0)

    def test_nested_layered_and_short_note_envelopes_remain_unchanged(self):
        def request(start, length, fade_in=40, fade_out=10):
            return {"position_ms": start, "length_ms": length, "fade_in_ms": fade_in, "fade_out_ms": fade_out}
        cases = [
            [request(0, 100, 0), request(60, 100), request(80, 100)],  # A third model occupies the join.
            [request(0, 200, 0), request(60, 80)],  # Nested auxiliary.
            [request(0, 100, 0), request(40, 150, 10)],  # Longer OTO lead, not a matched fade.
            [request(0, 100, 80), request(60, 100)],  # Outgoing ramp would overlap its incoming ramp.
            [request(0, 100, 0), request(60, 60, 40, 40)],  # Incoming note already fades out in the join.
            [request(0, 100, 0)],  # Isolated ending.
            [request(0, 100, 0), request(100, 100, 0)],  # No overlap.
        ]
        renderer = WorldlineRenderer()
        for requests in cases:
            with self.subTest(requests=requests):
                before = [dict(item) for item in requests]
                self.assertEqual(renderer._balance_crossfades(requests), 0)
                self.assertEqual(requests, before)

    def test_native_grid_alignment_allows_only_one_frame_placement_discrepancy(self):
        requests = [{"position_ms": 0, "length_ms": 110, "fade_in_ms": 0, "fade_out_ms": 10},
                    {"position_ms": 60, "length_ms": 100, "fade_in_ms": 40, "fade_out_ms": 10}]
        self.assertEqual(WorldlineRenderer()._balance_crossfades(requests), 1)
        self.assertEqual(requests[0]["fade_out_ms"], 50)
        self.assertEqual(requests[1]["fade_in_ms"], 50)
        for frame in range(6, 11):
            self.assertAlmostEqual(sum(native_envelope_weight(item, frame) for item in requests), 1)

    def test_guest_command_binds_private_sources_and_native_code_without_box64(self):
        renderer = WorldlineRenderer()
        with patch("teto_renderer.worldline.shutil.which", return_value="/bin/proot-distro"):
            command = renderer._guest_command(Path("/safe/runtime/libworldline.so"), Path("/safe/job"))
        self.assertEqual(command[:3], ["proot-distro", "login", "voicepeak-arm64"])
        self.assertIn("/safe/job:/opt/worldline-job", command)
        self.assertIn("/safe/runtime:/opt/worldline-lib", command)
        self.assertIn("/opt/worldline-code/worldline_native.py", command)
        self.assertNotIn("box64", " ".join(command))
        self.assertNotIn("--shared-tmp", command)

    def test_crashed_guest_with_exit_zero_cannot_look_successful(self):
        command = [sys.executable, "-c", "import sys;sys.stderr.write('proot info: vpid 1: terminated with signal 11\\n')"]
        with self.assertRaises(TetoSynthesisError):
            WorldlineRenderer._run(command, deadline=time.monotonic() + 3)

    def test_deadline_kills_descendant_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "descendant-survived"
            child = "import pathlib,time;time.sleep(0.8);pathlib.Path(" + repr(str(marker)) + ").write_text('survived')"
            parent = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);time.sleep(20)"
            with self.assertRaises(subprocess.TimeoutExpired):
                WorldlineRenderer._run([sys.executable, "-c", parent], deadline=time.monotonic() + 0.15)
            time.sleep(0.9)
            self.assertFalse(marker.exists())

    def test_invalid_json_wav_silence_truncation_and_size_are_rejected(self):
        renderer = WorldlineRenderer()
        for output in ("", "not JSON", '{"ok":false}', "[]"):
            with self.assertRaises(TetoSynthesisError):
                renderer._proof(output)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output.wav"
            wav(output)
            with self.assertRaises(TetoSynthesisError):
                renderer._read_output(output, max_audio_bytes=100, max_seconds=5)
            raw = output.read_bytes()
            output.write_bytes(raw[:-100])
            with self.assertRaisesRegex(TetoSynthesisError, "truncado"):
                renderer._read_output(output, max_audio_bytes=100000, max_seconds=5)
            with wave.open(str(output), "wb") as target:
                target.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
                target.writeframes(b"\0\0" * 4410)
            with self.assertRaisesRegex(TetoSynthesisError, "silêncio"):
                renderer._read_output(output, max_audio_bytes=100000, max_seconds=5)

    def test_oversized_plan_and_bad_pin_never_decode_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, digest, env = self.fixture(root)
            env["PHONE_WORKER_TETO_MAX_AUDIO_SECONDS"] = "2"
            with patch.dict(os.environ, env), patch.object(worldline, "ARM64_LIBRARY_SHA256", digest):
                renderer = WorldlineRenderer()
                with patch.object(renderer, "_decode_sources") as decode:
                    with self.assertRaisesRegex(TetoSynthesisError, "planejado"):
                        renderer.synthesize("teto " * 30)
                    decode.assert_not_called()

    @unittest.skipUnless(os.getenv("WORLDLINE_TEST_LIBRARY") and shutil.which("ffmpeg"), "set WORLDLINE_TEST_LIBRARY to pinned local native library")
    def test_real_pinned_c_api_renders_host_plan_and_decoded_sources(self):
        from teto_renderer.worldline_native import inspect_library

        native = Path(os.environ["WORLDLINE_TEST_LIBRARY"]).resolve()
        class LocalNativeRenderer(WorldlineRenderer):
            def _library_metadata(self):
                return inspect_library(native)
            def _library(self):
                return native
            def _guest_command(self, library, workdir=None):
                command = [sys.executable, str(WORKER / "teto_renderer" / "worldline_native.py"), "--library", str(native)]
                return command + (["--probe"] if workdir is None else ["--job", str(workdir / "job.json"), "--output", str(workdir / "output.wav")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, env = self.fixture(root)
            with patch.dict(os.environ, env):
                renderer = LocalNativeRenderer()
                self.assertTrue(renderer.status(force=True)["ready"])
                result = renderer.synthesize("teto, teto", timeout_seconds=30)
            with wave.open(io.BytesIO(result["audio"]), "rb") as output:
                self.assertGreater(output.getnframes(), 4410)
            self.assertTrue(result["native_phrase_render_verified"])
            self.assertEqual(result["rendered_phonemes"], 4)
            self.assertFalse(result["portuguese_speech_verified"])
            self.assertAlmostEqual(result["timeline_audio_ms"], result["timeline_planned_ms"], delta=0.03)
            self.assertEqual(result["envelope_mode"], "complementary-native-envelopes")
            self.assertGreater(result["crossfade_pairs"], 0)

    @unittest.skipUnless(os.getenv("WORLDLINE_TEST_LIBRARY"), "set WORLDLINE_TEST_LIBRARY to pinned local native library")
    def test_real_pinned_api_accepts_complementary_40ms_pair_and_preserves_output_grid(self):
        from teto_renderer.worldline_native import run_isolated

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            wav(directory / "source.wav")
            entry = OtoEntry("a", directory / "source.wav", 0, 20, -300, 80, 40)
            notes = [RenderNote(("a",), "C4", 100, 0) for _ in range(2)]
            job = WorldlineRenderer()._job(notes, [entry] * 2, {entry.wav_path: "source.wav"}, 5)
            path, output = directory / "job.json", directory / "output.wav"
            path.write_text(json.dumps(job), encoding="utf-8")
            result = run_isolated(Path(os.environ["WORLDLINE_TEST_LIBRARY"]).resolve(), path, output, timeout=30)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["phrase_render_verified"])
            self.assertEqual(result["frames"], 7498)
            with wave.open(str(output), "rb") as source:
                self.assertEqual(source.getnframes(), 7498)
                self.assertTrue(any(array.array("h", source.readframes(source.getnframes()))))


if __name__ == "__main__":
    unittest.main()
