from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from updater.utilitarios.cd_packages import contract, prepare_cd_overlay, validate_candidate_tree


class CDPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-cd-package-")
        self.root = Path(self.temp.name)
        self.module = contract()

    def tearDown(self):
        self.temp.cleanup()

    def score(self, identity="test-song", revision=1, pitch=60):
        entry = {"id": identity, "name": "Test song", "artist": "Test", "excerpt": "test",
                 "source_url": "", "source_sha256": "a" * 64, "duration_ms": 1000,
                 "notes": [{"midi_note": pitch, "start_ms": 0, "duration_ms": 1000, "velocity": 100}],
                 "revision": revision}
        entry["score_sha256"] = self.module.score_sha256(entry)
        return entry

    def write(self, root, rel, value):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def install(self, root, entry):
        self.write(root, self.module.SCORES, [entry])
        audio = root / "cogs/tts/cd/previews" / f"{entry['id']}.mp3"
        audio.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-c:a", "libmp3lame", str(audio)], check=True)
        preview = {"manifest_version": 2, "score_sha256": entry["score_sha256"],
                   "source_sha256": entry["source_sha256"], "mp3_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                   "voice_id": "kasane-teto", "language": "pt-BR", "pitch_semitones": 0, "speech_rate": 1.0}
        self.write(root, self.module.PREVIEWS, {entry["id"]: preview})
        from PIL import Image
        data = io.BytesIO()
        Image.new("RGB", (32, 32), (200, 20, 30)).save(data, "JPEG")
        image = root / "cogs/tts/cd/thumbnails/abcdefghijk.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(data.getvalue())
        self.write(root, self.module.VIDEOS, {"version": 1, "songs": {entry["id"]: {
            "video_id": "abcdefghijk", "thumbnail_sha256": hashlib.sha256(data.getvalue()).hexdigest()}}})

    def archive(self, root, name="package.zip"):
        archive = self.root / name
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            for source in root.rglob("*"):
                if source.is_file():
                    handle.write(source, source.relative_to(root).as_posix())
        return archive

    def test_complete_package_decodes_audio_and_hashes_cover(self):
        source = self.root / "source"
        self.install(source, self.score())
        report = self.module.validate_package(self.archive(source), require_complete=True, audio_probe=True)
        self.assertEqual(report["artifacts"]["test-song"]["audio"]["verified"], "decoded")
        (source / "cogs/tts/cd/thumbnails/abcdefghijk.jpg").write_bytes(b"invalid")
        with self.assertRaises(self.module.CDPackageError):
            self.module.validate_package(self.archive(source, "tampered.zip"), require_complete=True)

    def test_pending_thumbnail_with_ready_audio_remains_valid_partial_package(self):
        source = self.root / "source"
        self.install(source, self.score())
        (source / "cogs/tts/cd/thumbnails/abcdefghijk.jpg").unlink()
        self.write(source, self.module.VIDEOS, {"version": 1, "songs": {"test-song": {
            "video_id": "abcdefghijk", "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"}}})
        report = self.module.validate_package(self.archive(source), audio_probe=True)
        self.assertEqual(report["artifacts"]["test-song"]["thumbnail"], "pending")
        self.assertEqual(report["artifacts"]["test-song"]["sample"], "ready")

    def test_legacy_orphan_preview_does_not_block_unrelated_increment(self):
        base, overlay = self.root / "base", self.root / "overlay"
        self.install(base, self.score())
        previews = self.module._map(base, self.module.PREVIEWS)[1]
        previews["rolling-girl"] = {"score_sha256": "b" * 64, "voice_id": "kasane-teto"}
        self.write(base, self.module.PREVIEWS, previews)
        self.write(overlay, self.module.SCORES, [self.score("second-song")])
        self.write(overlay, self.module.PREVIEWS, {})
        files = [(overlay / rel, Path(rel)) for rel in (self.module.SCORES, self.module.PREVIEWS)]
        prepare_cd_overlay(files, base)
        self.assertIn("rolling-girl", self.module._map(overlay, self.module.PREVIEWS)[1])

    def test_merge_preserves_other_cds_and_cover_update_keeps_sample(self):
        base, overlay = self.root / "base", self.root / "overlay"
        self.install(base, self.score())
        self.write(overlay, self.module.SCORES, [self.score("second-song")])
        self.module.merge_cd_overlay(overlay, base)
        self.assertEqual(set(self.module._scores(overlay)), {"test-song", "second-song"})
        cover = self.root / "cover"
        self.write(cover, self.module.VIDEOS, {"songs": {"test-song": {"video_id": "abcdefghijk", "title": "updated"}}})
        merged = self.module.merge_cd_overlay(cover, base)
        self.assertNotIn(self.module.PREVIEWS, merged)
        report = self.module.validate_package(self.archive(cover), base, require_complete=True, audio_probe=True)
        self.assertEqual(report["artifacts"]["test-song"]["sample"], "ready")

    def test_changed_score_invalidates_sample_without_deleting_bytes(self):
        base, overlay = self.root / "base", self.root / "overlay"
        self.install(base, self.score())
        self.write(overlay, self.module.SCORES, [self.score(revision=2, pitch=62)])
        merged = self.module.merge_cd_overlay(overlay, base)
        self.assertIn(self.module.PREVIEWS, merged)
        self.assertEqual(self.module._map(overlay, self.module.PREVIEWS)[1], {})
        self.assertTrue((base / "cogs/tts/cd/previews/test-song.mp3").is_file())

    def test_updater_prepares_merged_candidate_in_isolation(self):
        base, overlay = self.root / "base", self.root / "overlay"
        self.install(base, self.score())
        before = (base / self.module.SCORES).read_bytes()
        self.write(overlay, self.module.SCORES, [self.score(revision=2, pitch=62)])
        files = prepare_cd_overlay([(overlay / self.module.SCORES, Path(self.module.SCORES))], base)
        self.assertEqual({rel.as_posix() for _, rel in files}, {self.module.SCORES, self.module.PREVIEWS})
        self.assertTrue(all(source.is_file() for source, _ in files))
        self.assertEqual((base / self.module.SCORES).read_bytes(), before)
        self.assertEqual(self.module._map(overlay, self.module.PREVIEWS)[1], {})

    def test_worktree_validation_uses_git_base_and_pending_new_score(self):
        root = self.root / "git-candidate"
        self.install(root, self.score())
        for arguments in (["init", "-q"], ["config", "user.name", "CD test"],
                          ["config", "user.email", "cd-test@example.invalid"], ["add", "."],
                          ["commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
        self.write(root, self.module.SCORES, [self.score(revision=2, pitch=62)])
        self.write(root, self.module.PREVIEWS, {})
        report = validate_candidate_tree(root, [self.module.SCORES, self.module.PREVIEWS])
        self.assertEqual(report["artifacts"]["test-song"]["sample"], "pending")
        self.assertEqual(report["artifacts"]["test-song"]["thumbnail"], "ready")

    def test_changed_score_requires_revision_and_rejects_stale_sample(self):
        base, overlay = self.root / "base", self.root / "overlay"
        self.install(base, self.score())
        self.write(overlay, self.module.SCORES, [self.score(pitch=62)])
        with self.assertRaises(self.module.CDPackageError):
            self.module.merge_cd_overlay(overlay, base)
        self.write(overlay, self.module.SCORES, [self.score(revision=2, pitch=62)])
        self.write(overlay, self.module.PREVIEWS, self.module._map(base, self.module.PREVIEWS)[1])
        with self.assertRaises(self.module.CDPackageError):
            self.module.validate_package(self.archive(overlay), base, audio_probe=True)

    def test_rejects_legacy_manifest_and_zip_traversal(self):
        source = self.root / "source"
        self.install(source, self.score())
        preview = self.module._map(source, self.module.PREVIEWS)[1]
        del preview["test-song"]["manifest_version"]
        self.write(source, self.module.PREVIEWS, preview)
        with self.assertRaises(self.module.CDPackageError):
            self.module.validate_package(self.archive(source))
        archive = self.root / "traversal.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../outside", "no")
        with self.assertRaises(self.module.CDPackageError):
            self.module.validate_package(archive)

    def test_ingress_rejects_code_even_under_cd_directory(self):
        archive = self.root / "code.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("cogs/tts/cd/previews/evil.py", "raise RuntimeError()")
        with self.assertRaises(self.module.CDPackageError):
            self.module.validate_package(archive, require_cd_only=True)


if __name__ == "__main__":
    unittest.main()
