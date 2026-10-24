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


if __name__ == "__main__":
    unittest.main()
