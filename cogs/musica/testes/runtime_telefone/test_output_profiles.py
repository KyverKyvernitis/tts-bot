"""Output policy and state contracts without Discord, HTTP or audio devices."""
from __future__ import annotations

import math
from types import SimpleNamespace
import unittest

from cogs.musica.runtime_telefone.agente.configuracao import (
    audio_output_capabilities,
    audio_output_policy,
    opus_passthrough_eligibility,
    validate_audio_output_settings,
)
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState


def agent(**overrides):
    return SimpleNamespace(**{
        "audio_output_default_profile": "balanced",
        "audio_output_default_opus_direct": "off",
        "discord_opus_fec": True,
        "discord_opus_expected_packet_loss": 0.15,
        "discord_opus_default_bitrate": 160,
        "discord_opus_min_bitrate": 96,
        "discord_opus_max_bitrate": 256,
        "discord_opus_source_headroom": 32,
        "direct_pcm_volume_enabled": True,
        "opus_passthrough_enabled": False,
        "ffmpeg_options": "-vn -sn -dn -loglevel warning",
        **overrides,
    })


def voice(kbps):
    return SimpleNamespace(channel=SimpleNamespace(bitrate=kbps * 1000))


class OutputPolicyTests(unittest.TestCase):
    def test_balanced_preserves_previous_bitrate_choices(self):
        cases = ((128, 64, 64), (128, 96, 96), (128, 128, 128),
                 (128, 256, 160), (128, 384, 160), (160, 256, 192),
                 (192, 256, 224), (320, 384, 256), (0, 256, 160), (32, 256, 96))
        for source, channel, expected in cases:
            with self.subTest(source=source, channel=channel):
                result = audio_output_policy(agent(), voice(channel), AgentTrack(audio_abr=source))
                self.assertEqual(result["bitrate_kbps"], expected)
                self.assertEqual(result["profile"], "balanced")

    def test_explicit_quality_profiles_are_comparable_with_the_same_source(self):
        track = AgentTrack(audio_abr=128)
        state = GuildMusicState(1)
        for profile, expected in (("quality_192", 192), ("quality_256", 256)):
            state.audio_output_settings = {"profile": profile}
            result = audio_output_policy(agent(), voice(384), track, state)
            self.assertEqual(result["bitrate_kbps"], expected)
            self.assertEqual(result["source_abr_kbps"], 128)
        self.assertEqual(track.audio_abr, 128)

    def test_copy_budget_uses_channel_capacity_without_raising_encoder_target(self):
        track = AgentTrack(audio_abr=131)
        state = GuildMusicState(1)
        for profile, encoder in (("balanced", 163), ("quality_192", 192), ("quality_256", 256)):
            with self.subTest(profile=profile):
                state.audio_output_settings = {"profile": profile}
                result = audio_output_policy(agent(), voice(256), track, state)
                self.assertEqual(result["bitrate_kbps"], encoder)
                self.assertEqual(result["copy_bitrate_kbps"], 256)
                self.assertEqual(track.audio_abr, 131)

    def test_copy_budget_respects_channel_config_and_unknown_capacity(self):
        for channel, maximum, expected in ((96, 256, 96), (384, 128, 128),
                                           (384, 512, 384), (0, 256, 0)):
            with self.subTest(channel=channel, maximum=maximum):
                result = audio_output_policy(agent(discord_opus_max_bitrate=maximum),
                                             voice(channel), AgentTrack(audio_abr=131))
                self.assertEqual(result["copy_bitrate_kbps"], expected)

    def test_channel_cap_wins_over_quality_profile(self):
        state = GuildMusicState(1, audio_output_settings={"profile": "quality_256"})
        result = audio_output_policy(agent(), voice(64), AgentTrack(audio_abr=320), state)
        self.assertEqual(result["bitrate_kbps"], 64)
        self.assertTrue(result["limited_by_channel"])
        self.assertFalse(result["limited_by_config"])

    def test_configured_maximum_is_respected_and_reported(self):
        state = GuildMusicState(1, audio_output_settings={"profile": "quality_256"})
        result = audio_output_policy(agent(discord_opus_max_bitrate=128), voice(384), AgentTrack(), state)
        self.assertEqual(result["bitrate_kbps"], 128)
        self.assertTrue(result["limited_by_config"])
        self.assertEqual(result["target_bitrate_kbps"], 256)

    def test_missing_channel_keeps_configured_bound(self):
        result = audio_output_policy(agent(), SimpleNamespace(), AgentTrack(audio_abr=320))
        self.assertEqual(result["bitrate_kbps"], 256)
        self.assertEqual(result["channel_bitrate_kbps"], 0)

    def test_empty_agent_keeps_legacy_music_defaults(self):
        result = audio_output_policy(SimpleNamespace(), voice(384))
        self.assertEqual(result["bitrate_kbps"], 160)
        self.assertTrue(result["fec"])
        self.assertEqual(result["expected_packet_loss"], 0.15)
        self.assertEqual((result["application"], result["bandwidth"], result["signal_type"]),
                         ("audio", "full", "music"))
        self.assertEqual(result["opus_direct"], "auto")

    def test_direct_mode_is_per_guild_and_preserves_music_defaults(self):
        current = agent()
        enabled = GuildMusicState(1, audio_output_settings={"opus_direct": "auto"})
        other = GuildMusicState(2)
        one = audio_output_policy(current, voice(256), AgentTrack(audio_abr=128), enabled)
        two = audio_output_policy(current, voice(256), AgentTrack(audio_abr=128), other)
        self.assertEqual(one["opus_direct"], "auto")
        self.assertEqual(two["opus_direct"], "off")
        self.assertEqual(one["bitrate_kbps"], two["bitrate_kbps"])
        self.assertTrue(one["fec"])
        self.assertEqual(one["expected_packet_loss"], 0.15)
        self.assertTrue(current.direct_pcm_volume_enabled)

    def test_explicit_off_overrides_global_direct_default(self):
        current = agent(audio_output_default_opus_direct="auto")
        state = GuildMusicState(1, audio_output_settings={"opus_direct": "off"})
        self.assertEqual(audio_output_policy(current, voice(256), state=state)["opus_direct"], "off")

    def test_guild_override_preserves_explicit_zero_and_false(self):
        state = GuildMusicState(1, audio_output_settings={"fec": False, "expected_packet_loss": 0},
                                audio_output_revision=7)
        result = audio_output_policy(agent(), voice(384), state=state)
        self.assertFalse(result["fec"])
        self.assertEqual(result["expected_packet_loss"], 0.0)
        self.assertEqual(result["revision"], 7)

    def test_policy_reads_do_not_mutate_settings_or_other_guild(self):
        state = GuildMusicState(1, audio_output_settings={"profile": "quality_192"})
        other = GuildMusicState(2)
        before = dict(state.audio_output_settings)
        first = audio_output_policy(agent(), voice(384), state=state)
        first["profile"] = "changed-by-caller"
        self.assertEqual(state.audio_output_settings, before)
        self.assertEqual(state.audio_output_revision, 0)
        self.assertEqual(audio_output_policy(agent(), voice(384), state=other)["profile"], "balanced")

    def test_env_defaults_are_overridden_only_by_supplied_guild_settings(self):
        current = agent(audio_output_default_profile="quality_256", discord_opus_expected_packet_loss=0.02)
        state = GuildMusicState(1, audio_output_settings={"profile": "quality_192"})
        result = audio_output_policy(current, voice(384), state=state)
        self.assertEqual(result["bitrate_kbps"], 192)
        self.assertEqual(result["expected_packet_loss"], 0.02)


