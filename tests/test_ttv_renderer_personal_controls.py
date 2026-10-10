from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "deploy" / "termux" / "phone-worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer import TetoRenderer, WorldlineRenderer
from teto_renderer import worldline
from teto_renderer.errors import TetoResourceError


def fixture(root: Path):
    bank = root / "bank"
    bank.mkdir()
    samples = array.array("h", (round(4000 * math.sin(2 * math.pi * 220 * i / 44100)) for i in range(22050)))
    for name in ("te", "to"):
        with wave.open(str(bank / f"{name}.wav"), "wb") as output:
            output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            output.writeframes(samples.tobytes())
    (bank / "oto.ini").write_text("te.wav=て,0,20,0,0,0\nto.wav=と,0,20,0,0,0\n", encoding="utf-8")
    resampler = root / "resampler.py"
    resampler.write_text(
        "#!/usr/bin/env python3\n"
        "import array,json,math,pathlib,sys,wave\n"
        "frames=round(float(sys.argv[7])*44.1)\n"
        "pcm=array.array('h',(round(4000*math.sin(2*math.pi*220*i/44100)) for i in range(frames)))\n"
        "with wave.open(sys.argv[2],'wb') as output:\n"
        " output.setparams((1,2,44100,0,'NONE','not compressed'));output.writeframes(pcm.tobytes())\n"
        f"with open({str(root / 'resampler-jobs.jsonl')!r},'a') as log:log.write(json.dumps({{'pitch':sys.argv[3],'frames':frames}})+'\\n')\n",
        encoding="utf-8",
    )
    resampler.chmod(0o755)
    library = root / "libworldline.so"
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", data, 16, 3, 183)
    library.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    env = {
        "PHONE_WORKER_TETO_ENABLED": "true",
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "standard",
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(bank),
        "PHONE_WORKER_TETO_MIN_ALIASES": "1",
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND": str(resampler),
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(root / "cache"),
        "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "10",
        "PHONE_WORKER_TETO_BASE_PITCH": "C4",
        "PHONE_WORKER_TETO_SPEECH_RATE": "1.25",
        "PHONE_WORKER_TETO_VELOCITY": "100",
        "PHONE_WORKER_TETO_MODULATION": "15",
        "PHONE_WORKER_TETO_RENDER_THREADS": "2",
        "PHONE_WORKER_WORLDLINE_LIBRARY": str(library),
    }
    guest = root / "guest.py"
    guest.write_text(
        "import array,json,math,pathlib,sys,wave\n"
        "directory=pathlib.Path(sys.argv[1]);job=json.loads((directory/'job.json').read_text())\n"
        "frames=round(job['duration_ms']*44.1)+1\n"
        "pcm=array.array('h',(round(4000*math.sin(2*math.pi*220*i/44100)) for i in range(frames)))\n"
        "with wave.open(str(directory/'output.wav'),'wb') as output:\n"
        " output.setparams((1,2,44100,0,'NONE','not compressed'));output.writeframes(pcm.tobytes())\n"
        f"with open({str(root / 'worldline-jobs.jsonl')!r},'a') as log:log.write(json.dumps(job)+'\\n')\n"
        f"print(json.dumps({{'ok':True,'api_verified':True,'abi_verified':True,'library_hash_verified':True,'phrase_render_verified':True,'frames':frames,'library_sha256':{digest!r},'native_architecture':'arm64','source_commit':{worldline.SOURCE_COMMIT!r},'source_version':{worldline.SOURCE_VERSION!r}}}))\n",
        encoding="utf-8",
    )
    return env, digest, guest


def frames(result):
    with wave.open(io.BytesIO(result["audio"]), "rb") as audio:
        assert audio.getframerate() == 44100
        pcm = array.array("h", audio.readframes(audio.getnframes()))
        assert any(pcm)
        return audio.getnframes()


