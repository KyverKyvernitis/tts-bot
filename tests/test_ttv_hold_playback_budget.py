"""Long sequences retain enough playback time for their one-second holds."""
from types import SimpleNamespace
import unittest

from test_cd_arrangement_audio_contract import ContractProbe, QueueItem, queued
from cogs.tts.performance import parse_performance_text


class HoldPlaybackBudgetTests(unittest.TestCase):
    def test_long_speech_hold_sequence_cannot_expire_before_its_audio(self):
        text, segments = parse_performance_text("a" + "~" * 18 + " fim")
        item = QueueItem(1, 2, 3, text, "teto", "kasane-teto", "pt-BR", "1.0", "C4",
                         ttv_segments=segments)
        self.assertGreater(ContractProbe(".")._estimate_playback_timeout(item), 20)

    def test_one_more_hold_reserves_one_more_second(self):
        probe = ContractProbe(".")
        plain = SimpleNamespace(engine="teto", text="a", ttv_segments=None,
            advanced_nightcore_level=0, advanced_slowed_level=0, advanced_reverb_level=0)
        held = SimpleNamespace(**vars(plain))
        held.ttv_segments = [{"text": "a", "hold": True}]
        self.assertEqual(probe._estimate_playback_timeout(held)
                         - probe._estimate_playback_timeout(plain), 1)

    def test_cd_duration_already_contains_its_holds(self):
        item = queued()
        probe = ContractProbe(".")
        before = probe._estimate_playback_timeout(item)
        item.ttv_segments = [{"text": "a", "hold": True}] * 18
        self.assertEqual(probe._estimate_playback_timeout(item), before)


if __name__ == "__main__":
    unittest.main()
