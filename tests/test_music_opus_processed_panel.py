"""Current native Opus processing evidence through runtime, VPS sync and panel.

Only Discord/transport boundaries are represented by data doubles. These tests
do not infer audible quality or a real Discord session from a panel label.
"""
from __future__ import annotations

import asyncio
from copy import copy
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.agente_telefone.conversao import faixa_do_payload
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from tests import test_music_opus_panel as panel_helpers


class ProcessedPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = panel_helpers.production_functions(
            "interface/componentes.py", {"_public_audio_quality_label"})
        cls.monitor = panel_helpers.production_functions(
            "agente_telefone/monitor.py",
            {"_assinatura_faixa_painel", "_assinatura_playlist_virtual", "_assinatura_painel_remoto",
             "_deve_atualizar_painel", "_marcar_monitor_reconectando"},
            estado_comando_diferido=lambda gid: {})
        cls.sync = panel_helpers.production_functions("reproducao/sincronizacao.py")

    def setUp(self):
        self.track = AgentTrack(queue_item_id="current", title="Test", audio_codec="aac", audio_abr=128)
        self.music = object()
        self.live = {"copy_active": False, "processed_active": True,
                     "processed_bitrate_kbps": 160, "encoded_packets": 10,
                     "opus_transport": True, "output_mode": "opus_processed"}
        self.source = SimpleNamespace(
            _closed=False, music_ended=False, last_read_had_music=True,
            music_source=self.music, last_read_music_source=self.music,
            opus_direct_status=lambda: dict(self.live))
        self.trace = {"track": self.track, "token": 3, "guild_id": 1,
                      "source": self.source, "expected_music_source": self.music,
                      "first_packet_monotonic": .1}
        self.voice = SimpleNamespace(source=self.source, channel=SimpleNamespace(id=2),
                                     is_connected=lambda: True, _music_packet_trace=self.trace)
        self.state = GuildMusicState(guild_id=1, voice_channel_id=2, current=self.track,
                                     transport="direct", status="playing", player=self.voice,
                                     volume_percent=55, playback_token=3, updated_at=100)

    def current(self):
        return self.state.public()["current"]

    def payload(self, *, processed=True, bitrate=160, direct=False):
        return {"title": "Test", "queue_item_id": "current", "audio_codec": "aac", "audio_abr": 128,
                "opus_direct_active": direct, "opus_processed_active": processed,
                "opus_processed_bitrate_kbps": bitrate}

    def local_state(self, track, **values):
        state = SimpleNamespace(current=track, current_backend="agent", current_status="playing",
                                paused=False, current_quality_kbps=0)
        state.__dict__.update(values)
        return state

    def remote(self, **values):
        return {"status": "playing", "confirmed_playing": True, "voice_connected": True,
                "player_present": True, "playback_token": 3, "current": self.payload(), **values}

    def synchronize(self, router, remote, **kwargs):
        with mock.patch.object(panel_helpers.duracao_fila, "schedule_virtual_duration_scan", return_value=None):
            return asyncio.run(self.sync.sincronizar_estado_agente(
                router, 1, agent_state=remote, create_panel=False, **kwargs))

    def test_runtime_public_proves_processed_music_without_claiming_original_copy(self):
        current = self.current()
        self.assertTrue(current["opus_processed_active"])
        self.assertEqual(current["opus_processed_bitrate_kbps"], 160)
        self.assertFalse(current["opus_direct_active"])
        self.assertEqual((current["audio_codec"], current["audio_abr"]), ("aac", 128))
        self.assertEqual(self.state.volume_percent, 55)
        self.assertNotIn("opus_processed_active", self.track.public())
        self.state.queue = [AgentTrack(queue_item_id="queued", audio_codec="aac")]
        self.assertNotIn("opus_processed_active", self.state.public()["queue"][0])

    def test_runtime_processed_requires_valid_confirmed_bitrate(self):
        for bitrate in (None, True, 0, 15, 513, "160", float("nan")):
            with self.subTest(bitrate=bitrate):
                self.live["processed_bitrate_kbps"] = bitrate
                current = self.current()
                self.assertFalse(current["opus_processed_active"])
                self.assertEqual(current["opus_processed_bitrate_kbps"], 0)

    def test_runtime_rejects_stale_track_token_guild_source_and_first_packet(self):
        for field, value in (("track", copy(self.track)), ("token", 2), ("guild_id", 2),
                             ("source", object()), ("expected_music_source", object()),
                             ("first_packet_monotonic", None)):
            with self.subTest(field=field):
                previous = self.trace[field]
                self.trace[field] = value
                self.assertFalse(self.current()["opus_processed_active"])
                self.trace[field] = previous
        self.source.last_read_music_source = object()
        self.assertFalse(self.current()["opus_processed_active"])

    def test_runtime_clears_processed_for_pause_tts_only_recovery_and_closed_source(self):
        self.state.paused = True
        self.assertFalse(self.current()["opus_processed_active"])
        self.state.paused = False
        self.source.last_read_had_music = False
        self.assertFalse(self.current()["opus_processed_active"])
        self.source.last_read_had_music = True
        self.state.voice_runtime_recovery_pending = True
        self.assertFalse(self.current()["opus_processed_active"])
        self.state.voice_runtime_recovery_pending = False
        self.source._closed = True
        self.assertFalse(self.current()["opus_processed_active"])

    def test_runtime_zero_volume_keeps_real_processed_packet_evidence(self):
        self.state.volume_percent = 0
        self.assertTrue(self.current()["opus_processed_active"])
        self.assertEqual(self.state.volume_percent, 0)

    def test_runtime_revision_tracks_static_processed_mode_not_packet_counts(self):
        initial = self.state.state_revision()
        self.live["encoded_packets"] = 1000
        self.assertEqual(self.state.state_revision(), initial)
        self.live["processed_bitrate_kbps"] = 96
        self.assertNotEqual(self.state.state_revision(), initial)
        self.live["processed_active"] = False
        self.assertEqual(self.state.state_revision(), "100.000000:3")

    def test_direct_and_processed_runtime_fields_cannot_both_be_true(self):
        self.track.audio_codec = "opus"
        self.live["copy_active"] = True
        current = self.current()
        self.assertFalse(current["opus_direct_active"] and current["opus_processed_active"])

    def test_parser_accepts_only_confirmed_processed_flag_and_valid_bitrate(self):
        track = faixa_do_payload(self.payload())
        self.assertTrue(track.opus_processed_active)
        self.assertEqual(track.opus_processed_bitrate_kbps, 160)
        self.assertEqual(self.ui._public_audio_quality_label(self.local_state(track), track),
                         "Opus processado · 160 kbps")
        for patch in ({"processed": 1}, {"processed": "true"}, {"processed": False},
                      {"bitrate": None}, {"bitrate": True}, {"bitrate": 513}, {"bitrate": "160"}):
            with self.subTest(patch=patch):
                track = faixa_do_payload(self.payload(**patch))
                self.assertFalse(track.opus_processed_active)
                self.assertEqual(track.opus_processed_bitrate_kbps, 0)

    def test_parser_never_inherits_processed_mode_from_fallback_track(self):
        old = faixa_do_payload(self.payload())
        track = faixa_do_payload({"title": "Test", "audio_codec": "aac"}, old)
        self.assertFalse(track.opus_processed_active)
        self.assertEqual(track.opus_processed_bitrate_kbps, 0)
        self.assertTrue(old.opus_processed_active)

    def test_panel_requires_current_playing_agent_output(self):
        track = faixa_do_payload(self.payload())
        for patch in ({"current_status": "paused"}, {"current_status": "reconnecting"},
                      {"paused": True}, {"current_backend": "lavalink"},
                      {"current": faixa_do_payload(self.payload())}):
            with self.subTest(patch=patch):
                state = self.local_state(track, **patch)
                self.assertEqual(self.ui._public_audio_quality_label(state, track), "AAC · 128 kbps")

    def test_runtime_through_vps_sync_to_panel_preserves_original_media(self):
        remote = self.remote(current=self.current())
        router = panel_helpers.Router()
        self.synchronize(router, remote)
        current = router.state.current
        self.assertEqual(self.ui._public_audio_quality_label(router.state, current),
                         "Opus processado · 160 kbps")
        self.assertFalse(current.opus_direct_active)
        self.assertEqual(current.resolved_audio_codec, "aac")

    def test_preserved_current_clears_missing_or_other_track_processed_fields(self):
        router = panel_helpers.Router()
        self.synchronize(router, self.remote())
        current = router.state.current
        remote = self.remote()
        remote["current"].pop("opus_processed_active")
        remote["current"].pop("opus_processed_bitrate_kbps")
        self.synchronize(router, remote, queued=True)
        self.assertIs(router.state.current, current)
        self.assertFalse(current.opus_processed_active)
        self.assertEqual(current.opus_processed_bitrate_kbps, 0)
        self.synchronize(router, self.remote(), queued=True)
        remote = self.remote()
        remote["current"]["queue_item_id"] = "other"
        self.synchronize(router, remote, queued=True)
        self.assertFalse(current.opus_processed_active)
        self.assertEqual(current.opus_processed_bitrate_kbps, 0)

    def test_reconnection_clears_processed_before_next_authoritative_snapshot(self):
        router = panel_helpers.Router()
        self.synchronize(router, self.remote())
        asyncio.run(self.monitor._marcar_monitor_reconectando(router, 1, falhas=2))
        self.assertFalse(router.state.current.opus_processed_active)
        self.assertEqual(router.state.current.opus_processed_bitrate_kbps, 0)

    def test_panel_signature_changes_on_static_mode_or_bitrate_only(self):
        remote = self.remote()
        initial = self.monitor._assinatura_painel_remoto(remote)
        remote["audio_output"] = {"encoded_packets": 100, "source": {"prepared_cache_hit": True}}
        self.assertEqual(self.monitor._assinatura_painel_remoto(remote), initial)
        remote["current"]["opus_processed_bitrate_kbps"] = 96
        self.assertNotEqual(self.monitor._assinatura_painel_remoto(remote), initial)
        changed = self.monitor._assinatura_painel_remoto(remote)
        remote["current"]["opus_processed_active"] = False
        self.assertNotEqual(self.monitor._assinatura_painel_remoto(remote), changed)


if __name__ == "__main__":
    unittest.main()
