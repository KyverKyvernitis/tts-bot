"""Standalone render regression without requiring Discord's HTTP client."""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class StatusRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake_discord = types.ModuleType("discord")
        cls.fake_discord.utils = SimpleNamespace(escape_mentions=lambda v:v, escape_markdown=lambda v:v)
        # The renderer only calls formatting helpers. The real Discord module is
        # substituted for this isolated test, not for application startup.
        cls.patch = patch.dict(sys.modules, {"discord": cls.fake_discord})
        cls.patch.start()
        from utility.status import render
        cls.renderer = render

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        sys.modules.pop("utility.status.render", None)

    def snapshot(self, metrics):
        return SimpleNamespace(tts=metrics, tts_available=True, collected_at=1700000000)

    def text(self, pages):
        return "\n".join(card.text for page in pages for card in page.cards)

    def test_edge_section_renders_bounded_pairs_and_recent_rates(self):
        metrics = {
            "edge_sources_enabled": True, "edge_rate_limit_429": 4,
            "edge_sources": {"10.0.0.174": {"attempts": 100, "successes": 96, "failures": 4,
                         "http_429": 3, "rate_limit_remaining_seconds": 0, "avg_synth_ms": 1020, "probing": False}},
            "edge_endpoint_telemetry": {"enabled": ["services", "bing"], "pairs": {
                "services|10.0.0.174": {"attempts": 100, "successes": 96, "failures": 4,
                "http_429": 3, "http_403": 1, "no_audio": 0, "timeouts": 0,
                "last_15m": {"attempts": 8, "http_429": 1},
                "last_60m": {"attempts": 40, "http_429": 2},
                "avg_synth_ms": 1020, "avg_first_audio_ms": 500,
                "first_audio_samples": 90, "active": 0, "unavailable_seconds": 0}
            }}
        }
        pages = self.renderer.tts_pages(self.snapshot(metrics), "edge")
        output = self.text(pages)
        self.assertIn("Edge Services", output)
        self.assertIn("Bing Speech", output)
        self.assertIn("3.00%", output)
        self.assertIn("15 min: 8", output)
        self.assertIn("1º áudio: 500 ms", output)
        for page in pages:
            self.assertLessEqual(len(page.cards), self.renderer.ROWS_PER_PAGE)
            self.assertLessEqual(sum(len(card.text) for card in page.cards), self.renderer.PAGE_TEXT_BUDGET)

    def test_missing_metrics_show_unavailable(self):
        output = self.text(self.renderer.tts_pages(self.snapshot({}), "edge"))
        self.assertIn("Ainda não há tentativas", output)


if __name__ == "__main__":
    unittest.main()
