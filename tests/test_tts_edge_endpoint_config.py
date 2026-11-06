"""Endpoint switch must remain request-scoped, durable, and cheap."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cogs.tts import edge_endpoint as ep


class EndpointConfigurationTests(unittest.TestCase):
    def test_durable_atomic_settings_and_rejected_empty_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime" / "edge.json"
            config = ep.EndpointSettings(path)
            self.assertEqual(config.enabled, ("services",))
            self.assertEqual(config.save(["bing", "services"]), ("services", "bing"))
            self.assertEqual(json.loads(path.read_text())["enabled"], ["services", "bing"])
            self.assertEqual(ep.EndpointSettings(path).enabled, ("services", "bing"))
            with self.assertRaises(ValueError):
                config.save([])
            with self.assertRaises(ValueError):
                config.save(["bogus"])
            with self.assertRaises(ValueError):
                config.save(["services", "services"])
            self.assertEqual(ep.EndpointSettings(path).enabled, ("services", "bing"))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_invalid_disk_config_falls_back_without_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.json"
            path.write_text('{"version":1,"enabled":[]}', encoding="utf-8")
            config = ep.EndpointSettings(path)
            self.assertEqual(config.enabled, ("services",))
            self.assertTrue(config.load_error)

    def test_per_ip_failures_and_minute_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            setting = ep.EndpointSettings(Path(directory) / "edge.json")
            setting.save(["services", "bing"])
            with patch.object(ep, "settings", setting):
                counter = ep.EdgeEndpointTelemetry()
                self.assertEqual(counter.choose("10.0.0.174"), "services")
                counter.start("services", "10.0.0.174")
                class NoAudioReceived(Exception):
                    pass
                counter.failure("services", "10.0.0.174", NoAudioReceived())
                self.assertEqual(counter.choose("10.0.0.174"), "bing")
                self.assertEqual(counter.choose("10.0.0.237"), "services")
                counter.start("bing", "10.0.0.174")
                counter.success("bing", "10.0.0.174", 450, 210)
                data = counter.snapshot()["pairs"]
                self.assertEqual(data["services|10.0.0.174"]["no_audio"], 1)
                self.assertEqual(data["bing|10.0.0.174"]["avg_first_audio_ms"], 210)
                self.assertEqual(data["bing|10.0.0.174"]["fallback_selected"], 1)
                self.assertEqual(data["bing|10.0.0.174"]["last_15m"]["successes"], 1)
                self.assertEqual(data["bing|10.0.0.174"]["active"], 0)

    def test_http_429_does_not_rotate_endpoint_on_same_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            setting = ep.EndpointSettings(Path(directory) / "edge.json")
            setting.save(["services", "bing"])
            with patch.object(ep, "settings", setting):
                counter = ep.EdgeEndpointTelemetry()
                class RateLimited(Exception):
                    status = 429
                counter.start("services", "10.0.0.174")
                counter.failure("services", "10.0.0.174", RateLimited())
                self.assertEqual(counter.choose("10.0.0.174"), "services")
                self.assertEqual(counter.snapshot()["pairs"]["services|10.0.0.174"]["http_429"], 1)


class EndpointConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_url_stays_independent_across_suspended_tasks(self):
        with patch.object(ep, "_ACTIVE_ENDPOINT", ep.contextvars.ContextVar("edge-test", default="services")):
            url = ep._ScopedWssUrl("wss://services.example/key", "wss://bing.example/key")
            gate = asyncio.Event()
            async def request(endpoint):
                with ep.edge_endpoint_scope(endpoint):
                    before = f"{url}"
                    await gate.wait()
                    after = f"{url}"
                    return before, after
            a = asyncio.create_task(request("services"))
            b = asyncio.create_task(request("bing"))
            await asyncio.sleep(0)
            gate.set()
            self.assertEqual(await a, ("wss://services.example/key",) * 2)
            self.assertEqual(await b, ("wss://bing.example/key",) * 2)
            self.assertEqual(f"{url}", "wss://services.example/key")


if __name__ == "__main__":
    unittest.main()