class TtvRendererPersonalControlsTests(unittest.TestCase):
    def run_with_renderer(self, renderer_class, check):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env, digest, guest = fixture(root)
            with patch.dict(os.environ, env), patch.object(worldline, "ARM64_LIBRARY_SHA256", digest):
                renderer = renderer_class()
                if renderer_class is WorldlineRenderer:
                    with patch.object(renderer, "_guest_command", side_effect=lambda library, workdir=None: [sys.executable, str(guest), str(workdir)]):
                        check(renderer, root)
                else:
                    check(renderer, root)

    def test_personal_rate_changes_pcm_duration_without_changing_pitch_or_global_profile(self):
        for renderer_class in (TetoRenderer, WorldlineRenderer):
            with self.subTest(backend=renderer_class.__name__):
                def check(renderer, root):
                    status_fingerprint = renderer.fingerprint()
                    before_env = dict(os.environ)
                    outputs = [renderer.synthesize("teto teto", speech_rate=rate) for rate in (0.85, 1.0, 1.15)]
                    duration = [frames(output) for output in outputs]
                    self.assertGreater(duration[0], duration[1])
                    self.assertGreater(duration[1], duration[2])
                    self.assertEqual([output["speech_rate"] for output in outputs], [0.85, 1.0, 1.15])
                    self.assertEqual([output["pitch_offset_semitones"] for output in outputs], [0, 0, 0])
                    self.assertEqual(len({output["renderer_fingerprint"] for output in outputs}), 3)
                    self.assertEqual(len({output["voicebank_fingerprint"] for output in outputs}), 1)
                    self.assertEqual(renderer.fingerprint(), status_fingerprint)
                    self.assertEqual(dict(os.environ), before_env)
                    if renderer_class is WorldlineRenderer:
                        jobs = [json.loads(line) for line in (root / "worldline-jobs.jsonl").read_text().splitlines()]
                        self.assertEqual(len(jobs), 3)
                        self.assertEqual([[item["tone"] for item in job["requests"]] for job in jobs], [[60] * 4] * 3)
                    else:
                        jobs = [json.loads(line) for line in (root / "resampler-jobs.jsonl").read_text().splitlines()]
                        self.assertEqual({job["pitch"] for job in jobs}, {"C4"})
                self.run_with_renderer(renderer_class, check)

    def test_legacy_global_rate_is_used_only_when_no_personal_rate_is_supplied(self):
        for renderer_class in (TetoRenderer, WorldlineRenderer):
            with self.subTest(backend=renderer_class.__name__):
                def check(renderer, root):
                    legacy = renderer.synthesize("teto")
                    same_rate = renderer.synthesize("teto", speech_rate=1.25)
                    normal = renderer.synthesize("teto", speech_rate=1.0)
                    self.assertEqual(legacy["speech_rate"], 1.25)
                    self.assertEqual(legacy["renderer_fingerprint"], same_rate["renderer_fingerprint"])
                    self.assertEqual(legacy["audio"], same_rate["audio"])
                    self.assertNotEqual(legacy["renderer_fingerprint"], normal["renderer_fingerprint"])
                    self.assertLess(frames(legacy), frames(normal))
                    shifted = renderer.synthesize("teto", speech_rate=1.25, pitch_offset_semitones=2.0)
                    self.assertNotEqual(legacy["renderer_fingerprint"], shifted["renderer_fingerprint"])
                self.run_with_renderer(renderer_class, check)

    def test_invalid_personal_rate_is_rejected_before_loading_assets(self):
        for renderer_class in (TetoRenderer, WorldlineRenderer):
            renderer = renderer_class()
            with patch.object(renderer, "_load_index") as load:
                for invalid in (True, False, "1.0", "bad", float("nan"), float("inf"), -float("inf"), 0.74, 1.51):
                    with self.subTest(backend=renderer_class.__name__, rate=invalid):
                        with self.assertRaisesRegex(ValueError, "velocidade TTV"):
                            renderer.synthesize("teto", speech_rate=invalid)
                load.assert_not_called()

    def test_overlapping_request_is_rejected_without_leaking_personal_rate(self):
        for renderer_class in (TetoRenderer, WorldlineRenderer):
            with self.subTest(backend=renderer_class.__name__):
                def check(renderer, root):
                    if renderer_class is TetoRenderer:
                        self.assertTrue(renderer.status(force=True)["ready"])
                    before_env = dict(os.environ)
                    entered = threading.Event()
                    release = threading.Event()
                    load_index = renderer._load_index

                    def blocked_load():
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError("first request did not receive release")
                        return load_index()

                    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(renderer, "_load_index", side_effect=blocked_load):
                        first = pool.submit(renderer.synthesize, "teto", speech_rate=0.85)
                        try:
                            self.assertTrue(entered.wait(5), "first request never acquired renderer")
                            with self.assertRaisesRegex(TetoResourceError, "ocupado"):
                                renderer.synthesize("teto", speech_rate=1.15)
                        finally:
                            release.set()
                        result = first.result(timeout=10)
                    self.assertEqual(result["speech_rate"], 0.85)
                    self.assertGreater(frames(result), 0)
                    after = renderer.synthesize("teto", speech_rate=1.15)
                    self.assertEqual(after["speech_rate"], 1.15)
                    self.assertGreater(frames(result), frames(after))
                    self.assertEqual(dict(os.environ), before_env)
                self.run_with_renderer(renderer_class, check)


if __name__ == "__main__":
    unittest.main()
