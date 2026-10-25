"""Archive metadata is a candidate for validation, never proof of packet copy."""
from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest

from cogs.musica.runtime_telefone.agente.configuracao import (
    opus_archive_segments_candidate, opus_passthrough_eligibility,
)


def archive_track():
    reference = dict(guild_id=1, channel_id=2, message_id=3, attachment_id=4)
    segments = []
    for order in range(3):
        segments.append({**reference, "message_id": 3 + order, "attachment_id": 4 + order,
                         "order": order, "offset_seconds": float(order), "duration": 1.0,
                         "audio_codec": "opus", "audio_sample_rate": 48000, "audio_channels": 2,
                         "filename": f"parte-{order}.ogg", "size_bytes": 10000,
                         "sha256": str(order) * 64})
    return SimpleNamespace(archive_key="a" * 32, archive_ref=reference, archive_segments=segments,
                           duration=3.0, audio_codec="opus", audio_sample_rate=48000,
                           audio_channels=2, audio_abr=131, is_live=False, start_offset_seconds=0)


class ArchiveOpusPolicyTests(unittest.TestCase):
    def test_valid_manifest_only_allows_attempt_and_still_requires_packet_validation(self):
        track = archive_track()
        self.assertTrue(opus_archive_segments_candidate(track))
        result = opus_passthrough_eligibility(SimpleNamespace(audio_output_default_opus_direct="auto"),
                                              track, volume_percent=100, output_bitrate_kbps=256)
        self.assertTrue(result["candidate"])
        self.assertFalse(result["eligible"])
        self.assertTrue(result["packet_validation_required"])
        self.assertNotIn("segmented_archive", result["reasons"])

    def test_foreign_duplicate_missing_or_incompatible_segments_remain_pcm(self):
        patches = ({"audio_codec": "vorbis"}, {"audio_sample_rate": 44100}, {"audio_channels": 1},
                   {"order": 0}, {"order": True}, {"offset_seconds": .5},
                   {"duration": float("nan")}, {"duration": -1}, {"duration": True},
                   {"guild_id": 10}, {"channel_id": 20}, {"attachment_id": 0},
                   {"attachment_id": True}, {"sha256": "invalid"}, {"filename": "../part.ogg"},
                   {"filename": ""}, {"size_bytes": True}, {"size_bytes": 0}, {"size_bytes": "10000"})
        for patch in patches:
            with self.subTest(patch=patch):
                track = archive_track()
                track.archive_segments[1].update(patch)
                self.assertFalse(opus_archive_segments_candidate(track))
        track = archive_track()
        track.archive_segments[1].update(message_id=3, attachment_id=4)
        self.assertFalse(opus_archive_segments_candidate(track))
        for field in ("sha256", "filename", "duration", "attachment_id"):
            track = archive_track()
            del track.archive_segments[1][field]
            self.assertFalse(opus_archive_segments_candidate(track))

    def test_unconfirmed_root_or_inconsistent_total_is_not_a_candidate(self):
        for patch in ({"archive_key": ""}, {"archive_ref": {}}, {"duration": 30},
                      {"duration": float("inf")}, {"archive_segments": []},
                      {"archive_segments": [{"order": 0}, {"order": 1}]}):
            with self.subTest(patch=patch):
                track = archive_track()
                track.__dict__.update(copy.deepcopy(patch))
                self.assertFalse(opus_archive_segments_candidate(track))
        track = archive_track()
        track.archive_ref["attachment_id"] = 99
        self.assertFalse(opus_archive_segments_candidate(track))

    def test_volume_effect_seek_and_capacity_guards_still_apply_to_archives(self):
        agent = SimpleNamespace(audio_output_default_opus_direct="auto")
        for reason, patch, args in (
                ("volume_requires_pcm", {}, {"volume_percent": 55}),
                ("effects_require_pcm", {}, {"effects": (0, 1, 0)}),
                ("seek_requires_pcm", {"start_offset_seconds": 1.0}, {}),
                ("source_bitrate_limit", {}, {"output_bitrate_kbps": 96})):
            with self.subTest(reason=reason):
                track = archive_track()
                track.__dict__.update(patch)
                options = {"volume_percent": 100, "output_bitrate_kbps": 256, **args}
                result = opus_passthrough_eligibility(agent, track, **options)
                self.assertFalse(result["candidate"])
                self.assertIn(reason, result["reasons"])


if __name__ == "__main__":
    unittest.main()