class OutputSettingValidationTests(unittest.TestCase):
    def test_partial_update_is_copy_and_preserves_other_settings(self):
        old = {"profile": "balanced", "fec": True, "expected_packet_loss": 0.15}
        patch = {"profile": " quality_192 "}
        changed = validate_audio_output_settings(patch, old)
        self.assertEqual(changed, {"profile": "quality_192", "fec": True, "expected_packet_loss": 0.15})
        self.assertEqual(old["profile"], "balanced")
        self.assertEqual(patch["profile"], " quality_192 ")

    def test_rejects_unknown_fields_including_attempts_to_change_source(self):
        for patch in ({"bitrate": 512}, {"token": "private"}, {"volume": 150},
                      {"stream_url": "https://example.org/other"}, {"complexity": 10}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_audio_output_settings(patch)

    def test_rejects_non_object_settings(self):
        for value in (None, [], "balanced", 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_audio_output_settings(value)

    def test_rejects_unknown_profile(self):
        for value in ("premium", "", "quality_384", None, 192, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_audio_output_settings({"profile": value})

    def test_fec_requires_real_boolean(self):
        for value in ("false", "true", 0, 1, None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_audio_output_settings({"fec": value})

    def test_direct_mode_requires_known_string_without_changing_other_settings(self):
        for value in ("on", "true", "disabled", "", None, True, False, 0, 1, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_audio_output_settings({"opus_direct": value})
        old = {"profile": "quality_192", "fec": True, "expected_packet_loss": 0.15}
        expected = {**old, "opus_direct": "auto"}
        self.assertEqual(validate_audio_output_settings({"opus_direct": " AUTO "}, old), expected)
        self.assertNotIn("opus_direct", old)

    def test_packet_loss_rejects_nonfinite_and_out_of_range_values(self):
        for value in (math.nan, math.inf, -math.inf, -0.001, 1.001, True, False, "0.15", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_audio_output_settings({"expected_packet_loss": value})
        self.assertEqual(validate_audio_output_settings({"expected_packet_loss": 0}), {"expected_packet_loss": 0.0})
        self.assertEqual(validate_audio_output_settings({"expected_packet_loss": 1}), {"expected_packet_loss": 1.0})

    def test_capabilities_are_independent_snapshots(self):
        first = audio_output_capabilities()
        first["profiles"].append("fake")
        first["profile_target_kbps"]["quality_192"] = 999
        second = audio_output_capabilities()
        self.assertNotIn("fake", second["profiles"])
        self.assertEqual(second["profile_target_kbps"]["quality_192"], 192)
        self.assertFalse(second["loss_is_measured"])
        self.assertTrue(second["pcm_mixer_preserved"])
        self.assertEqual(second["opus_direct_modes"], ["off", "auto"])
        self.assertEqual(second["opus_direct_default"], "auto")
        self.assertTrue(second["opus_direct_persistent"])
        self.assertTrue(second["passthrough_default"])
        explicit_off = audio_output_capabilities("off")
        self.assertEqual(explicit_off["opus_direct_default"], "off")
        self.assertFalse(explicit_off["passthrough_default"])


class OutputStateTests(unittest.TestCase):
    def test_settings_and_applied_state_are_owned_per_guild(self):
        one, two = GuildMusicState(1), GuildMusicState(2)
        one.audio_output_settings["profile"] = "quality_256"
        one.audio_output_applied["bitrate_kbps"] = 256
        self.assertEqual(two.audio_output_settings, {})
        self.assertEqual(two.audio_output_applied, {})

    def test_balanced_revision_keeps_prior_format_until_an_output_change(self):
        state = GuildMusicState(1, updated_at=123, playback_token=7)
        self.assertEqual(state.state_revision(), "123.000000:7")
        state.audio_output_revision = 1
        self.assertNotEqual(state.state_revision(), "123.000000:7")

    def test_public_snapshot_whitelists_output_fields_and_copies_settings(self):
        state = GuildMusicState(1, audio_output_settings={"profile": "quality_192", "token": "private"},
                                audio_output_applied={"bitrate_kbps": 192, "stream_url": "signed-private-url"},
                                audio_output_revision=4)
        result = state.public()["audio_output"]
        self.assertEqual(result["settings"], {"profile": "quality_192"})
        self.assertEqual(result["applied"], {"bitrate_kbps": 192})
        self.assertEqual(result["revision"], 4)
        result["settings"]["profile"] = "changed"
        result["applied"]["bitrate_kbps"] = 64
        self.assertEqual(state.audio_output_settings["profile"], "quality_192")
        self.assertEqual(state.audio_output_applied["bitrate_kbps"], 192)


class OpusPassthroughDiagnosticTests(unittest.TestCase):
    def track(self, **updates):
        return AgentTrack(**{"audio_codec": "opus", "audio_sample_rate": 48000,
                            "audio_channels": 2, **updates})

    def test_default_mixer_is_not_disabled_for_passthrough(self):
        result = opus_passthrough_eligibility(agent(), self.track(), volume_percent=55)
        self.assertFalse(result["eligible"])
        for reason in ("disabled", "volume_requires_pcm", "packet_validation_required"):
            self.assertIn(reason, result["reasons"])
        self.assertNotIn("pcm_mixer_required", result["reasons"])
        self.assertEqual(result["mode"], "off")

    def test_metadata_compatible_candidate_still_requires_packet_validation(self):
        result = opus_passthrough_eligibility(
            agent(audio_output_default_opus_direct="auto"),
            self.track(), volume_percent=100)
        self.assertTrue(result["candidate"])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reasons"], ["packet_validation_required"])
        self.assertEqual(result["mode"], "auto")

    def test_incompatible_source_or_requested_processing_is_reported(self):
        current = agent(audio_output_default_opus_direct="auto")
        cases = (({"audio_codec": "aac"}, {}, "codec_not_opus"),
                 ({"audio_sample_rate": 44100}, {}, "sample_rate_not_48000"),
                 ({"audio_channels": 1}, {}, "channels_not_stereo"),
                 ({"is_live": True}, {}, "live_stream"),
                 ({"start_offset_seconds": 10}, {}, "seek_requires_pcm"),
                 ({"archive_segments": [{"order": 0}, {"order": 1}]}, {}, "segmented_archive"),
                 ({}, {"force_pcm": True}, "pcm_mixer_required"),
                 ({}, {"effects": (1, 0, 0)}, "effects_require_pcm"))
        for track_values, call_values, reason in cases:
            with self.subTest(reason=reason):
                result = opus_passthrough_eligibility(current, self.track(**track_values),
                                                     volume_percent=100, **call_values)
                self.assertFalse(result["candidate"])
                self.assertIn(reason, result["reasons"])
                self.assertFalse(result["eligible"])

    def test_custom_audio_filter_requires_pcm(self):
        current = agent(audio_output_default_opus_direct="auto",
                        ffmpeg_options="-vn -af volume=0.8")
        result = opus_passthrough_eligibility(current, self.track(), volume_percent=100)
        self.assertIn("custom_audio_filter", result["reasons"])
        self.assertFalse(result["eligible"])

    def test_single_archive_segment_can_be_validated_without_disabling_mixer(self):
        current = agent()
        state = GuildMusicState(1, audio_output_settings={"opus_direct": "auto"})
        result = opus_passthrough_eligibility(
            current, self.track(archive_segments=[{"order": 0}]), volume_percent=100, state=state)
        self.assertTrue(result["candidate"])
        self.assertTrue(current.direct_pcm_volume_enabled)
        self.assertFalse(result["eligible"])

    def test_source_abr_above_selected_output_limit_requires_pcm(self):
        current = agent(audio_output_default_opus_direct="auto")
        for source, output, expected in ((128, 96, False), (128, 128, True),
                                         (128, 256, True), (0, 96, True)):
            with self.subTest(source=source, output=output):
                result = opus_passthrough_eligibility(
                    current, self.track(audio_abr=source), volume_percent=100,
                    output_bitrate_kbps=output)
                self.assertEqual(result["candidate"], expected)
                self.assertEqual("source_bitrate_limit" in result["reasons"], not expected)

    def test_per_guild_off_takes_precedence_over_global_auto(self):
        result = opus_passthrough_eligibility(
            agent(audio_output_default_opus_direct="auto"), self.track(), volume_percent=100,
            state=GuildMusicState(1, audio_output_settings={"opus_direct": "off"}))
        self.assertEqual(result["mode"], "off")
        self.assertIn("disabled", result["reasons"])
        self.assertFalse(result["candidate"])


if __name__ == "__main__":
    unittest.main()
