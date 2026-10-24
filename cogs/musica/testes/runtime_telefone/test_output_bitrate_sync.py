"""Actual sync methods and mixer with simulated Discord/encoder boundaries.

These are stdlib unittest checks. The production mixer is loaded with only
discord.AudioSource adapted; selected ReproducaoMixin methods are compiled
from their real AST so network dependencies are not needed. No real Opus
encoding, Discord call or audible quality is claimed by this suite.
"""
from __future__ import annotations

import ast
import asyncio
from array import array
import contextlib
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
RUNTIME = ROOT / "cogs/musica/runtime_telefone/agente"


def load_production_boundaries():
    spec = importlib.util.spec_from_file_location("output_sync_real_mixer", RUNTIME / "mixer_pcm.py")
    mixer = importlib.util.module_from_spec(spec)
    class AudioSource:
        pass
    with mock.patch.dict("sys.modules", {"discord": SimpleNamespace(AudioSource=AudioSource)}):
        spec.loader.exec_module(mixer)
    wanted = {"_discord_opus_bitrate_kbps", "_resolved_audio_output_policy", "_output_policy_signature", "_audio_output_voice_view",
              "_sync_audio_output", "_play_music_source", "_play_direct_voice"}
    source = ast.parse((RUNTIME / "reproducao.py").read_text())
    original = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "ReproducaoMixin")
    selected = [node for node in original.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in wanted]
    assert {node.name for node in selected} == wanted
    boundary = ast.ClassDef(name="Agent", bases=[], keywords=[], body=selected, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), boundary], type_ignores=[]))
    namespace = {"SimpleNamespace": SimpleNamespace, "contextlib": contextlib, "time": time, "asyncio": asyncio,
                 "GuildMusicState": SimpleNamespace, "env_float": lambda key, default: default,
                 "safe_id": lambda value: int(value or 0), "short_text": lambda value, limit: str(value)[:limit],
                 "AgentMixedAudioSource": mixer.AgentMixedAudioSource}
    exec(compile(module, str(RUNTIME / "reproducao.py"), "exec"), namespace)
    return namespace["Agent"], mixer


class PCM:
    def __init__(self, value=1000):
        self.frame = array("h", [value, -value] * 960).tobytes()
        self.closed = False

    def read(self):
        return self.frame

    def cleanup(self):
        self.closed = True


class Encoder:
    def __init__(self):
        self.calls = []

    def set_bitrate(self, value):
        self.calls.append(("bitrate", value))
        return value

    def set_fec(self, value):
        self.calls.append(("fec", value))

    def set_expected_packet_loss_percent(self, value):
        self.calls.append(("loss", value))

    def set_signal_type(self, value):
        self.calls.append(("signal", value))

    def set_bandwidth(self, value):
        self.calls.append(("bandwidth", value))


class OutputSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Agent, cls.mixer = load_production_boundaries()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.agent = self.Agent()
        self.logs = []
        self.agent.log = lambda event, **fields: self.logs.append((event, fields))
        self.agent.discord_opus_default_bitrate = 160
        self.agent.discord_opus_min_bitrate = 96
        self.agent.discord_opus_max_bitrate = 256
        self.agent.discord_opus_source_headroom = 32
        self.pcm = PCM()
        self.source = self.mixer.AgentMixedAudioSource(loop=self.loop, music_source=self.pcm, music_volume=1,
                                                     persistent=True, telemetry_enabled=False)
        self.source.quality_context = {}
        self.addCleanup(self.source.cleanup)
        self.track = SimpleNamespace(audio_abr=160)
        self.channel = SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1))
        self.encoder = Encoder()
        self.voice = SimpleNamespace(source=self.source, channel=self.channel, encoder=self.encoder)
        self.st = SimpleNamespace(current=self.track, player=self.voice, transport="direct", voice_channel_id=20,
                                  playback_token=9, updated_at=0, audio_output_applied={})
        self.agent.states = {1: self.st}

    def sync(self, **kwargs):
        return self.loop.run_until_complete(self.agent._sync_audio_output(1, **kwargs))

    def policy(self, revision=0, bitrate=192, fec=True, loss=0.15):
        return {"bitrate_kbps": bitrate, "channel_bitrate_kbps": 256, "profile": "quality_192",
                "fec": fec, "expected_packet_loss": loss, "application": "audio", "bandwidth": "full",
                "signal_type": "music", "revision": revision}

    def test_channel_update_applies_on_audio_read_and_preserves_pcm(self):
        result = self.sync()
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.encoder.calls, [])
        self.assertEqual(self.source.read(), self.pcm.frame)
        self.assertEqual(self.encoder.calls[0], ("bitrate", 192))
        self.assertEqual(self.st.audio_output_applied["state"], "applied")
        self.assertGreater(self.st.updated_at, 0)
        self.assertEqual(self.source.quality_context["opus_bitrate_kbps"], 192)
        self.assertFalse(self.pcm.closed)

    def test_channel_and_profile_changed_during_pcm_prepare_apply_to_initial_play(self):
        self._exercise_startup_policy_change(during_prepare=True)

    def test_channel_update_during_first_frame_confirmation_keeps_applied_context(self):
        self._exercise_startup_policy_change(during_prepare=False)

    def _exercise_startup_policy_change(self, *, during_prepare):
        config_spec = importlib.util.spec_from_file_location("output_sync_real_config", RUNTIME / "configuracao.py")
        config = importlib.util.module_from_spec(config_spec)
        config_spec.loader.exec_module(config)
        self.agent._audio_output_policy = lambda voice, track, state: config.audio_output_policy(self.agent, voice, track, state)
        self.channel.bitrate = 64000
        cache = {"channel": self.channel}
        self.agent.client = SimpleNamespace(get_channel=lambda channel_id: cache["channel"])
        self.agent._loop = self.loop
        self.agent.direct_pcm_volume_enabled = True
        self.agent._ffmpeg_options_for_source = lambda *args, **kwargs: ("", "native_48k")
        self.agent._set_status = lambda state, status, **kwargs: setattr(state, "status", status)
        self.agent._set_voice_session_mode = lambda *args, **kwargs: None
        self.agent._arm_music_packet_trace = lambda *args, **kwargs: None
        self.agent._schedule_next_queue_prefetch = lambda *args, **kwargs: None
        self.agent._build_ffmpeg_source = lambda *args, **kwargs: self.source
        async def refresh(*args, **kwargs): return None
        async def confirm(*args, **kwargs):
            if not during_prepare:
                cache["channel"] = SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1))
                await self.agent._sync_audio_output(1, reason="channel_update", channel=cache["channel"])
                self.source.read()
            return 0
        self.agent._refresh_voice_presence_policy = refresh
        self.agent._confirm_direct_playback = confirm
        self.st.audio_output_settings = {}
        self.st.audio_output_revision = 0
        self.st.effect_signature = lambda: (0, 0, 0)
        self.st.effect_level = lambda effect: 0
        self.st.volume_percent = 55
        self.track.title = "quality race"
        self.track.stream_url = "https://media.invalid/never-fetched"
        self.track.archive_segments, self.track.attachment_ref = [], {}
        self.track.audio_sample_rate, self.track.audio_channels = 48000, 2
        self.track.start_offset_seconds, self.track.is_live = 0, False
        self.track.agent_timing_ms = {}
        self.voice.source = None
        self.voice.is_connected = lambda: True
        self.voice.is_playing = lambda: False
        self.voice.is_paused = lambda: False
        played = []
        def play(source, **kwargs):
            played.append(kwargs)
            self.voice.source = source
        self.voice.play = play
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def prepare(*args, **kwargs):
                entered.set()
                await release.wait()
                return self.pcm
            self.agent._prepare_current_pcm = prepare
            task = asyncio.create_task(self.agent._play_direct_voice(1, self.track, prepared_voice=(self.voice, False)))
            await entered.wait()
            cache["channel"] = SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1))
            if during_prepare:
                self.st.audio_output_settings = {"profile": "quality_256", "fec": False, "expected_packet_loss": 0.02}
                self.st.audio_output_revision = 1
            else:
                cache["channel"] = self.channel
            release.set()
            await task
        self.loop.run_until_complete(scenario())
        self.assertEqual(played[0]["bitrate"], 256 if during_prepare else 64)
        self.assertEqual(played[0]["fec"], not during_prepare)
        self.assertEqual(played[0]["expected_packet_loss"], 0.02 if during_prepare else 0.15)
        self.assertEqual(self.st.audio_output_applied["revision"], int(during_prepare))
        self.assertEqual(self.source.quality_context["opus_bitrate_kbps"], 256 if during_prepare else 192)
        self.assertEqual(self.st.audio_output_applied["bitrate_kbps"], 256 if during_prepare else 192)
        selected = next(fields for event, fields in self.logs if event == "audio_source_selected")
        self.assertEqual(selected["opus_bitrate_kbps"], 256 if during_prepare else 64)
        self.assertEqual(selected["audio_output_profile"], "quality_256" if during_prepare else "balanced")

    def test_after_channel_reference_does_not_mutate_voice_client(self):
        self.channel.bitrate = 64000
        after = SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1))
        self.assertEqual(self.sync(channel=after, expected_channel_id=20)["requested"]["bitrate_kbps"], 192)
        self.source.read()
        self.assertEqual(self.encoder.calls[0], ("bitrate", 192))
        self.assertEqual(self.voice.channel.bitrate, 64000)

    def test_reduction_respects_channel_below_configured_floor(self):
        self.channel.bitrate = 64000
        self.sync()
        self.source.read()
        self.assertEqual(self.st.audio_output_applied["bitrate_kbps"], 64)

    def test_profile_change_uses_current_cache_after_channel_event(self):
        self.channel.bitrate = 64000
        after = SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1))
        self.agent.client = SimpleNamespace(get_channel=lambda channel_id: after)
        self.sync(channel=after)
        self.source.read()
        self.st.audio_output_applied = {}
        result = self.sync(reason="profile_change")
        self.source.read()
        self.assertEqual(result["requested"]["bitrate_kbps"], 192)
        self.assertEqual(self.st.audio_output_applied["bitrate_kbps"], 192)
        self.assertEqual(self.channel.bitrate, 64000)

    def test_newer_cache_drops_pending_obsolete_after_channel(self):
        after = SimpleNamespace(id=20, bitrate=128000, guild=SimpleNamespace(id=1))
        latest = SimpleNamespace(id=20, bitrate=256000, guild=SimpleNamespace(id=1))
        self.agent.client = SimpleNamespace(get_channel=lambda channel_id: latest)
        self.sync(channel=after)
        self.source.read()
        self.assertEqual(self.encoder.calls, [])

    def test_queued_updates_coalesce_to_latest_policy(self):
        self.channel.bitrate = 128000
        self.sync()
        self.channel.bitrate = 256000
        self.sync()
        self.source.read()
        self.assertEqual([call for call in self.encoder.calls if call[0] == "bitrate"], [("bitrate", 192)])

    def test_duplicate_pending_policy_is_not_requeued(self):
        self.sync()
        callback = self.source._encoder_update
        self.sync()
        self.assertIs(self.source._encoder_update, callback)
        self.source.read()
        self.assertEqual(self.sync()["state"], "unchanged")

    def test_stale_source_track_player_generation_and_channel_do_not_change_encoder(self):
        mutations = (
            lambda: setattr(self.voice, "source", object()),
            lambda: setattr(self.st, "current", SimpleNamespace(audio_abr=160)),
            lambda: setattr(self.st, "player", SimpleNamespace()),
            lambda: setattr(self.st, "playback_token", 10),
            lambda: setattr(self.st, "voice_channel_id", 21),
            lambda: setattr(self.voice, "channel", SimpleNamespace(id=21, bitrate=256000)),
            lambda: self.agent.states.clear(),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.agent.states = {1: self.st}
                self.st.current, self.st.player, self.st.playback_token, self.st.voice_channel_id = self.track, self.voice, 9, 20
                self.voice.source, self.voice.channel = self.source, self.channel
                self.source._music_output_pending_signature = None
                self.sync()
                callback = self.source._encoder_update
                mutation()
                callback()
                self.assertEqual(self.encoder.calls, [])

    def test_reused_source_can_queue_same_policy_for_new_track(self):
        self.sync()
        old_callback = self.source._encoder_update
        self.st.current = SimpleNamespace(audio_abr=160)
        self.st.playback_token += 1
        self.sync()
        self.assertIsNot(self.source._encoder_update, old_callback)
        old_callback()
        self.assertEqual(self.encoder.calls, [])
        self.source.read()
        self.assertEqual(self.encoder.calls[0], ("bitrate", 192))

    def test_consumed_callback_while_disconnected_can_resync_same_session(self):
        self.voice.is_connected = lambda: True
        self.sync()
        self.voice.is_connected = lambda: False
        self.source.read()
        self.assertEqual(self.encoder.calls, [])
        self.assertIsNone(self.source._music_output_pending_signature)
        self.voice.is_connected = lambda: True
        self.sync()
        self.source.read()
        self.assertEqual(self.encoder.calls[0], ("bitrate", 192))

    def test_profile_revision_changed_before_read_drops_obsolete_control(self):
        chosen = self.policy()
        self.agent._audio_output_policy = lambda *args: dict(chosen)
        self.sync()
        chosen["revision"] += 1
        self.source.read()
        self.assertEqual(self.encoder.calls, [])

    def test_paused_music_retains_tts_and_applies_controls(self):
        overlay = PCM(500)
        future = self.source.add_tts(overlay)
        self.source.set_music_paused(True)
        self.sync()
        self.assertEqual(self.source.read(), overlay.frame)
        self.assertEqual(self.encoder.calls[0], ("bitrate", 192))
        self.assertTrue(self.source.has_tts())
        self.assertFalse(future.done())

    def test_wrong_channel_or_guild_cannot_control_current_encoder(self):
        self.assertEqual(self.sync(expected_channel_id=21)["state"], "deferred")
        self.assertEqual(self.sync(channel=SimpleNamespace(id=20, guild=SimpleNamespace(id=2)))["state"], "deferred")
        self.assertIsNone(self.source._encoder_update)

    def test_encoded_source_is_deferred_without_stopping_or_claiming_bitrate(self):
        self.voice.source = SimpleNamespace(is_opus=lambda: True)
        result = self.sync()
        self.assertEqual(result["state"], "deferred")
        self.assertEqual(result["applied"], {})
        self.assertEqual(self.encoder.calls, [])

    def test_idle_and_other_transport_are_not_changed(self):
        self.st.transport = "worker"
        self.assertEqual(self.sync()["state"], "idle")
        self.st.transport = "direct"
        self.st.current = None
        self.assertEqual(self.sync()["state"], "idle")

    def test_unsupported_fec_controls_are_reported_as_partial(self):
        self.voice.encoder = SimpleNamespace(set_bitrate=lambda value: value)
        self.sync()
        self.source.read()
        applied = self.st.audio_output_applied
        self.assertEqual(applied["state"], "partial")
        self.assertIsNone(applied["fec"])
        self.assertIsNone(applied["expected_packet_loss"])
        self.assertEqual(applied["bitrate_kbps"], 192)

    def test_partial_setter_failure_does_not_escape_read_or_claim_unknown_controls(self):
        self.encoder.set_fec = mock.Mock(side_effect=RuntimeError("unsupported"))
        self.sync()
        self.assertEqual(self.source.read(), self.pcm.frame)
        self.assertEqual(self.st.audio_output_applied["state"], "partial")
        self.assertEqual(self.st.audio_output_applied["bitrate_kbps"], 192)
        self.assertIsNone(self.st.audio_output_applied["fec"])

    def test_policy_error_in_audio_thread_preserves_audio(self):
        self.sync()
        self.agent._audio_output_policy = mock.Mock(side_effect=ValueError("private policy detail"))
        self.assertEqual(self.source.read(), self.pcm.frame)
        self.assertEqual(self.encoder.calls, [])
        self.assertNotIn("private policy detail", str(self.logs))

    def test_initial_play_explicit_policy_and_legacy_call_behavior(self):
        calls = []
        voice = SimpleNamespace(play=lambda *args, **kwargs: calls.append(kwargs))
        self.agent._play_music_source(voice, self.source, after=None, opus_bitrate_kbps=192,
                                      output_policy=self.policy(fec=False, loss=0.02))
        self.assertEqual(calls[0]["fec"], False)
        self.assertEqual(calls[0]["expected_packet_loss"], 0.02)
        self.assertEqual(calls[0]["application"], "audio")
        self.assertEqual(voice._music_audio_output_applied["state"], "applied")
        self.agent._play_music_source(voice, self.source, after=None, opus_bitrate_kbps=192)
        self.assertNotIn("fec", calls[1])

    def test_old_play_signature_fallback_does_not_claim_policy_applied(self):
        calls = []
        def play(source, *, after=None): calls.append(source)
        voice = SimpleNamespace(play=play)
        self.agent._play_music_source(voice, self.source, after=None, opus_bitrate_kbps=192,
                                      output_policy=self.policy())
        self.assertEqual(calls, [self.source])
        self.assertEqual(voice._music_audio_output_applied["state"], "unsupported")
        self.assertIsNone(voice._music_opus_bitrate_kbps)

    def test_unrelated_type_error_from_play_is_not_hidden(self):
        voice = SimpleNamespace(play=mock.Mock(side_effect=TypeError("internal bug")))
        with self.assertRaisesRegex(TypeError, "internal bug"):
            self.agent._play_music_source(voice, self.source, after=None, opus_bitrate_kbps=192,
                                          output_policy=self.policy())
        self.assertEqual(voice.play.call_count, 1)


if __name__ == "__main__":
    unittest.main()
