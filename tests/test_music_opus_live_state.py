"""Current playback evidence and revision for the Opus direto panel label.

State methods are production code; the transport/source boundary is explicit.
These checks do not claim a real Discord session or audible-quality result.
"""
from __future__ import annotations

from copy import copy
from types import SimpleNamespace
import unittest

from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState


class LiveOpusStateTests(unittest.TestCase):
    def setUp(self):
        self.track = AgentTrack(queue_item_id="current", audio_codec="opus")
        self.music = object()
        self.live = {"copy_active": True, "copied_packets": 12, "encoded_packets": 0}
        self.source = SimpleNamespace(
            _closed=False, music_ended=False, last_read_had_music=True,
            music_source=self.music, last_read_music_source=self.music,
            opus_direct_status=lambda: dict(self.live))
        self.trace = {"track": self.track, "token": 3, "guild_id": 1,
                      "source": self.source, "expected_music_source": self.music,
                      "first_packet_monotonic": 0.1}
        self.voice = SimpleNamespace(
            source=self.source, channel=SimpleNamespace(id=2), connected=True,
            is_connected=lambda: True, is_playing=lambda: True,
            _music_packet_trace=self.trace)
        self.state = GuildMusicState(guild_id=1, voice_channel_id=2, current=self.track,
                                     transport="direct", status="playing", player=self.voice,
                                     playback_token=3, updated_at=100)

    def test_current_payload_has_live_flag_without_polluting_track_or_queue(self):
        queued = AgentTrack(queue_item_id="queued", audio_codec="opus")
        self.state.queue = [queued]
        payload = self.state.public()
        self.assertIs(payload["current"]["opus_direct_active"], True)
        self.assertNotIn("opus_direct_active", self.track.public())
        self.assertNotIn("opus_direct_active", payload["queue"][0])
        for field in ("opus_direct_origin", "opus_direct_bitrate_kbps"):
            self.assertNotIn(field, self.track.public())
            self.assertNotIn(field, payload["queue"][0])

    def prepared_source(self, bitrate=256):
        self.track.audio_codec, self.track.audio_abr = "aac", 128
        metadata = {"prepared_opus": True, "prepared_bitrate_kbps": bitrate,
                    "prepared_source_codec": "aac", "prepared_cache_hit": False}
        self.music = SimpleNamespace(opus_metadata=metadata)
        self.source.music_source = self.source.last_read_music_source = self.music
        self.trace["expected_music_source"] = self.music
        return metadata

    def test_prepared_aac_exposes_verified_output_without_rewriting_original_metadata(self):
        self.prepared_source()
        current = self.state.public()["current"]
        self.assertTrue(current["opus_direct_active"])
        self.assertEqual(current["opus_direct_origin"], "prepared")
        self.assertEqual(current["opus_direct_bitrate_kbps"], 256)
        self.assertEqual(current["audio_codec"], "aac")
        self.assertEqual(current["audio_abr"], 128)
        self.assertEqual(self.track.audio_codec, "aac")
        self.assertEqual(self.track.audio_abr, 128)

    def test_missing_or_invalid_prepared_provenance_never_invents_output_bitrate(self):
        for bitrate in (None, True, "256", 0, 15, 513, float("nan")):
            with self.subTest(bitrate=bitrate):
                self.prepared_source(bitrate)
                current = self.state.public()["current"]
                self.assertEqual(current["opus_direct_origin"], "")
                self.assertEqual(current["opus_direct_bitrate_kbps"], 0)
        for marker in (None, 1, "true", False):
            with self.subTest(marker=marker):
                metadata = self.prepared_source()
                metadata["prepared_opus"] = marker
                self.assertEqual(self.state.public()["current"]["opus_direct_origin"], "")

    def test_prepared_revision_tracks_static_provenance_and_not_packet_average_or_cache_hit(self):
        metadata = self.prepared_source()
        revision = self.state.state_revision()
        self.assertTrue(revision.endswith(":opus_direct:prepared256"))
        metadata["prepared_cache_hit"] = True
        self.live.update(copied_packets=1000, source_bitrate_kbps=255.9)
        self.assertEqual(self.state.state_revision(), revision)
        metadata["prepared_bitrate_kbps"] = 192
        self.assertNotEqual(self.state.state_revision(), revision)
        self.assertTrue(self.state.state_revision().endswith(":prepared192"))
        metadata["prepared_opus"] = False
        self.track.audio_codec = "opus"
        self.assertEqual(self.state.public()["current"]["opus_direct_origin"], "original")
        self.assertTrue(self.state.state_revision().endswith(":opus_direct"))

    def test_fallback_and_pause_clear_every_prepared_output_field(self):
        self.prepared_source()
        self.live["copy_active"] = False
        current = self.state.public()["current"]
        self.assertEqual((current["opus_direct_active"], current["opus_direct_origin"],
                          current["opus_direct_bitrate_kbps"]), (False, "", 0))
        self.live["copy_active"] = True
        self.state.paused = True
        self.assertEqual(self.state._opus_direct_public(),
                         {"opus_direct_active": False, "opus_direct_origin": "",
                          "opus_direct_bitrate_kbps": 0, "opus_processed_active": False,
                          "opus_processed_bitrate_kbps": 0})

    def test_source_replaced_while_reading_preparation_metadata_fails_closed(self):
        self.prepared_source()
        owner = self
        class Replaced:
            @property
            def opus_metadata(self):
                owner.voice.source = object()
                return {"prepared_opus": True, "prepared_bitrate_kbps": 256}
        self.music = Replaced()
        self.source.music_source = self.source.last_read_music_source = self.music
        self.trace["expected_music_source"] = self.music
        self.assertEqual(self.state._opus_direct_public(),
                         {"opus_direct_active": False, "opus_direct_origin": "",
                          "opus_direct_bitrate_kbps": 0, "opus_processed_active": False,
                          "opus_processed_bitrate_kbps": 0})

    def test_enabled_setting_and_fallback_encoder_never_prove_copy(self):
        self.state.audio_output_settings = {"opus_direct": "auto"}
        self.state.audio_output_applied = {"opus_direct": "auto", "state": "applied"}
        for actual in (False, None, 1, "true"):
            with self.subTest(actual=actual):
                self.live["copy_active"] = actual
                self.assertFalse(self.state._opus_direct_active())
                self.assertIs(self.state.public()["current"]["opus_direct_active"], False)

    def test_revision_changes_only_with_live_mode_not_packet_counters(self):
        direct = self.state.state_revision()
        self.assertTrue(direct.endswith(":opus_direct"))
        self.live["copied_packets"] += 50
        self.assertEqual(self.state.state_revision(), direct)
        self.live["copy_active"] = False
        self.assertEqual(self.state.state_revision(), "100.000000:3")
        self.live["encoded_packets"] += 50
        self.assertEqual(self.state.state_revision(), "100.000000:3")

    def test_revision_preserves_audio_settings_revision(self):
        self.state.audio_output_revision = 7
        self.assertEqual(self.state.state_revision(), "100.000000:3:audio7:opus_direct")
        self.live["copy_active"] = False
        self.assertEqual(self.state.state_revision(), "100.000000:3:audio7")

    def test_paused_starting_idle_disconnected_recovering_are_not_direct(self):
        scenarios = (("paused", True), ("starting", False), ("preparing", False),
                     ("idle", False), ("failed", False))
        for status, paused in scenarios:
            with self.subTest(status=status):
                self.state.status, self.state.paused = status, paused
                self.assertFalse(self.state._opus_direct_active())
        self.state.status, self.state.paused = "playing", False
        self.voice.is_connected = lambda: False
        self.assertFalse(self.state._opus_direct_active())
        self.voice.is_connected = lambda: True
        self.state.voice_runtime_recovery_pending = True
        self.assertFalse(self.state._opus_direct_active())

    def test_stale_track_generation_guild_source_and_music_are_rejected(self):
        for field, value in (("track", copy(self.track)), ("token", 2), ("guild_id", 9),
                             ("source", object()), ("expected_music_source", object()),
                             ("first_packet_monotonic", None)):
            with self.subTest(field=field):
                previous = self.trace[field]
                self.trace[field] = value
                self.assertFalse(self.state._opus_direct_active())
                self.trace[field] = previous
        self.assertTrue(self.state._opus_direct_active())
        self.source.last_read_music_source = object()
        self.assertFalse(self.state._opus_direct_active())

    def test_missing_old_runtime_metadata_and_source_errors_fail_closed(self):
        del self.voice._music_packet_trace
        self.assertFalse(self.state._opus_direct_active())
        self.voice._music_packet_trace = self.trace
        def failure():
            raise RuntimeError("source closed concurrently")
        self.source.opus_direct_status = failure
        self.assertFalse(self.state._opus_direct_active())

    def test_source_cleanup_silence_and_channel_move_clear_copy(self):
        for name in ("_closed", "music_ended"):
            with self.subTest(name=name):
                setattr(self.source, name, True)
                self.assertFalse(self.state._opus_direct_active())
                setattr(self.source, name, False)
        self.source.last_read_had_music = False
        self.assertFalse(self.state._opus_direct_active())
        self.source.last_read_had_music = True
        self.voice.channel.id = 99
        self.assertFalse(self.state._opus_direct_active())

    def test_runtime_payload_reaches_actual_controller_sync_and_panel_then_clears(self):
        from tests import test_music_opus_panel as panel
        panel.OpusPanelTests.setUpClass()
        boundary = panel.OpusPanelTests("test_full_sync_tracks_copy_then_tts_or_volume_fallback")
        router = panel.Router()
        self.track.audio_abr = 131
        boundary.synchronize(router, self.state.public())
        self.assertEqual(boundary.ui._public_audio_quality_label(router.state, router.state.current),
                         "Opus direto · 131 kbps")
        before = self.state.state_revision()
        self.live["copy_active"] = False
        self.assertNotEqual(self.state.state_revision(), before)
        boundary.synchronize(router, self.state.public())
        self.assertEqual(boundary.ui._public_audio_quality_label(router.state, router.state.current),
                         "Opus · 131 kbps")

    def test_prepared_runtime_payload_reaches_sync_and_panel_with_original_aac_preserved(self):
        from tests import test_music_opus_panel as panel
        panel.OpusPanelTests.setUpClass()
        boundary = panel.OpusPanelTests("test_full_sync_tracks_copy_then_tts_or_volume_fallback")
        router = panel.Router()
        self.prepared_source()
        boundary.synchronize(router, self.state.public())
        self.assertEqual(boundary.ui._public_audio_quality_label(router.state, router.state.current),
                         "Opus direto · 256 kbps")
        self.assertEqual(router.state.current.resolved_audio_codec, "aac")
        self.assertEqual(router.state.current.resolved_audio_abr, 128)
        self.live["copy_active"] = False
        boundary.synchronize(router, self.state.public())
        self.assertEqual(boundary.ui._public_audio_quality_label(router.state, router.state.current),
                         "AAC · 128 kbps")
        self.assertEqual(router.state.current.opus_direct_origin, "")
        self.assertEqual(router.state.current.opus_direct_bitrate_kbps, 0)


if __name__ == "__main__":
    unittest.main()
