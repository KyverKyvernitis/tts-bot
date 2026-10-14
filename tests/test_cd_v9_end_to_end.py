"""Real-media integration for transactional CD data, hot readers and composition.

No Discord login, network request or live voicebank is needed. MP3/JPEG bytes
are produced and decoded by the installed FFmpeg/Pillow. UTAU composition
tests use deterministic PCM tones and explicitly do not measure UTAU singing.
Run: python -m unittest discover -s tests -p test_cd_v9_end_to_end.py -v
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import importlib
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
import threading
import unittest
from unittest.mock import patch
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy/ttv-cd"))
from package_validation import score_sha256, validate_package
from osaka_cd.store import RuntimeStore, StoreError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_zip(path, members, *, reverse=False, compression=zipfile.ZIP_DEFLATED):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        names = sorted(members, reverse=reverse)
        for name in names:
            archive.writestr(name, members[name])
    return path


def package_members(identity, audio, image, *, pitch=60, revision=1,
                    include_sample=True, include_cover=True, name=None):
    """Construct the actual public contract, including dependency hashes."""
    score = {"id": identity, "name": name or identity.title(), "artist": "Fixture",
             "excerpt": "Isolated integration", "source_url": "https://example.org/midi",
             "source_sha256": digest(f"{identity}:{pitch}".encode()), "revision": revision,
             "duration_ms": 500, "notes": [{"midi_note": pitch, "start_ms": 0,
                                            "duration_ms": 500, "velocity": 100}]}
    score["score_sha256"] = score_sha256(score)
    video_id = digest(identity.encode())[:11]
    video = {"video_id": video_id, "title": score["name"], "artist": "Fixture"}
    cover_path = f"cogs/tts/cd/thumbnails/{video_id}.jpg"
    audio_path = f"cogs/tts/cd/previews/{identity}.mp3"
    if include_cover:
        video["thumbnail_sha256"] = digest(image)
    preview = {"manifest_version": 2, "id": identity, "revision": revision,
               "source_sha256": score["source_sha256"], "score_sha256": score["score_sha256"],
               "voice_id": "kasane-teto", "language": "pt-BR", "pitch_semitones": 0,
               "speech_rate": 1.0, "mp3_sha256": digest(audio)}
    artifacts = {"score": {"status": "ready", "sha256": score["score_sha256"]},
                 "thumbnail": ({"status": "ready", "path": cover_path, "sha256": digest(image)}
                               if include_cover else {"status": "pending"}),
                 "sample": ({"status": "ready", "path": audio_path, "sha256": digest(audio)}
                            if include_sample else {"status": "pending"})}
    manifest = {"manifest_version": 1, "id": identity, "revision": revision,
                "source_sha256": score["source_sha256"], "score_sha256": score["score_sha256"],
                "artifacts": artifacts}
    members = {"cogs/tts/cd_song_scores.json": canonical([score]),
               "cogs/tts/cd_song_videos.json": canonical({"version": 1, "songs": {identity: video}}),
               "cogs/tts/cd/previews/index.json": canonical({identity: preview} if include_sample else {}),
               f"cogs/tts/cd/packages/{identity}.json": canonical(manifest)}
    if include_sample:
        members[audio_path] = audio
    if include_cover:
        members[cover_path] = image
    return members


@contextmanager
def selected_store(store):
    """Pin the process-local store accessor to an isolated, temporary store."""
    from osaka_cd import runtime
    with patch.dict(os.environ, {"OSAKA_CD_DATA_ROOT": str(store.root)}), \
            patch("osaka_cd.store.get_store", return_value=store):
        runtime.invalidate()
        yield runtime
        runtime.invalidate()


class RealMediaFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("FFmpeg and ffprobe are required for real-media validation")
        try:
            from PIL import Image
        except ImportError as exc:
            raise unittest.SkipTest("Pillow is required for real JPEG fixtures") from exc
        cls.media_temp = tempfile.TemporaryDirectory(prefix="cd-v9-media-")
        media = Path(cls.media_temp.name)
        target = media / "tone.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        "sine=frequency=261.625565:duration=0.5:sample_rate=48000",
                        "-c:a", "libmp3lame", "-b:a", "128k", "-y", str(target)],
                       capture_output=True, check=True, timeout=20)
        cls.audio = target.read_bytes()
        images = []
        for color in ((170, 20, 20), (20, 30, 190)):
            image = io.BytesIO()
            Image.new("RGB", (64, 64), color).save(image, format="JPEG")
            images.append(image.getvalue())
        cls.image, cls.other_image = images

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.folder_temp = tempfile.TemporaryDirectory(prefix="cd-v9-integration-")
        self.folder = Path(self.folder_temp.name)
        self.legacy = self.folder / "legacy"
        (self.legacy / "cogs/tts/cd/previews").mkdir(parents=True)
        (self.legacy / "cogs/tts/cd_song_scores.json").write_text("[]", encoding="utf-8")
        (self.legacy / "cogs/tts/cd_song_videos.json").write_text('{"version":1,"songs":{}}', encoding="utf-8")
        (self.legacy / "cogs/tts/cd/previews/index.json").write_text("{}", encoding="utf-8")
        self.store = RuntimeStore(self.folder / "runtime", self.legacy)

    def tearDown(self):
        self.folder_temp.cleanup()

    def upload(self, identity="demo-song", *, store=None, expected=None, **kwargs):
        members = package_members(identity, self.audio, self.image, **kwargs)
        path = write_zip(self.folder / f"{identity}-{len(list(self.folder.glob('*.zip')))}.zip", members)
        target = store or self.store
        job = target.receive(path, digest(path.read_bytes()), expected_versions=expected)
        return target.process(job["job_id"])

    def read_snapshot(self):
        current = self.store.snapshot()
        root = Path(current["root"])
        scores = {item["id"]: item for item in json.loads((root / "cogs/tts/cd_song_scores.json").read_text())}
        previews = json.loads((root / "cogs/tts/cd/previews/index.json").read_text())
        return current, root, scores, previews


class RuntimePublicationIntegration(RealMediaFixtures):
    def test_full_original_arrangement_export_installs_and_hot_reader_binds_real_mix(self):
        from midi_extract import extract_arrangement
        from osaka_cd.arrangement import arrangement_sha256, preview_arrangement, arrangement_performance_sha256
        from osaka_cd.render import render_arrangement
        from catalog_import import _export, preview_manifest
        from cogs.tts import melodies, cd_previews
        configured = os.getenv("OSAKA_CD_ORIGINAL_MIDI", "")
        source = Path(configured) if configured else Path("/workspace/recovery/midi/108297-original.mid")
        if not source.is_file():
            self.skipTest("set OSAKA_CD_ORIGINAL_MIDI for original MIDI publication case")
        full = extract_arrangement(source)["arrangement"]
        window = preview_arrangement(full, max_duration_ms=5000)
        selected = window["arrangement"]
        lead = next(part for part in selected["parts"] if part["role"] == "lead")
        calls = []
        def render_tone(**request):
            calls.append(request)
            preroll = 100 if request["role"] == "lead" else 50
            raw = tone_wav(request["score"], frequency=240 if request["role"] == "lead" else 480,
                           preroll_ms=preroll)
            return raw, {"midi_preroll_ms": preroll, "sha256": digest(raw)}
        composed = render_arrangement(selected, "a", render_tone, output_format="mp3")
        melody = {"id": "original-polyphonic", "name": "Original Polyphonic", "artist": "Fixture",
                  "excerpt": "Isolated PCM composition", "source_url": "", "revision": 1,
                  "source_sha256": full["source"]["sha256"], "duration_ms": selected["duration_ms"],
                  "notes": [{key: note[key] for key in ("midi_note", "start_ms", "duration_ms", "velocity")}
                            for note in lead["notes"]],
                  "arrangement_path": "cogs/tts/cd/arrangements/original-polyphonic.json",
                  "arrangement_sha256": arrangement_sha256(full),
                  "arrangement_preview_start_ms": window["start_ms"],
                  "arrangement_preview_end_ms": window["end_ms"],
                  "preview_arrangement_sha256": arrangement_sha256(selected),
                  "preview_performance_sha256": arrangement_performance_sha256(selected),
                  "arrangement_part_count": len(selected["parts"])}
        melody["score_sha256"] = score_sha256(melody)
        preview = preview_manifest(melody, composed["audio"], {
            "rendering_profile": {"mode": "multipart-utau", "configuration_fingerprint": digest(b"isolated-tone-fixture"),
                                  "render_fingerprint": composed["mix_sha256"]}})
        video = {"video_id": "abcdefghijk", "thumbnail_sha256": digest(self.image)}
        path = self.folder / "original-polyphonic.zip"
        _export(melody, video, preview, self.image, composed["audio"], path,
                sample_status="ready", arrangement=full)
        validated = validate_package(path, require_cd_only=True, require_complete=True, audio_probe=True)
        self.assertEqual(validated["artifacts"]["original-polyphonic"]["arrangement"], "ready")
        received = self.store.receive(path)
        applied = self.store.process(received["job_id"])
        self.assertEqual(applied["state"], "applied", applied)
        with selected_store(self.store) as runtime:
            chosen = melodies.get_melody("original-polyphonic")
            self.assertEqual(chosen.arrangement_sha256, arrangement_sha256(full))
            self.assertEqual(json.loads(chosen.arrangement_json)["source"]["note_count"], full["source"]["note_count"])
            self.assertEqual(chosen.arrangement_preview_sha256, arrangement_sha256(selected))
            prepared = cd_previews.prepared_sample("original-polyphonic", {
                "ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR"})
            self.assertEqual(prepared, composed["audio"])
            self.assertTrue(runtime.acknowledge())
        self.assertTrue(self.store.job(received["job_id"])["installed"])
        self.assertEqual(len(calls), len(selected["parts"]))

    def test_receive_is_durable_and_activation_requires_reader_acknowledgement(self):
        members = package_members("demo-song", self.audio, self.image)
        path = write_zip(self.folder / "input.zip", members)
        job = self.store.receive(path)
        durable_zip = self.store.root / "received" / f"{job['job_id']}.zip"
        self.assertEqual(durable_zip.read_bytes(), path.read_bytes())
        path.unlink()
        restarted = RuntimeStore(self.store.root, self.legacy)
        self.assertEqual(restarted.outstanding(), [job["job_id"]])
        applied = restarted.process(job["job_id"])
        self.assertEqual(applied["state"], "applied")
        self.assertFalse(applied["installed"])
        active = restarted.snapshot()
        self.assertTrue(restarted.acknowledge(active["generation"]))
        self.assertEqual(restarted.job(job["job_id"])["state"], "activated")
        self.assertTrue(restarted.job(job["job_id"])["installed"])
        report = validate_package(durable_zip, require_complete=True, require_cd_only=True, audio_probe=True)
        self.assertEqual(report["artifacts"]["demo-song"]["audio"]["verified"], "decoded")

    def test_semantic_identity_survives_zip_order_compression_and_json_layout(self):
        members = package_members("demo-song", self.audio, self.image)
        first = self.store.receive(write_zip(self.folder / "one.zip", members))
        modified = {path: json.dumps(json.loads(data), indent=3).encode() if path.endswith(".json") else data
                    for path, data in members.items()}
        second = self.store.receive(write_zip(self.folder / "two.zip", modified, reverse=True,
                                             compression=zipfile.ZIP_STORED))
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(self.store.outstanding(), [first["job_id"]])
        applied = self.store.process(first["job_id"])
        generation = self.store.snapshot()["generation"]
        self.assertEqual(self.store.process(second["job_id"])["generation"], generation)
        self.assertEqual(applied["state"], "applied")

    def test_restart_recovers_processing_job_and_rejects_obsolete_ack(self):
        members = package_members("demo-song", self.audio, self.image)
        received = self.store.receive(write_zip(self.folder / "interrupted.zip", members))
        with self.store._connect() as connection:
            connection.execute("UPDATE jobs SET state='processing' WHERE job_id=?", (received["job_id"],))
        restarted = RuntimeStore(self.store.root, self.legacy)
        self.assertIn(received["job_id"], restarted.outstanding())
        recovered = restarted.process(received["job_id"])
        self.assertEqual(recovered["state"], "applied")
        old_generation = recovered["generation"]
        later = self.upload("other-song")
        self.assertFalse(self.store.acknowledge(old_generation))
        self.assertFalse(self.store.job(later["job_id"])["installed"])
        self.assertTrue(self.store.acknowledge(later["generation"]))
        self.assertTrue(self.store.job(later["job_id"])["installed"])
        self.assertTrue(self.store.job(recovered["job_id"])["installed"])

    def test_concurrent_same_song_updates_allow_only_one_expected_version(self):
        self.upload()
        expected = self.store.snapshot()["versions"]["demo-song"]
        jobs = [self.store.receive(write_zip(self.folder / f"update-{index}.zip",
                                            package_members("demo-song", self.audio, self.image, name=name)),
                                   expected_versions={"demo-song": expected})
                for index, name in enumerate(("One", "Two"))]
        second_store = RuntimeStore(self.store.root, self.legacy)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.process, job["job_id"])
                       for store, job in zip((self.store, second_store), jobs)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sorted(item["state"] for item in results), ["applied", "failed"])
        failed = next(item for item in results if item["state"] == "failed")
        self.assertEqual(failed["error_code"], "conflict")
        _, _, scores, _ = self.read_snapshot()
        self.assertIn(scores["demo-song"]["name"], ("One", "Two"))

    def test_concurrent_different_songs_preserve_both_catalog_entries(self):
        members_a = package_members("first-song", self.audio, self.image)
        members_b = package_members("second-song", self.audio, self.image)
        jobs = [self.store.receive(write_zip(self.folder / "first.zip", members_a)),
                self.store.receive(write_zip(self.folder / "second.zip", members_b))]
        # Two store instances exercise the physical flock, not just one RLock.
        second_store = RuntimeStore(self.store.root, self.legacy)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.process, job["job_id"])
                       for store, job in zip((self.store, second_store), jobs)]
            results = [future.result(timeout=30) for future in futures]
        self.assertTrue(all(item["state"] == "applied" for item in results), results)
        _, root, scores, previews = self.read_snapshot()
        self.assertEqual(set(scores), {"first-song", "second-song"})
        self.assertEqual(set(previews), set(scores))
        for identity in scores:
            self.assertEqual((root / f"cogs/tts/cd/previews/{identity}.mp3").read_bytes(), self.audio)

    def test_compare_and_swap_rejects_stale_cover_update_and_conserves_generation(self):
        self.upload()
        expected = self.store.snapshot()["versions"]["demo-song"]
        updated = package_members("demo-song", self.audio, self.other_image)
        fresh = self.store.receive(write_zip(self.folder / "fresh.zip", updated), expected_versions={"demo-song": expected})
        self.assertEqual(self.store.process(fresh["job_id"])["state"], "applied")
        generation = self.store.snapshot()["generation"]
        stale = package_members("demo-song", self.audio, self.image, name="Older metadata")
        old = self.store.receive(write_zip(self.folder / "stale.zip", stale), expected_versions={"demo-song": expected})
        result = self.store.process(old["job_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "conflict")
        self.assertEqual(self.store.snapshot()["generation"], generation)

    def test_media_corruption_never_replaces_last_valid_catalog(self):
        self.upload()
        generation = self.store.snapshot()["generation"]
        # Declared digest matches deliberately truncated MP3, requiring decode
        # validation to catch corruption rather than relying on a SHA mismatch.
        broken = package_members("broken-song", b"ID3\x04\x00\x00\x00\x00\x00\x00", self.image)
        job = self.store.receive(write_zip(self.folder / "bad-audio.zip", broken))
        result = self.store.process(job["job_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.store.snapshot()["generation"], generation)
        _, root, scores, _ = self.read_snapshot()
        self.assertEqual(set(scores), {"demo-song"})
        self.assertEqual((root / "cogs/tts/cd/previews/demo-song.mp3").read_bytes(), self.audio)

    def test_hash_mismatch_and_non_cd_code_upload_are_rejected(self):
        members = package_members("demo-song", self.audio, self.image)
        path = write_zip(self.folder / "good.zip", members)
        with self.assertRaises(StoreError):
            self.store.receive(path, "0" * 64)
        members["bot.py"] = b"raise RuntimeError('must never run')"
        with self.assertRaises(StoreError):
            self.store.receive(write_zip(self.folder / "code.zip", members))
        self.assertFalse(self.store.snapshot()["initialized"])

    def test_cover_completion_preserves_ready_sample_and_rolls_back_only_that_song(self):
        self.upload(include_cover=False)
        old = self.store.snapshot()
        first_root = Path(old["root"])
        initial_audio = (first_root / "cogs/tts/cd/previews/demo-song.mp3").read_bytes()
        self.upload("other-song")
        completed = self.upload(expected={"demo-song": self.store.snapshot()["versions"]["demo-song"]})
        self.assertEqual(completed["state"], "applied")
        _, root, scores, previews = self.read_snapshot()
        self.assertEqual((root / "cogs/tts/cd/previews/demo-song.mp3").read_bytes(), initial_audio)
        self.assertEqual(previews["demo-song"]["mp3_sha256"], digest(initial_audio))
        result = self.store.rollback("demo-song", generation=old["generation"],
                                     expected_version=self.store.snapshot()["versions"]["demo-song"])
        self.assertEqual(result["state"], "applied")
        _, root, scores, previews = self.read_snapshot()
        self.assertEqual(set(scores), {"demo-song", "other-song"})
        package = json.loads((root / "cogs/tts/cd/packages/demo-song.json").read_text())
        self.assertEqual(package["artifacts"]["thumbnail"]["status"], "pending")
        self.assertEqual(package["artifacts"]["sample"]["status"], "ready")
        self.assertEqual((root / "cogs/tts/cd/previews/demo-song.mp3").read_bytes(), initial_audio)

    def test_score_update_invalidates_audio_without_mutating_old_generation(self):
        self.upload()
        before = self.store.snapshot()
        self.upload(pitch=62, revision=2, include_sample=False,
                    expected={"demo-song": before["versions"]["demo-song"]})
        _, root, scores, previews = self.read_snapshot()
        self.assertEqual(scores["demo-song"]["revision"], 2)
        self.assertNotIn("demo-song", previews)
        old_audio = Path(before["root"]) / "cogs/tts/cd/previews/demo-song.mp3"
        self.assertEqual(old_audio.read_bytes(), self.audio)
        package = json.loads((root / "cogs/tts/cd/packages/demo-song.json").read_text())
        self.assertEqual(package["artifacts"]["sample"]["status"], "pending")

    def test_hot_readers_pin_one_generation_and_keep_previous_valid_snapshot(self):
        from cogs.tts import cd_covers, cd_previews, melodies
        self.upload(name="First")
        preferences = {"ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR"}
        with selected_store(self.store) as runtime:
            with runtime.pin_snapshot() as old:
                self.assertEqual(melodies.get_melody("demo-song").name, "First")
                self.assertEqual(cd_covers._cover_bytes("demo-song"), self.image)
                self.assertEqual(cd_previews.prepared_sample("demo-song", preferences), self.audio)
                changed = package_members("demo-song", self.audio, self.other_image, name="Second")
                job = self.store.receive(write_zip(self.folder / "second-revision.zip", changed),
                                         expected_versions={"demo-song": old["versions"]["demo-song"]})
                self.assertEqual(self.store.process(job["job_id"])["state"], "applied")
                self.assertEqual(melodies.get_melody("demo-song").name, "First")
                self.assertEqual(cd_covers._cover_bytes("demo-song"), self.image)
            self.assertEqual(melodies.get_melody("demo-song").name, "Second")
            self.assertEqual(cd_covers._cover_bytes("demo-song"), self.other_image)
            self.assertEqual(cd_previews.prepared_sample("demo-song", preferences), self.audio)
            selected = runtime.snapshot()
            self.assertTrue(runtime.acknowledge(selected))
            # External storage corruption is refused by the readers; it does
            # not change their last complete generation or resurrect UI views.
            scores = Path(selected["root"]) / "cogs/tts/cd_song_scores.json"
            scores.write_text("not json", encoding="utf-8")
            runtime.invalidate()
            self.assertEqual(runtime.snapshot()["generation"], selected["generation"])
            self.assertEqual(melodies.get_melody("demo-song").name, "Second")


def tone_wav(score, *, frequency, preroll_ms=0, rate=48000, amplitude=8000):
    """Emit mono PCM on the exact score clock, with declared renderer preroll."""
    frames = round((score["duration_ms"] + preroll_ms) * rate / 1000)
    samples = [0] * frames
    for note in score["notes"]:
        start = round((note["start_ms"] + preroll_ms) * rate / 1000)
        end = min(frames, start + round(note["duration_ms"] * rate / 1000))
        for index in range(start, end):
            samples[index] = round(amplitude * math.sin(2 * math.pi * frequency * (index - start) / rate))
    result = io.BytesIO()
    with wave.open(result, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return result.getvalue()


def arrangement_fixture(duration=1000, *, long=False):
    notes = ([{"midi_note": 60, "start_ms": start, "duration_ms": 1000, "velocity": 100}
              for start in range(0, duration, 1000)] if long else
             [{"midi_note": 60, "start_ms": 300, "duration_ms": duration - 300, "velocity": 100}])
    harmony = [dict(note, midi_note=64) for note in notes]
    return {"score_schema": 2, "duration_ms": duration, "parts": [
        {"id": "lead", "role": "lead", "voice_id": "kasane-teto", "lyrics_source": "vowel",
         "notes": notes, "gain": 1.0, "pan": -1.0},
        {"id": "harmony", "role": "harmony", "voice_id": "kasane-teto", "lyrics_source": "vowel",
         "notes": harmony, "gain": 1.0, "pan": 1.0}],
        "source": {"sha256": "a" * 64}, "warnings": []}


class MusicalCompositionIntegration(unittest.TestCase):
    def setUp(self):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("real FFmpeg is required for composition")
        self.temporary = tempfile.TemporaryDirectory(prefix="cd-v9-compose-")
        self.folder = Path(self.temporary.name)
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def renderer(self, **request):
        self.calls.append(request)
        preroll = 120 if request["role"] == "lead" else 40
        frequency = 240 if request["role"] == "lead" else 480
        raw = tone_wav(request["score"], frequency=frequency, preroll_ms=preroll)
        return raw, {"midi_preroll_ms": preroll, "sha256": digest(raw),
                     "voicebank_fingerprint": "isolated-tone-fixture"}

    def compose(self, arrangement=None, **kwargs):
        from osaka_cd.render import render_arrangement
        return render_arrangement(arrangement or arrangement_fixture(), "a", self.renderer,
                                  cache_dir=self.folder / "cache", render_profile_fingerprint="tone-test-v1",
                                  **kwargs)

    @staticmethod
    def stereo_samples(data):
        with wave.open(io.BytesIO(data)) as source:
            assert source.getnchannels() == 2 and source.getsampwidth() == 2
            rate, frames = source.getframerate(), source.getnframes()
            samples = struct.unpack("<" + "h" * (frames * 2), source.readframes(frames))
        return rate, frames, samples[0::2], samples[1::2]

    def test_real_pcm_stems_with_different_prerolls_are_mixed_on_same_attack(self):
        result = self.compose()
        rate, frames, left, right = self.stereo_samples(result["audio"])
        self.assertEqual(rate, 48000)
        self.assertEqual(frames, round(1120 * rate / 1000))
        first_left = next(index for index, sample in enumerate(left) if abs(sample) > 2)
        first_right = next(index for index, sample in enumerate(right) if abs(sample) > 2)
        self.assertEqual(first_left, first_right)
        self.assertAlmostEqual(first_left * 1000 / rate, 420, delta=1000 / rate)
        self.assertLessEqual(max(abs(sample) for sample in left + right), 32767 * 0.92 + 1)
        self.assertFalse(result["metadata"]["chunk_continuations_reattacked"])
        self.assertEqual(len(self.calls), 2)

    def test_stem_cache_reuses_synthesis_when_only_mix_changes(self):
        first = self.compose()
        self.assertEqual(len(self.calls), 2)
        warm = self.compose()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(stem["cache_hit"] for stem in warm["stems"]))
        self.assertEqual(first["audio"], warm["audio"])
        changed = arrangement_fixture()
        changed["parts"][1].update(gain=0.3, pan=0.2)
        mixed = self.compose(changed)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(first["mix_sha256"], mixed["mix_sha256"])
        self.assertNotEqual(first["audio"], mixed["audio"])

    def test_corrupt_stem_cache_is_rerendered_and_cancellation_keeps_valid_cache(self):
        first = self.compose()
        stem = first["stems"][0]
        path = self.folder / "cache/stems" / (stem["key"] + ".wav")
        path.write_bytes(b"not a WAV")
        repaired = self.compose()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(repaired["audio"], first["audio"])
        from osaka_cd.render import RenderError
        with self.assertRaisesRegex(RenderError, "render_cancelled"):
            self.compose(cancelled=lambda: True)
        self.assertEqual(self.compose()["audio"], first["audio"])
        self.assertEqual(len(self.calls), 3)

    def test_multi_chunk_composition_has_exact_full_duration_and_decodable_mp3(self):
        result = self.compose(arrangement_fixture(20000, long=True), output_format="wav")
        rate, frames, left, right = self.stereo_samples(result["audio"])
        self.assertEqual(frames, round((20000 + 120) * rate / 1000))
        self.assertGreater(result["metadata"]["chunks"], 1)
        boundary = round((18000 + 120) * rate / 1000)
        for channel in (left, right):
            self.assertGreater(sum(abs(sample) for sample in channel[boundary:boundary + 480]), 1000)
        mp3 = self.compose(arrangement_fixture(20000, long=True), output_format="mp3")
        target = self.folder / "full-mix.mp3"
        target.write_bytes(mp3["audio"])
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(target),
                                  "-f", "null", "-"], capture_output=True, timeout=30)
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode())
        self.assertEqual(len(self.calls), len(result["stems"]))
        self.assertTrue(all(stem["cache_hit"] for stem in mp3["stems"]))

    def test_missing_alignment_metadata_fails_instead_of_guessing_onset(self):
        from osaka_cd.render import RenderError, render_arrangement
        def incomplete(**request):
            return tone_wav(request["score"], frequency=240, preroll_ms=100), {}
        with self.assertRaisesRegex(RenderError, "stem_alignment_metadata_missing"):
            render_arrangement(arrangement_fixture(), "a", incomplete)

    def test_actual_11141_metadata_allows_exact_preroll_inference(self):
        from osaka_cd.render import render_arrangement
        def actual_contract(**request):
            return tone_wav(request["score"], frequency=240, preroll_ms=100), {
                "worker_version": "1.11.41", "teto_timeline_mode": "midi-vowel-continuous"}
        result = render_arrangement(arrangement_fixture(), "a", actual_contract)
        self.assertAlmostEqual(result["metadata"]["midi_preroll_ms"], 100)


class OriginalMidiIntegration(unittest.TestCase):
    def test_original_polyphonic_arrangement_preserves_source_and_all_selected_notes(self):
        from midi_extract import extract_arrangement
        from osaka_cd.arrangement import arrangement_render_plan, preview_arrangement, arrangement_sha256
        configured = os.getenv("OSAKA_CD_ORIGINAL_MIDI", "")
        candidates = [Path(configured)] if configured else [
            ROOT / "examples/polyphonic/wowaka - Rolling Girl original.mid",
            Path("/workspace/recovery/midi/108297-original.mid")]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            self.skipTest("set OSAKA_CD_ORIGINAL_MIDI to run the real Rolling Girl source case")
        before = source.read_bytes()
        result = extract_arrangement(source)
        arrangement = result["arrangement"]
        self.assertGreater(arrangement["duration_ms"], 180000)
        self.assertGreater(arrangement["source"]["note_count"], 2000)
        self.assertEqual(sum(len(stream["notes"]) for stream in arrangement["source"]["streams"]),
                         arrangement["source"]["note_count"])
        self.assertGreaterEqual(len(arrangement["parts"]), 2)
        self.assertFalse(arrangement["selection"]["generated_harmonies"])
        self.assertFalse(arrangement["selection"]["vocal_transcription_confirmed"])
        self.assertEqual(arrangement_sha256(arrangement), result["extraction"]["arrangement_sha256"])
        excerpt = preview_arrangement(arrangement)
        self.assertLessEqual(excerpt["arrangement"]["duration_ms"], 60000)
        self.assertTrue(all(len(part["notes"]) <= 128 for part in excerpt["arrangement"]["parts"]))
        plan = arrangement_render_plan(arrangement)
        self.assertEqual(plan["chunks"][0]["start_ms"], 0)
        self.assertEqual(plan["chunks"][-1]["end_ms"], arrangement["duration_ms"])
        for previous, following in zip(plan["chunks"], plan["chunks"][1:]):
            self.assertEqual(previous["end_ms"], following["start_ms"])
        retained = {part["id"]: set() for part in arrangement["parts"]}
        for chunk in plan["chunks"]:
            for part in chunk["parts"]:
                self.assertLessEqual(len(part["score"]["notes"]), 128)
                self.assertLessEqual(part["score"]["duration_ms"], 18000)
                retained[part["id"]].update(part["source_note_indices"])
        for part in arrangement["parts"]:
            self.assertEqual(retained[part["id"]], set(range(len(part["notes"]))))
        self.assertEqual(source.read_bytes(), before)


class AuthenticatedHttpIntegration(RealMediaFixtures):
    def setUp(self):
        try:
            from flask import Flask
        except ImportError as exc:
            raise unittest.SkipTest("Flask is required for in-process HTTP route integration") from exc
        super().setUp()
        self.token = "isolated-http-fixture-secret-32-characters"
        self.environment = patch.dict(os.environ, {"OSAKA_CD_PUBLISH_ENABLED": "1",
                                                   "OSAKA_CD_PUBLISH_TOKEN": self.token})
        self.environment.start()
        from updater.api_cd import register_cd_publish_route
        self.app = Flask("cd-v9-isolated-http")
        self.app.extensions["osaka_cd_store"] = self.store
        register_cd_publish_route(self.app, lambda: lambda *args: self.fail("code updater must not be invoked"))
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/zip"}

    def tearDown(self):
        publisher = self.app.extensions.get("osaka_cd_publisher")
        if publisher:
            publisher.pool.shutdown(wait=True)
        self.environment.stop()
        super().tearDown()

    def test_auth_checks_apply_to_upload_status_and_rollback_without_leaking_private_paths(self):
        for method, path in (("post", "/api/osaka/cd/publish"),
                             ("get", "/api/osaka/cd/status"),
                             ("get", "/api/osaka/cd/jobs/" + "a" * 64),
                             ("post", "/api/osaka/cd/rollback")):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(self.token, response.get_data(as_text=True))
            self.assertNotIn(str(self.store.root), response.get_data(as_text=True))
        response = self.client.get("/api/osaka/cd/status", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("root", response.get_json())
        with patch.dict(os.environ, {"OSAKA_CD_PUBLISH_ENABLED": "0"}):
            self.assertEqual(self.client.get("/api/osaka/cd/status", headers=self.headers).status_code, 404)

    def test_http_receipt_stays_pending_until_hot_catalog_acks_then_retry_is_idempotent(self):
        from osaka_cd.events import subscribe
        done = threading.Event()
        stop = subscribe(lambda _: done.set())
        try:
            path = write_zip(self.folder / "http.zip", package_members("demo-song", self.audio, self.image))
            response = self.client.post("/api/osaka/cd/publish", data=path.read_bytes(), headers={
                **self.headers, "X-CD-Package-SHA256": digest(path.read_bytes()),
                "X-CD-Expected-Versions": json.dumps({"demo-song": None})})
            self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
            job = response.get_json()
            self.assertFalse(job["installed"])
            self.assertTrue(done.wait(timeout=20), "publication did not commit")
            applied = self.client.get(job["status_url"], headers=self.headers)
            self.assertEqual(applied.get_json()["state"], "applied")
            from cogs.tts import melodies
            with selected_store(self.store) as runtime:
                self.assertEqual(melodies.get_melody("demo-song").id, "demo-song")
                self.assertTrue(runtime.acknowledge())
            installed = self.client.get(job["status_url"], headers=self.headers)
            self.assertTrue(installed.get_json()["installed"])
            repeated = self.client.post("/api/osaka/cd/publish", data=path.read_bytes(), headers=self.headers)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.get_json()["job_id"], job["job_id"])
        finally:
            stop()

    def test_invalid_checksum_content_type_and_cas_are_visible_failures(self):
        path = write_zip(self.folder / "http-invalid.zip", package_members("demo-song", self.audio, self.image))
        wrong_type = self.client.post("/api/osaka/cd/publish", data=path.read_bytes(),
                                      headers={**self.headers, "Content-Type": "text/plain"})
        self.assertEqual(wrong_type.status_code, 415)
        wrong_hash = self.client.post("/api/osaka/cd/publish", data=path.read_bytes(),
                                      headers={**self.headers, "X-CD-Package-SHA256": "f" * 64})
        self.assertEqual(wrong_hash.status_code, 400)
        malformed = self.client.post("/api/osaka/cd/publish", data=path.read_bytes(),
                                     headers={**self.headers, "X-CD-Expected-Versions": "not json"})
        self.assertEqual(malformed.status_code, 400)
        missing = self.client.get("/api/osaka/cd/jobs/" + "b" * 64, headers=self.headers)
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
