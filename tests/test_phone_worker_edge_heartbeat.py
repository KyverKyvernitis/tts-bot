"""Offline heartbeat/registry contract for the Phone Worker's Edge capability."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE = ROOT / "deploy/termux/phone-worker/phone_worker_runtime/control_plane.py"


def load_control_plane():
    spec = importlib.util.spec_from_file_location("edge_heartbeat_control_plane", CONTROL_PLANE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EdgeHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.control = load_control_plane()
        self.base = {
            "worker_id": "edge-heartbeat-phone", "physical_worker_id": "edge-heartbeat-phone",
            "name": "Offline Edge phone", "source": "termux-phone-worker", "runtime_kind": "termux",
            "platform": "android-termux", "version": "1.11.41", "profile": "turbo",
            "roles": ["phone-worker"], "capabilities": ["tts-synth"],
            "supported_tasks": ["tts_agent_status", "tts_agent_synthesize"],
            "worker_update_transports": ["bootstrap-manifest-v2"],
            "endpoint": "http://127.0.0.1:8766", "health": {"control_plane_alive": True},
            "status": {"core_worker_network": {"failure_streak": 0}},
        }
        self.teto = {"ready": True, "enabled": True, "fingerprint": "existing-teto-bank",
                     "ttv_voices": [{"id": "kasane-teto", "ready": True}]}

    def payload(self, engines):
        agent = {"ok": True, "available": True, "synth_ready": True,
                 "available_engines": engines, "stream_protocol": 2,
                 "cache_binary_protocol": 2, "teto": copy.deepcopy(self.teto)}
        return self.control.build_payload(self.base, system={"tts_agent": agent},
                                          battery={}, network={}, updater={})

    def test_reuses_snapshot_and_keeps_teto_and_stream_protocol(self):
        before = copy.deepcopy(self.base)
        payload = self.payload(["teto", "edge"])
        agent = payload["status"]["tts_agent"]
        self.assertEqual(agent["available_engines"], ["teto", "edge"])
        self.assertEqual(agent["stream_protocol"], 2)
        self.assertEqual(agent["teto"], self.teto)
        self.assertEqual(payload["status"]["core_worker_network"], {"failure_streak": 0})
        self.assertEqual(self.base, before)

    def test_registry_accepts_heartbeat_addition_and_removal_of_edge(self):
        # Exercise the same authenticated handler as POST /core-worker/heartbeat.
        from utility.commands import workers_registry
        with tempfile.TemporaryDirectory(prefix="edge-heartbeat-") as directory:
            path = Path(directory) / "registry.json"
            registry = workers_registry.CoreWorkersRegistry(path)
            initial = self.payload(["teto"])
            pairing = registry.create_pairing(created_by_id=1, created_by_name="offline test")
            paired = registry.redeem_pairing({**initial, "code": pairing["code"]})
            for engines in (["teto"], ["teto", "edge"], ["teto"]):
                with patch.object(workers_registry, "get_core_workers_registry", return_value=registry):
                    code, result = workers_registry.core_worker_heartbeat_http(
                        {"Authorization": f"Bearer {paired['token']}"}, self.payload(engines))
                self.assertEqual(code, 200)
                self.assertTrue(result["ok"])
                worker = json.loads(path.read_text())["workers"][self.base["worker_id"]]
                agent = worker["status"]["tts_agent"]
                self.assertEqual(agent["available_engines"], engines)
                self.assertEqual(agent["stream_protocol"], 2)
                self.assertEqual(agent["teto"], self.teto)

    def test_missing_or_invalid_snapshot_does_not_invent_capabilities(self):
        for system in ({}, {"tts_agent": None}, {"tts_agent": []}):
            payload = self.control.build_payload(self.base, system=system,
                                                battery={}, network={}, updater={})
            self.assertNotIn("tts_agent", payload["status"])


if __name__ == "__main__":
    unittest.main()
