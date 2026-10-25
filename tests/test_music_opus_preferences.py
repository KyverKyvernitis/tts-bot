"""Persistent Opus mode with real temporary files; no installed env is read."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
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


class OpusPreferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="opus-preferences-check-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "phone-worker/state/output-preferences.json"
        self.store = config.OpusDirectPreferenceStore(self.path)

    def write_raw(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())

    def payload(self, guilds):
        return {"schema": self.store.SCHEMA, "guilds": guilds}

    def test_missing_file_is_ready_and_constructor_does_not_create_anything(self):
        self.assertTrue(self.store.ready)
        self.assertFalse(self.path.parent.exists())
        self.assertIsNone(self.store.mode(1))
        self.assertEqual(self.store.snapshot(), {})
        self.assertEqual(self.store.status(1), {"persistent": True, "ready": True,
                                              "stored": False, "mode": None, "error_type": ""})

    def test_modes_survive_a_new_store_and_guilds_are_isolated(self):
        self.store.set_mode(1, "auto")
        self.store.set_mode(2, "off")
        loaded = config.OpusDirectPreferenceStore(self.path)
        self.assertEqual(loaded.snapshot(), {"1": "auto", "2": "off"})
        self.assertEqual(loaded.mode(1), "auto")
        self.assertEqual(loaded.mode("2"), "off")
        self.assertIsNone(loaded.mode(3))
        self.assertEqual(json.loads(self.path.read_bytes()), self.payload({"1": "auto", "2": "off"}))

    def test_snapshot_cannot_mutate_persistent_map(self):
        self.store.set_mode(1, "off")
        snapshot = self.store.snapshot()
        snapshot["1"] = "auto"
        snapshot["2"] = "off"
        self.assertEqual(self.store.snapshot(), {"1": "off"})

    def test_reset_removes_only_one_guild_and_survives_reload(self):
        self.store.set_mode(1, "auto")
        self.store.set_mode(2, "off")
        self.store.set_mode(1, None)
        self.assertEqual(config.OpusDirectPreferenceStore(self.path).snapshot(), {"2": "off"})

    def test_only_opus_modes_and_valid_snowflakes_are_allowed(self):
        for guild in (0, -1, True, False, 1.5, "x", "", 2 ** 64):
            with self.subTest(guild=guild), self.assertRaises(ValueError):
                self.store.set_mode(guild, "auto")
        for mode in (True, False, "on", "AUTO", "", 1, {}, []):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.store.set_mode(1, mode)
        self.assertFalse(self.path.exists())

    def test_written_file_and_backup_are_private_regular_files(self):
        self.store.set_mode(1, "off")
        before = self.path.read_bytes()
        self.store.set_mode(2, "auto")
        self.assertEqual(self.store.backup_path.read_bytes(), before)
        for path in (self.path, self.store.backup_path):
            self.assertTrue(stat.S_ISREG(path.stat().st_mode))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_atomic_replace_failure_preserves_old_file_and_cache(self):
        self.store.set_mode(1, "off")
        before = self.path.read_bytes()
        replace = os.replace
        def fail_main(source, target):
            if Path(target) == self.path:
                raise OSError("simulated storage failure")
            return replace(source, target)
        with mock.patch.object(config.os, "replace", side_effect=fail_main), self.assertRaises(OSError):
            self.store.set_mode(2, "auto")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot(), {"1": "off"})
        self.assertEqual(self.store.backup_path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".opus-preferences-*")), [])

    def test_failure_of_new_file_does_not_announce_persistence(self):
        with mock.patch.object(config.os, "replace", side_effect=OSError("simulated")), self.assertRaises(OSError):
            self.store.set_mode(1, "auto")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.store.status(1)["stored"])
        self.assertEqual(self.store.snapshot(), {})

    def test_idempotent_command_does_not_rewrite_and_reset_missing_guild_does_not_create(self):
        self.store.set_mode(2, None)
        self.assertFalse(self.path.parent.exists())
        self.store.set_mode(1, "auto")
        with mock.patch.object(self.store, "_atomic_write", side_effect=AssertionError("must not rewrite")):
            self.store.set_mode(1, "auto")

    def test_corrupt_file_is_preserved_read_only_without_enabling_copy(self):
        cases = (b"not-json", b'\xff', self.payload({"1": "wrong"}),
                 {"schema": "future-v2", "guilds": {"1": "off"}},
                 self.payload({"01": "off"}), self.payload({"0": "auto"}),
                 {"schema": self.store.SCHEMA, "guilds": {}, "token": "never accepted"},
                 ('{"schema":"' + self.store.SCHEMA + '","guilds":{"1":"off","1":"auto"}}').encode())
        for raw in cases:
            with self.subTest(raw=raw):
                self.write_raw(raw)
                before = self.path.read_bytes()
                loaded = config.OpusDirectPreferenceStore(self.path)
                self.assertFalse(loaded.ready)
                self.assertTrue(loaded.status()["error_type"])
                self.assertIsNone(loaded.mode(1))
                self.assertEqual(loaded.snapshot(), {})
                current = SimpleNamespace(opus_direct_preferences=loaded, audio_output_default_opus_direct="auto")
                self.assertEqual(config.effective_opus_direct_mode(current, GuildMusicState(1)), "off")
                with self.assertRaises(RuntimeError):
                    loaded.set_mode(2, "auto")
                self.assertEqual(self.path.read_bytes(), before)

    def test_oversized_file_and_excessive_guild_count_are_rejected(self):
        self.write_raw(b" " * (self.store.MAX_BYTES + 1))
        self.assertFalse(config.OpusDirectPreferenceStore(self.path).ready)
        self.write_raw(self.payload({str(index): "off" for index in range(1, self.store.MAX_GUILDS + 2)}))
        self.assertFalse(config.OpusDirectPreferenceStore(self.path).ready)

    def test_link_is_not_followed_or_overwritten(self):
        target = Path(self.temporary.name) / "other-private-file"
        target.write_text("sentinel")
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(target)
        loaded = config.OpusDirectPreferenceStore(self.path)
        self.assertFalse(loaded.ready)
        with self.assertRaises(RuntimeError):
            loaded.set_mode(1, "auto")
        self.assertEqual(target.read_text(), "sentinel")
        self.assertTrue(self.path.is_symlink())

    def test_reload_after_repair_restores_explicit_preferences(self):
        self.write_raw(b"bad")
        self.store.load()
        self.assertFalse(self.store.ready)
        self.write_raw(self.payload({"7": "off"}))
        self.store.load()
        self.assertTrue(self.store.ready)
        self.assertEqual(self.store.mode(7), "off")
        self.assertEqual(self.store.error_type, "")

    def test_concurrent_guild_updates_share_one_serialized_map(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda guild: self.store.set_mode(guild, "auto" if guild % 2 else "off"), range(1, 31)))
        expected = {str(guild): "auto" if guild % 2 else "off" for guild in range(1, 31)}
        self.assertEqual(config.OpusDirectPreferenceStore(self.path).snapshot(), expected)

    def test_directory_fsync_failure_after_commit_does_not_leave_cache_stale(self):
        original = os.fsync
        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("simulated unsupported directory fsync")
            original(fd)
        with mock.patch.object(config.os, "fsync", side_effect=fsync):
            self.store.set_mode(1, "off")
        self.assertEqual(self.store.mode(1), "off")
        self.assertEqual(config.OpusDirectPreferenceStore(self.path).mode(1), "off")

    def test_audio_policy_reader_does_not_wait_for_a_slow_preference_write(self):
        self.store.set_mode(1, "off")
        entered, release, reader_done = threading.Event(), threading.Event(), threading.Event()
        original = self.store._atomic_write
        errors, observed = [], []
        current = SimpleNamespace(opus_direct_preferences=self.store,
                                  audio_output_default_opus_direct="auto")
        voice = SimpleNamespace(channel=SimpleNamespace(bitrate=256000))
        state = GuildMusicState(1)
        def slow_write(path, raw):
            if path == self.path:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release preference writer")
            return original(path, raw)
        def write_mode():
            try:
                self.store.set_mode(1, "auto")
            except Exception as error:
                errors.append(error)
        def read_policy():
            try:
                observed.append((self.store.mode(1),
                                 config.effective_opus_direct_mode(current, state),
                                 config.audio_output_policy(current, voice, state=state)["opus_direct"]))
            except Exception as error:
                errors.append(error)
            finally:
                reader_done.set()
        with mock.patch.object(self.store, "_atomic_write", side_effect=slow_write), \
             mock.patch.object(self.store, "_read", side_effect=AssertionError("no disk read on audio policy")):
            writer = threading.Thread(target=write_mode, daemon=True)
            reader = threading.Thread(target=read_policy, daemon=True)
            writer.start()
            try:
                self.assertTrue(entered.wait(1), "writer did not reach slow filesystem boundary")
                reader.start()
                self.assertTrue(reader_done.wait(.2), "audio policy blocked behind preference fsync")
                self.assertEqual(observed, [("off", "off", "off")])
            finally:
                release.set()
                writer.join(2)
                if reader.ident is not None:
                    reader.join(2)
        self.assertFalse(writer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.mode(1), "auto")
        self.assertEqual(config.effective_opus_direct_mode(current, state), "auto")

    def test_corrupt_reload_publishes_disabled_view_without_using_old_mode(self):
        self.store.set_mode(1, "auto")
        self.assertEqual(self.store.read_mode(1), (True, "auto"))
        self.path.write_bytes(b"corrupt after startup")
        self.store.load()
        self.assertEqual(self.store.read_mode(1), (False, None))
        self.assertFalse(self.store.ready)
        current = SimpleNamespace(opus_direct_preferences=self.store,
                                  audio_output_default_opus_direct="auto")
        self.assertEqual(config.effective_opus_direct_mode(current, GuildMusicState(1)), "off")


class EffectiveOpusPreferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="opus-effective-preferences-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "preferences.json"
        self.store = config.OpusDirectPreferenceStore(self.path)
        self.agent = SimpleNamespace(opus_direct_preferences=self.store,
                                     audio_output_default_opus_direct="auto")

    def test_global_auto_is_default_and_explicit_installed_off_is_honored(self):
        self.assertEqual(config.effective_opus_direct_mode(SimpleNamespace()), "auto")
        self.agent.audio_output_default_opus_direct = "off"
        self.assertEqual(config.effective_opus_direct_mode(self.agent, GuildMusicState(1)), "off")

    def test_session_then_stored_guild_then_global_precedence(self):
        self.store.set_mode(1, "off")
        self.assertEqual(config.effective_opus_direct_mode(self.agent, GuildMusicState(1)), "off")
        self.assertEqual(config.effective_opus_direct_mode(self.agent, GuildMusicState(2)), "auto")
        state = GuildMusicState(1, audio_output_settings={"opus_direct": "auto"})
        self.assertEqual(config.effective_opus_direct_mode(self.agent, state), "auto")
        self.assertEqual(config.effective_opus_direct_mode(self.agent, guild_id=1), "off")

    def test_persisted_override_survives_restart_and_new_state_after_idle_pruning(self):
        self.store.set_mode(1, "off")
        self.agent.opus_direct_preferences = config.OpusDirectPreferenceStore(self.path)
        for _ in range(2):
            state = GuildMusicState(1)
            self.assertEqual(config.effective_opus_direct_mode(self.agent, state), "off")
            self.assertEqual(state.audio_output_settings, {})

    def test_reset_returns_global_default_without_persisting_profile_or_protection(self):
        self.store.set_mode(1, "off")
        self.store.set_mode(1, None)
        self.assertEqual(config.effective_opus_direct_mode(self.agent, GuildMusicState(1)), "auto")
        self.assertEqual(json.loads(self.path.read_bytes()), {"schema": self.store.SCHEMA, "guilds": {}})

    def test_policy_and_eligibility_use_stored_mode_without_file_reads(self):
        self.store.set_mode(1, "off")
        voice = SimpleNamespace(channel=SimpleNamespace(bitrate=256000))
        track = AgentTrack(audio_codec="opus", audio_sample_rate=48000, audio_channels=2, audio_abr=128)
        state = GuildMusicState(1)
        with mock.patch.object(self.store, "_read", side_effect=AssertionError("no filesystem on policy read")):
            self.assertEqual(config.audio_output_policy(self.agent, voice, track, state)["opus_direct"], "off")
            result = config.opus_passthrough_eligibility(self.agent, track, state=state, volume_percent=100)
        self.assertFalse(result["candidate"])
        self.assertIn("disabled", result["reasons"])
        self.assertEqual(state.volume_percent, 55)

    def test_invalid_state_override_is_rejected_not_silently_persisted(self):
        state = GuildMusicState(1, audio_output_settings={"opus_direct": "on"})
        with self.assertRaises(ValueError):
            config.effective_opus_direct_mode(self.agent, state)
        self.assertFalse(self.path.exists())

    def test_capabilities_reflect_global_default_and_preserve_all_music_defaults(self):
        self.assertEqual(config.audio_output_capabilities()["opus_direct_default"], "auto")
        self.assertEqual(config.audio_output_capabilities("off")["opus_direct_default"], "off")
        result = config.audio_output_policy(self.agent, SimpleNamespace(channel=SimpleNamespace(bitrate=256000)))
        self.assertEqual(result["bitrate_kbps"], 160)
        self.assertTrue(result["fec"])
        self.assertEqual(result["expected_packet_loss"], .15)


if __name__ == "__main__":
    unittest.main()
