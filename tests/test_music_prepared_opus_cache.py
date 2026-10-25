"""Prepared Opus cache contracts using actual temporary files and bounded quotas.

Cache failures must not become audio failures. These checks cover publication,
reuse, eviction and immutable media identity, without network or decoding.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.runtime_telefone.agente import configuracao as config
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState


class PreparedOpusCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="prepared-opus-cache-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "phone-worker/state/prepared-opus"
        self.store = config.PreparedOpusCache(self.folder, max_bytes=4096,
                                               max_item_bytes=1024, min_free_bytes=0)
        self.key = "a" * 64

    def publish(self, key, raw=b"validated-opus-test-payload", *, store=None):
        cache = store or self.store
        writer = cache.writer(key)
        self.assertIsNotNone(writer)
        self.assertEqual(writer.write(raw), len(raw))
        self.assertTrue(writer.commit())
        return cache.get(key)

    def test_incomplete_bytes_are_private_and_never_returned_as_a_hit(self):
        self.assertIsNone(self.store.get(self.key))
        writer = self.store.writer(self.key)
        self.assertIsNotNone(writer)
        self.addCleanup(writer.abort)
        writer.write(b"partial")
        self.assertIsNone(self.store.get(self.key))
        self.assertTrue(writer.active)
        files = [path for path in self.folder.iterdir() if path.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertNotEqual(files[0].suffix, ".ogg")

    def test_completed_entry_is_atomic_private_and_reusable_after_restart(self):
        raw = b"complete-validated-opus"
        path = self.publish(self.key, raw)
        self.assertEqual(path, self.folder / (self.key + ".ogg"))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        restarted = config.PreparedOpusCache(self.folder, max_bytes=4096,
                                               max_item_bytes=1024, min_free_bytes=0)
        self.assertEqual(restarted.get(self.key).read_bytes(), raw)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [self.key + ".ogg"])

    def test_cache_hit_does_not_read_entire_entry_before_starting_audio(self):
        path = self.publish(self.key)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("no full file read on hit")):
            self.assertEqual(self.store.get(self.key), path)

    def test_duplicate_writer_is_deduplicated_until_owner_aborts(self):
        first = self.store.writer(self.key)
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.writer(self.key))
        first.write(b"unfinished")
        first.abort()
        first.abort()
        self.assertFalse(first.active)
        self.assertIsNone(self.store.get(self.key))
        self.assertEqual(list(self.folder.iterdir()), [])
        next_writer = self.store.writer(self.key)
        self.assertIsNotNone(next_writer)
        next_writer.cleanup()

    def test_empty_commit_and_item_overflow_never_publish_partial_entries(self):
        writer = self.store.writer(self.key)
        self.assertIsNotNone(writer)
        self.assertFalse(writer.commit())
        self.assertIsNone(self.store.get(self.key))
        writer = self.store.writer(self.key)
        self.assertIsNotNone(writer)
        self.assertEqual(writer.write(b"x" * 1025), 1025)
        self.assertFalse(writer.active)
        self.assertEqual(writer.write(b"audio-keeps-flowing"), 19)
        self.assertFalse(writer.commit())
        self.assertIsNone(self.store.get(self.key))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_replace_failure_preserves_previous_entries_and_releases_writer(self):
        old_key, new_key = "b" * 64, "c" * 64
        old_path = self.publish(old_key, b"previous-valid-file")
        writer = self.store.writer(new_key)
        writer.write(b"new-complete-file")
        with mock.patch.object(config.os, "replace", side_effect=OSError("storage unavailable")):
            self.assertFalse(writer.commit())
        self.assertEqual(old_path.read_bytes(), b"previous-valid-file")
        self.assertEqual(self.store.get(old_key), old_path)
        self.assertIsNone(self.store.get(new_key))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [old_key + ".ogg"])
        retry = self.store.writer(new_key)
        self.assertIsNotNone(retry)
        retry.abort()

    def test_writer_setup_failure_closes_and_removes_its_temporary_file(self):
        with mock.patch.object(config.os, "fchmod", side_effect=OSError("permissions unavailable")):
            self.assertIsNone(self.store.writer(self.key))
        self.assertEqual(list(self.folder.iterdir()), [])
        retry = self.store.writer(self.key)
        self.assertIsNotNone(retry)
        retry.abort()

    def test_lru_eviction_keeps_recent_hit_within_total_budget(self):
        cache = config.PreparedOpusCache(self.folder, max_bytes=10,
                                           max_item_bytes=6, min_free_bytes=0)
        a, b, c = (str(i) * 64 for i in range(1, 4))
        pa, pb = self.publish(a, b"aaaa", store=cache), self.publish(b, b"bbbb", store=cache)
        os.utime(pa, (100, 100))
        os.utime(pb, (200, 200))
        self.assertEqual(cache.get(a), pa)
        self.publish(c, b"cccc", store=cache)
        self.assertIsNotNone(cache.get(a))
        self.assertIsNone(cache.get(b))
        self.assertIsNotNone(cache.get(c))
        self.assertLessEqual(sum(p.stat().st_size for p in self.folder.glob("*.ogg")), 10)

    def test_invalid_key_symlinks_empty_and_oversized_entries_are_not_hits(self):
        for invalid in ("", "../escape", "a" * 63, "A" * 64, "/tmp/audio", True, None):
            with self.subTest(key=invalid):
                self.assertIsNone(self.store.get(invalid))
                self.assertIsNone(self.store.writer(invalid))
        self.folder.mkdir(parents=True, exist_ok=True)
        outside = self.folder.parent / "original-music.ogg"
        outside.write_bytes(b"original-must-survive")
        path = self.folder / (self.key + ".ogg")
        path.symlink_to(outside)
        self.assertIsNone(self.store.get(self.key))
        self.assertEqual(outside.read_bytes(), b"original-must-survive")
        path.unlink(missing_ok=True)
        path.write_bytes(b"")
        self.assertIsNone(self.store.get(self.key))
        path.write_bytes(b"x" * 1025)
        self.assertIsNone(self.store.get(self.key))

    def test_disabled_or_low_disk_cache_does_not_prevent_audio_production(self):
        disabled = config.PreparedOpusCache(self.folder, max_bytes=0,
                                              max_item_bytes=1024, min_free_bytes=0)
        self.assertIsNone(disabled.writer(self.key))
        limited = config.PreparedOpusCache(self.folder, max_bytes=4096,
                                             max_item_bytes=1024, min_free_bytes=1024)
        with mock.patch.object(config.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            writer = limited.writer(self.key)
            if writer is not None:
                self.assertEqual(writer.write(b"audio"), 5)
                self.assertFalse(writer.commit())
        self.assertIsNone(limited.get(self.key))
        retry = limited.writer(self.key)
        self.assertIsNotNone(retry)
        retry.abort()

    def test_concurrent_guilds_only_obtain_one_writer_for_the_same_key(self):
        start = threading.Barrier(3)
        release, ready = threading.Event(), threading.Event()
        guard, obtained = threading.Lock(), []
        def obtain():
            start.wait(timeout=2)
            writer = self.store.writer(self.key)
            with guard:
                obtained.append(writer)
                if len(obtained) == 2:
                    ready.set()
            release.wait(timeout=2)
            if writer is not None:
                writer.abort()
        threads = [threading.Thread(target=obtain, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            start.wait(timeout=2)
            self.assertTrue(ready.wait(timeout=2))
            self.assertEqual(sum(writer is not None for writer in obtained), 1)
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_cache_directory_symlink_never_writes_into_original_collection(self):
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        originals = self.folder.parent / "originals"
        originals.mkdir()
        original = originals / (self.key + ".ogg")
        original.write_bytes(b"original-music")
        self.folder.symlink_to(originals, target_is_directory=True)
        self.assertIsNone(self.store.get(self.key))
        self.assertIsNone(self.store.writer(self.key))
        self.assertEqual(original.read_bytes(), b"original-music")

    def test_restart_removes_only_dead_process_temporary_files(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        orphan = self.folder / ".opus-prepare-999999-dead.tmp"
        orphan.write_bytes(b"unfinished-orphan")
        with mock.patch.object(config.os, "kill", side_effect=ProcessLookupError("dead process")):
            path = self.publish(self.key, b"new-valid-entry")
        self.assertEqual(path.read_bytes(), b"new-valid-entry")
        self.assertFalse(orphan.exists())

    def test_other_live_process_temporary_file_is_preserved_and_counted_in_budget(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        foreign = self.folder / ".opus-prepare-424242-live.tmp"
        foreign.write_bytes(b"abcdef")
        cache = config.PreparedOpusCache(self.folder, max_bytes=10,
                                           max_item_bytes=8, min_free_bytes=0)
        with mock.patch.object(config.os, "kill", return_value=None):
            writer = cache.writer(self.key)
            if writer is not None:
                self.assertEqual(writer.write(b"123456"), 6)
                self.assertFalse(writer.commit())
        self.assertEqual(foreign.read_bytes(), b"abcdef")
        self.assertIsNone(cache.get(self.key))


class PreparedOpusIdentityTests(unittest.TestCase):
    def test_quality_and_protection_parameters_are_part_of_the_cache_identity(self):
        track = self.track()
        original = config.prepared_opus_cache_key(track, 256)
        self.assertIsNotNone(original)
        for settings in ({'resample_filter': 'aresample=48000:filter_size=64'},
                         {'fec': False}, {'expected_packet_loss': .05}):
            with self.subTest(settings=settings):
                self.assertNotEqual(config.prepared_opus_cache_key(track, 256, **settings), original)
        for invalid in ({'fec': 'yes'}, {'expected_packet_loss': True},
                        {'expected_packet_loss': float('nan')}, {'resample_filter': 'x' * 2049}):
            self.assertIsNone(config.prepared_opus_cache_key(track, 256, **invalid))

    def track(self):
        return AgentTrack(webpage_url="https://www.youtube.com/watch?v=abcdefghijk",
                          original_url="https://www.youtube.com/watch?v=abcdefghijk",
                          stream_url="https://cdn.invalid/audio?expire=1&signature=old",
                          audio_format_id="140", audio_codec="aac", audio_stream_index=0,
                          audio_sample_rate=44100, audio_channels=2, duration=195.0)

    def test_signed_stream_expiry_and_requester_changes_do_not_change_stable_key(self):
        track = self.track()
        before = config.prepared_opus_cache_key(track, 256)
        self.assertRegex(before, r"^[a-f0-9]{64}$")
        track.stream_url = "https://different-cdn.invalid/changed?expire=2&signature=new"
        track.requester_id = 100
        track.queue_item_id = "different-session"
        track.title = "edited title"
        self.assertEqual(config.prepared_opus_cache_key(track, 256), before)
        self.assertNotEqual(config.prepared_opus_cache_key(track, 192), before)
        track.audio_format_id = "251"
        self.assertNotEqual(config.prepared_opus_cache_key(track, 256), before)

    def test_archive_key_includes_immutable_part_identity_and_hash(self):
        track = self.track()
        track.archive_key = "a" * 32
        track.archive_ref = dict(guild_id=1, channel_id=2, message_id=3, attachment_id=4)
        track.archive_segments = [{**track.archive_ref, "order": 0, "duration": 195.0,
                                   "offset_seconds": 0.0, "size_bytes": 1000, "sha256": "b" * 64,
                                   "filename": "original.m4a", "audio_codec": "aac",
                                   "audio_sample_rate": 44100, "audio_channels": 2}]
        before = config.prepared_opus_cache_key(track, 256)
        self.assertRegex(before, r"^[a-f0-9]{64}$")
        track.archive_segments[0]["url"] = "https://cdn.invalid/audio?signature=changed"
        self.assertEqual(config.prepared_opus_cache_key(track, 256), before)
        track.archive_segments[0]["sha256"] = "c" * 64
        self.assertNotEqual(config.prepared_opus_cache_key(track, 256), before)
        track.archive_segments[0]["attachment_id"] += 1
        self.assertNotEqual(config.prepared_opus_cache_key(track, 256), before)

    def test_unstable_live_seek_and_invalid_encoder_modes_are_not_cacheable(self):
        for bitrate in (None, True, 0, -1, 513, float("nan"), "256"):
            with self.subTest(bitrate=bitrate):
                self.assertIsNone(config.prepared_opus_cache_key(self.track(), bitrate))
        for patch in ({"is_live": True}, {"start_offset_seconds": 1.0},
                      {"webpage_url": "", "original_url": "", "stream_url": "https://cdn.invalid/signed"}):
            with self.subTest(patch=patch):
                track = self.track()
                track.__dict__.update(copy.deepcopy(patch))
                self.assertIsNone(config.prepared_opus_cache_key(track, 256))

    def test_unknown_mutable_page_url_does_not_reuse_potentially_changed_content(self):
        track = self.track()
        track.webpage_url = "https://media.invalid/latest.mp3"
        track.original_url = track.webpage_url
        self.assertIsNone(config.prepared_opus_cache_key(track, 256))


class PreparedOpusEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.agent = SimpleNamespace(audio_output_default_opus_direct="auto", ffmpeg_options="-vn")
        self.state = GuildMusicState(1)
        self.track = AgentTrack(audio_codec="aac", audio_sample_rate=44100,
                                audio_channels=1, audio_abr=128)

    def eligibility(self, **kwargs):
        options = {"volume_percent": 100, "output_bitrate_kbps": 256, "state": self.state, **kwargs}
        return config.opus_preparation_eligibility(self.agent, self.track, **options)

    def test_source_conversion_may_prepare_aac_mono_44100_but_is_not_copy_proof(self):
        result = self.eligibility()
        self.assertTrue(result["candidate"])
        self.assertFalse(result["eligible"])
        self.assertTrue(result["packet_validation_required"])

    def test_processing_and_unknown_capacity_guards_remain_active(self):
        for reason, options in (("volume_requires_pcm", {"volume_percent": 55}),
                                ("effects_require_pcm", {"effects": (0, 1, 0)}),
                                ("pcm_mixer_required", {"force_pcm": True})):
            with self.subTest(reason=reason):
                result = self.eligibility(**options)
                self.assertFalse(result["candidate"])
                self.assertIn(reason, result["reasons"])
        for bitrate in (0, None, True, -1, float("nan")):
            with self.subTest(bitrate=bitrate):
                self.assertFalse(self.eligibility(output_bitrate_kbps=bitrate)["candidate"])
        self.track.is_live = True
        self.assertFalse(self.eligibility()["candidate"])
        self.track.is_live = False
        self.track.start_offset_seconds = 1
        self.assertFalse(self.eligibility()["candidate"])


if __name__ == "__main__":
    unittest.main()
