"""Actual VPS model/parser/panel methods with Discord boundaries omitted.

stdlib unittest; no Discord request or subjective audio result is claimed.
The UI and monitor functions are compiled from their production AST to avoid
importing unrelated network and Discord components in this offline test.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from collections import deque
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from cogs.musica.agente_telefone.conversao import faixa_do_payload
from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.reproducao import duracao_fila


ROOT = Path(__file__).resolve().parents[1]
MUSIC = ROOT / "cogs/musica"


def production_functions(relative, wanted=None, **values):
    path = MUSIC / relative
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and (wanted is None or n.name in wanted)]
    if wanted is not None:
        assert {n.name for n in nodes} == set(wanted)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    code = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    namespace = {"__name__": "test_panel_boundary", "__package__": "cogs.musica.reproducao",
                 "asyncio": asyncio, "contextlib": contextlib, "deque": deque,
                 "logger": logging.getLogger("test.music.opus.panel"),
                 "faixa_do_payload": faixa_do_payload,
                 "MusicTrack": MusicTrack,
                 "registrar_link_busca": lambda *a, **kw: None,
                 "desvincular_guild_worker": lambda *a, **kw: None,
                 **values}
    exec(compile(code, str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


class Router:
    def __init__(self):
        self.state = MusicGuildState()

    def get_state(self, guild_id):
        return self.state

    @staticmethod
    def _panel_key_for_track(track):
        return (track.queue_item_id or track.webpage_url or track.title) if track else ""

    @staticmethod
    def _set_current_status(state, status):
        state.current_status = status

    def __getattr__(self, name):
        return lambda *a, **kw: None

    async def update_panel(self, *args, **kwargs):
        return None


class OpusPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = production_functions("interface/componentes.py", {"_public_audio_quality_label"})
        cls.monitor = production_functions(
            "agente_telefone/monitor.py",
            {"_assinatura_faixa_painel", "_assinatura_playlist_virtual", "_assinatura_painel_remoto",
             "_deve_atualizar_painel", "_marcar_monitor_reconectando"},
            estado_comando_diferido=lambda gid: {})
        cls.sync = production_functions("reproducao/sincronizacao.py")

    def track(self, *, direct=False, codec="opus", item="current"):
        return faixa_do_payload({"title": "Test", "webpage_url": "https://example.invalid/track",
                                 "queue_item_id": item, "audio_codec": codec, "audio_abr": 131,
                                 "opus_direct_active": direct})

    def state(self, track, **patch):
        return SimpleNamespace(current=track, current_backend="agent", current_status="playing",
                               paused=False, current_quality_kbps=0, **patch)

    def test_default_model_flag_is_false_and_parser_accepts_only_true(self):
        self.assertFalse(MusicTrack("Test", "", 1).opus_direct_active)
        for value in (False, None, 1, "true", "auto", {}, []):
            with self.subTest(value=value):
                self.assertFalse(self.track(direct=value).opus_direct_active)
        self.assertTrue(self.track(direct=True).opus_direct_active)

    def test_parser_never_inherits_runtime_flag_from_fallback(self):
        old = self.track(direct=True)
        current = faixa_do_payload({"title": "Test", "audio_codec": "opus"}, old)
        self.assertFalse(current.opus_direct_active)
        self.assertTrue(old.opus_direct_active)

    def test_direct_label_keeps_existing_source_bitrate(self):
        track = self.track(direct=True)
        self.assertEqual(self.ui._public_audio_quality_label(self.state(track), track),
                         "Opus direto · 131 kbps")

    def prepared_track(self, *, direct=True, origin="prepared", bitrate=256):
        return faixa_do_payload({"title": "Test", "queue_item_id": "current",
                                 "audio_codec": "aac", "audio_abr": 128,
                                 "opus_direct_active": direct, "opus_direct_origin": origin,
                                 "opus_direct_bitrate_kbps": bitrate})

    def test_prepared_aac_label_distinguishes_output_from_original_source(self):
        track = self.prepared_track()
        self.assertEqual(self.ui._public_audio_quality_label(self.state(track), track),
                         "Opus direto · 256 kbps")
        self.assertEqual(track.resolved_audio_codec, "aac")
        self.assertEqual(track.resolved_audio_abr, 128)

    def test_aac_requires_live_copy_and_valid_prepared_provenance(self):
        cases = ({"direct": False}, {"origin": "original"}, {"origin": ""},
                 {"bitrate": None}, {"bitrate": True}, {"bitrate": "256"}, {"bitrate": 0},
                 {"bitrate": float("nan")}, {"bitrate": 513})
        for patch in cases:
            with self.subTest(patch=patch):
                track = self.prepared_track(**patch)
                self.assertEqual(self.ui._public_audio_quality_label(self.state(track), track),
                                 "AAC · 128 kbps")
        track = self.prepared_track()
        for patch in ({"current_status": "paused"}, {"paused": True},
                      {"current_status": "reconnecting"}, {"current_backend": "lavalink"}):
            with self.subTest(patch=patch):
                state = self.state(track)
                state.__dict__.update(patch)
                self.assertEqual(self.ui._public_audio_quality_label(state, track), "AAC · 128 kbps")

    def test_prepared_provenance_is_not_inherited_from_fallback_metadata(self):
        old = self.prepared_track()
        current = faixa_do_payload({"title": "Test", "audio_codec": "aac"}, old)
        self.assertEqual((current.opus_direct_active, current.opus_direct_origin,
                          current.opus_direct_bitrate_kbps), (False, "", 0))

    def test_configured_auto_is_not_evidence_of_copy(self):
        track = self.track()
        state = self.state(track)
        state.audio_output = {"settings": {"opus_direct": "auto"}}
        self.assertEqual(self.ui._public_audio_quality_label(state, track), "Opus · 131 kbps")

    def test_label_reverts_on_false_flag_and_does_not_change_volume(self):
        track = self.track(direct=True)
        state = self.state(track)
        state.volume = 0.55
        track.opus_direct_active = False
        self.assertEqual(self.ui._public_audio_quality_label(state, track), "Opus · 131 kbps")
        self.assertEqual(state.volume, 0.55)

    def test_only_current_agent_playing_opus_gets_badge(self):
        track = self.track(direct=True)
        cases = (dict(current_backend="local"), dict(current_backend="lavalink"),
                 dict(current_status="reconnecting"), dict(current_status="starting"),
                 dict(current_status="paused"), dict(paused=True), dict(current=self.track()))
        for values in cases:
            with self.subTest(values=values):
                state = self.state(track)
                for name, value in values.items():
                    setattr(state, name, value)
                self.assertEqual(self.ui._public_audio_quality_label(state, track), "Opus · 131 kbps")
        other = self.track(direct=True, codec="aac")
        self.assertEqual(self.ui._public_audio_quality_label(self.state(other), other), "AAC · 131 kbps")

    def test_copy_transition_changes_panel_signature_but_packet_counters_do_not(self):
        remote = {"status": "playing", "current": {"title": "Test", "opus_direct_active": False}}
        first = self.monitor._assinatura_painel_remoto(remote)
        remote["current"]["opus_direct_active"] = True
        direct = self.monitor._assinatura_painel_remoto(remote)
        self.assertNotEqual(first, direct)
        remote["audio_output"] = {"copied_packets": 100, "encoded_packets": 0}
        self.assertEqual(direct, self.monitor._assinatura_painel_remoto(remote))
        remote["audio_output"]["copied_packets"] = 1000
        self.assertEqual(direct, self.monitor._assinatura_painel_remoto(remote))
        self.assertTrue(self.monitor._deve_atualizar_painel(
            assinatura=direct, assinatura_anterior=first, agora=1, ultimo_refresh=1,
            painel_existe=True, refresh_seconds=30))

    def test_prepared_static_fields_change_panel_signature_without_counter_churn(self):
        remote = self.remote()
        first = self.monitor._assinatura_painel_remoto(remote)
        remote["current"].update(opus_direct_origin="prepared", opus_direct_bitrate_kbps=256)
        prepared = self.monitor._assinatura_painel_remoto(remote)
        self.assertNotEqual(first, prepared)
        remote["current"]["opus_direct_bitrate_kbps"] = 192
        self.assertNotEqual(prepared, self.monitor._assinatura_painel_remoto(remote))
        stable = self.monitor._assinatura_painel_remoto(remote)
        remote["audio_output"] = {"passthrough": {"copied_packets": 200,
                                                   "source_bitrate_kbps": 191.7,
                                                   "source": {"prepared_cache_hit": True}}}
        self.assertEqual(stable, self.monitor._assinatura_painel_remoto(remote))

    def remote(self, *, direct=True, **patch):
        return {"status": "playing", "confirmed_playing": True, "voice_connected": True,
                "player_present": True, "playback_token": 7,
                "current": {"title": "Test", "webpage_url": "https://example.invalid/track",
                            "queue_item_id": "current", "audio_codec": "opus", "audio_abr": 131,
                            "opus_direct_active": direct}, **patch}

    def synchronize(self, router, remote, **kwargs):
        with mock.patch.object(duracao_fila, "schedule_virtual_duration_scan", return_value=None):
            return asyncio.run(self.sync.sincronizar_estado_agente(
                router, 1, agent_state=remote, create_panel=False, **kwargs))

    def test_full_sync_tracks_copy_then_tts_or_volume_fallback(self):
        router = Router()
        self.synchronize(router, self.remote(direct=True))
        self.assertTrue(router.state.current.opus_direct_active)
        self.assertEqual(self.ui._public_audio_quality_label(router.state, router.state.current),
                         "Opus direto · 131 kbps")
        self.synchronize(router, self.remote(direct=False))
        self.assertFalse(router.state.current.opus_direct_active)
        self.assertEqual(self.ui._public_audio_quality_label(router.state, router.state.current),
                         "Opus · 131 kbps")

    def prepared_remote(self, **patch):
        remote = self.remote(**patch)
        remote["current"].update(audio_codec="aac", audio_abr=128,
                                  opus_direct_origin="prepared", opus_direct_bitrate_kbps=256)
        return remote

    def test_full_sync_prepared_aac_then_fallback_clears_its_provenance(self):
        router = Router()
        remote = self.prepared_remote()
        self.synchronize(router, remote)
        current = router.state.current
        self.assertEqual(self.ui._public_audio_quality_label(router.state, current),
                         "Opus direto · 256 kbps")
        remote["current"]["opus_direct_active"] = False
        self.synchronize(router, remote, queued=True)
        self.assertIs(router.state.current, current)
        self.assertEqual((current.opus_direct_active, current.opus_direct_origin,
                          current.opus_direct_bitrate_kbps), (False, "", 0))
        self.assertEqual(self.ui._public_audio_quality_label(router.state, current), "AAC · 128 kbps")

    def test_preserved_current_clears_missing_or_other_item_prepared_fields(self):
        router = Router()
        self.synchronize(router, self.prepared_remote())
        current = router.state.current
        remote = self.prepared_remote()
        remote["current"].pop("opus_direct_origin")
        remote["current"].pop("opus_direct_bitrate_kbps")
        self.synchronize(router, remote, queued=True)
        self.assertEqual((current.opus_direct_origin, current.opus_direct_bitrate_kbps), ("", 0))
        self.synchronize(router, self.prepared_remote(), queued=True)
        remote = self.prepared_remote()
        remote["current"]["queue_item_id"] = "another"
        self.synchronize(router, remote, queued=True)
        self.assertEqual((current.opus_direct_active, current.opus_direct_origin,
                          current.opus_direct_bitrate_kbps), (False, "", 0))

    def test_reconnect_clears_prepared_output_before_next_authoritative_sync(self):
        router = Router()
        self.synchronize(router, self.prepared_remote())
        current = router.state.current
        asyncio.run(self.monitor._marcar_monitor_reconectando(router, 1, falhas=2))
        self.assertEqual((current.opus_direct_active, current.opus_direct_origin,
                          current.opus_direct_bitrate_kbps), (False, "", 0))

    def test_same_track_enqueue_response_updates_preserved_object_and_clears_missing_flag(self):
        router = Router()
        self.synchronize(router, self.remote())
        original = router.state.current
        self.synchronize(router, self.remote(direct=False), queued=True)
        self.assertIs(router.state.current, original)
        self.assertFalse(original.opus_direct_active)
        self.synchronize(router, self.remote(), queued=True)
        self.assertTrue(original.opus_direct_active)
        remote = self.remote()
        remote["current"].pop("opus_direct_active")
        self.synchronize(router, remote, queued=True)
        self.assertFalse(original.opus_direct_active)

    def test_unconfirmed_or_paused_payload_never_keeps_badge(self):
        for values in ({"confirmed_playing": False, "voice_connected": False, "player_present": False},
                       {"status": "paused", "paused": True}):
            with self.subTest(values=values):
                router = Router()
                self.synchronize(router, self.remote(**values))
                self.assertFalse(router.state.current.opus_direct_active)

    def test_queued_response_for_other_item_cannot_mark_preserved_current(self):
        router = Router()
        self.synchronize(router, self.remote())
        original = router.state.current
        remote = self.remote()
        remote["current"]["queue_item_id"] = "different-item"
        self.synchronize(router, remote, queued=True)
        self.assertIs(router.state.current, original)
        self.assertFalse(original.opus_direct_active)

    def test_reconnect_monitor_clears_last_confirmed_copy_without_advancing_track(self):
        router = Router()
        router.state.current = self.track(direct=True)
        router.state.current_backend = "agent"
        router.state.current_status = "playing"
        current = router.state.current
        asyncio.run(self.monitor._marcar_monitor_reconectando(router, 1, falhas=2))
        self.assertIs(router.state.current, current)
        self.assertFalse(current.opus_direct_active)
        self.assertEqual(router.state.current_status, "reconnecting")


if __name__ == "__main__":
    unittest.main()
