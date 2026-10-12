"""Regression tests for Edge synthesis endpoint and safe failure diagnostics."""
from __future__ import annotations

import ast
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from cogs.tts.edge_endpoint import configure_edge_endpoint


ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "cogs/tts/audio.py"
STREAMING = ROOT / "cogs/tts/streaming.py"
LEGACY = (
    "wss://speech.platform.bing.com/consumer/speech/synthesize/"
    "readaloud/edge/v1?TrustedClientToken=DEMO"
)
UPDATED = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    "?Ocp-Apim-Subscription-Key=DEMO"
)


class EndpointTests(unittest.TestCase):
    def _configure_with(self, url, *, key="DEMO"):
        fake_package = types.ModuleType("edge_tts")
        fake_package.__path__ = []
        cm = types.ModuleType("edge_tts.communicate")
        cm.WSS_URL = url
        constants = types.ModuleType("edge_tts.constants")
        constants.TRUSTED_CLIENT_TOKEN = key
        fake_package.communicate = cm
        fake_package.constants = constants
        modules = {
            "edge_tts": fake_package,
            "edge_tts.communicate": cm,
            "edge_tts.constants": constants,
        }
        return cm, patch.dict(sys.modules, modules)

    def test_legacy_endpoint_changes_to_verified_endpoint(self):
        cm, environment = self._configure_with(LEGACY)
        with environment:
            self.assertTrue(configure_edge_endpoint())
            self.assertEqual(cm.WSS_URL, UPDATED)
            self.assertTrue(configure_edge_endpoint())
            self.assertEqual(cm.WSS_URL, UPDATED)

    def test_unrecognized_future_endpoint_is_not_overwritten(self):
        future = "wss://future.example.net/stream?key=a"
        cm, environment = self._configure_with(future)
        with environment:
            self.assertFalse(configure_edge_endpoint())
        self.assertEqual(cm.WSS_URL, future)

    def test_missing_token_does_not_rewrite_endpoint(self):
        cm, environment = self._configure_with(LEGACY, key="")
        with environment:
            self.assertFalse(configure_edge_endpoint())
        self.assertEqual(cm.WSS_URL, LEGACY)

    def test_normal_and_progressive_synthesis_share_communicate(self):
        audio = ast.parse(AUDIO.read_text(encoding="utf-8"))
        streaming = ast.parse(STREAMING.read_text(encoding="utf-8"))
        calls = []
        for tree in (audio, streaming):
            calls.extend(
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Communicate"
            )
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any(
            isinstance(node.value, ast.Name) and node.value.id == "edge_tts"
            for node in (call.func for call in calls)
        ))
        # The configuration is invoked at module import, before speech starts.
        self.assertIn("configure_edge_endpoint()", AUDIO.read_text(encoding="utf-8"))

    def test_sensitive_websocket_url_does_not_enter_journal_diagnostics(self):
        source = AUDIO.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(AUDIO))
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_tts_safe_exception_detail"
        )
        module = ast.Module(body=[fn], type_ignores=[])
        ns = {"re": re, "BaseException": BaseException}
        exec(compile(module, str(AUDIO), "exec"), ns)
        sensitive = (
            "403, url='wss://api.msedgeservices.com/tts/cognitiveservices/"
            "websocket/v1?Ocp-Apim-Subscription-Key=SECRET_VALUE'"
        )
        result = ns["_tts_safe_exception_detail"](RuntimeError(sensitive))
        self.assertIn("403", result)
        self.assertIn("[url]", result)
        self.assertNotIn("SECRET_VALUE", result)
        self.assertNotIn("Ocp-Apim", result)
        self.assertNotIn("api.msedgeservices.com", result)
        # The common error path and persistent metric both use redacted details.
        self.assertIn('engine_metrics["last_error"] = _tts_safe_exception_detail(error)', source)
        self.assertIn("stream_reason,\n                    _tts_safe_exception_detail(exc)", source)


if __name__ == "__main__":
    unittest.main()
