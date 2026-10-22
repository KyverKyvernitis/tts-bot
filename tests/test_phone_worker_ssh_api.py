"""SSH health is cheap, authenticated and based on actual HTTP observations."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.body[:size]


class SSHHealthContracts(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"PHONE_WORKER_TRANSPORT": "ssh", "CORE_WORKER_ID": "phone-test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.worker = load("phone_ssh_api_test_" + str(id(self)), PHONE)

    def handler(self, path):
        handler = self.worker.WorkerHandler.__new__(self.worker.WorkerHandler)
        handler.path = path
        handler._require_auth = mock.Mock(return_value=True)
        handler._task_tts_agent_status = mock.Mock(return_value={"ok": True, "available_engines": ["edge", "teto"]})
        return handler

    def test_tts_health_preserves_identity_and_reads_transport_without_inventory(self):
        handler = self.handler("/tts-agent/health")
        connection = {"mode": "ssh", "state": "connected", "generation": 17}
        with mock.patch.object(self.worker, "_ssh_connection_snapshot", return_value=connection), \
             mock.patch.object(self.worker, "_system_status", side_effect=AssertionError("full inventory")), \
             mock.patch.object(self.worker, "_json_response") as output:
            handler.do_GET()
        payload = output.call_args.args[2]
        self.assertEqual(output.call_args.args[1], 200)
        self.assertEqual(payload["worker_id"], "phone-test")
        self.assertEqual(payload["runtime_kind"], "termux")
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["connection"], connection)
        self.assertEqual(payload["tts_agent"]["available_engines"], ["edge", "teto"])

    def test_music_health_uses_light_snapshot_and_root_ok_allows_bootstrap(self):
        handler = self.handler("/music-agent/health")
        unavailable = {"ok": False, "available": False, "configured": True, "reason": "starting"}
        with mock.patch.object(self.worker, "_music_agent_light_snapshot", return_value=unavailable), \
             mock.patch.object(self.worker, "_ssh_connection_snapshot", return_value={"mode": "ssh"}), \
             mock.patch.object(self.worker, "_system_status", side_effect=AssertionError("full inventory")), \
             mock.patch.object(self.worker, "_music_agent_snapshot", side_effect=AssertionError("dependency inventory")), \
             mock.patch.object(self.worker, "_json_response") as output:
            handler.do_GET()
        payload = output.call_args.args[2]
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["music_agent"]["ok"])
        self.assertEqual(payload["worker_id"], "phone-test")
        self.assertEqual(payload["runtime_kind"], "termux")

    def test_unauthenticated_light_health_does_not_probe_or_read_state(self):
        for path in ("/tts-agent/health", "/music-agent/health"):
            handler = self.handler(path)
            handler._require_auth.return_value = False
            with mock.patch.object(self.worker, "_ssh_connection_snapshot", side_effect=AssertionError("state read")), \
                 mock.patch.object(self.worker, "_music_agent_light_snapshot", side_effect=AssertionError("probe")), \
                 mock.patch.object(self.worker, "_json_response") as output:
                handler.do_GET()
            output.assert_not_called()
            handler._task_tts_agent_status.assert_not_called()

    def test_connection_snapshot_is_whitelisted_sanitized_and_read_only(self):
        raw = {"mode": "ssh", "state": "connected", "generation": 8, "rtt_ms": 80.4,
               "last_error": "Authorization: Bearer SENTINEL", "private_key": "DO NOT EXPOSE",
               "known_hosts": ["secret detail"], "commands": ["ssh", "host"]}
        module = SimpleNamespace(connection_snapshot=mock.Mock(return_value=raw))
        with mock.patch.object(self.worker, "_phone_worker_ssh_module", return_value=module):
            snapshot = self.worker._ssh_connection_snapshot()
        self.assertEqual(snapshot["generation"], 8)
        self.assertEqual(snapshot["rtt_ms"], 80.4)
        self.assertNotIn("SENTINEL", json.dumps(snapshot))
        self.assertNotIn("private_key", snapshot)
        self.assertNotIn("commands", snapshot)
        self.assertEqual(raw["last_error"], "Authorization: Bearer SENTINEL")

    def test_missing_transport_module_keeps_health_snapshot_available(self):
        with mock.patch.object(self.worker, "_phone_worker_ssh_module", side_effect=RuntimeError("SENTINEL")):
            result = self.worker._ssh_connection_snapshot()
        self.assertEqual(result["mode"], "ssh")
        self.assertEqual(result["state"], "not_configured")
        self.assertNotIn("SENTINEL", json.dumps(result))

    def test_legacy_mode_does_not_load_optional_ssh_module(self):
        with mock.patch.dict(os.environ, {"PHONE_WORKER_TRANSPORT": "legacy"}), \
             mock.patch.object(self.worker, "_phone_worker_ssh_module", side_effect=AssertionError("SSH module loaded")):
            self.assertEqual(self.worker._ssh_connection_snapshot(), {"mode": "legacy"})

    def test_ssh_tailscale_snapshot_runs_no_cli_and_has_no_vpn_requirement(self):
        with mock.patch.object(self.worker, "_phone_worker_telemetry_module", side_effect=AssertionError("Tailscale called")):
            result = self.worker._tailscale_snapshot()
        self.assertEqual(result["state"], "not_required")
        self.assertFalse(result["connected"])

    def test_warm_light_music_snapshot_does_not_run_an_inventory(self):
        self.worker._CONTROL_PLANE_SNAPSHOT_CACHE["music_agent_light"] = {
            "at": time.monotonic(), "data": {"ok": True, "available": True, "observed_at": time.time()}}
        with mock.patch.object(self.worker, "_phone_worker_music_bridge_module", side_effect=AssertionError("probe")):
            result = self.worker._music_agent_light_snapshot()
        self.assertTrue(result["ok"])
        self.assertLess(result["observation_age_seconds"], 1)

    def test_old_music_snapshot_is_not_advertised_ready(self):
        self.worker._CONTROL_PLANE_SNAPSHOT_CACHE["music_agent_light"] = {
            "at": time.monotonic(), "data": {"ok": True, "available": True, "observed_at": time.time() - 10}}
        result = self.worker._music_agent_light_snapshot()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "health_stale")

    def test_core_job_diagnostic_is_read_only_and_has_identity(self):
        with mock.patch.object(self.worker, "_ssh_connection_snapshot", return_value={"mode": "ssh", "state": "connected"}), \
             mock.patch.object(self.worker, "_vps_tcp_ping_snapshot", return_value={"reachable": True, "source": "http_over_ssh"}), \
             mock.patch.object(self.worker, "_system_status", side_effect=AssertionError("full inventory")):
            result = self.worker._execute_core_worker_job({"type": "ssh_connection_diagnostic"},
                max_body_bytes=1024, max_output_bytes=1024, job_timeout=5)
        self.assertEqual(result["worker_id"], "phone-test")
        self.assertTrue(result["vps_http"]["reachable"])
        self.assertIn("ssh_connection_diagnostic", self.worker.SUPPORTED_DIRECT_TASKS)
        self.assertIn("ssh_connection_diagnostic", self.worker.SUPPORTED_CORE_WORKER_JOB_TYPES)

    def test_single_new_runtime_module_is_registered(self):
        target = "phone_worker_runtime/ssh_transport.py"
        self.assertEqual(self.worker._WORKER_UPDATE_TARGETS[target], ("worker", target, 0o644))

    def local_handler(self, path, *, token="private-worker-token", authorization="", host="127.0.0.1"):
        handler = self.worker.WorkerHandler.__new__(self.worker.WorkerHandler)
        handler.path = path
        handler.client_address = (host, 12345)
        handler.server = SimpleNamespace(worker_token=token)
        handler.headers = {"Authorization": authorization}
        handler._read_json = mock.Mock(side_effect=AssertionError("unauthenticated body read"))
        return handler

    def test_ssh_loopback_local_routes_reject_before_body_or_mutation(self):
        for path in ("/local/status", "/local/profile", "/local/pair", "/local/heartbeat"):
            with self.subTest(path=path):
                handler = self.local_handler(path)
                with mock.patch.object(self.worker, "_error") as error, \
                     mock.patch.object(self.worker, "_local_agent_status_payload", side_effect=AssertionError("status read")), \
                     mock.patch.object(self.worker, "_apply_local_core_worker_profile", side_effect=AssertionError("profile mutation")), \
                     mock.patch.object(self.worker, "_pair_core_worker", side_effect=AssertionError("pairing mutation")):
                    (handler.do_GET if path == "/local/status" else handler.do_POST)()
                self.assertEqual(error.call_args.args[1], 403)
                handler._read_json.assert_not_called()

    def test_ssh_local_routes_fail_closed_without_configured_token(self):
        handler = self.local_handler("/local/profile", token="")
        with mock.patch.object(self.worker, "_error") as error:
            self.assertFalse(handler._require_local_client())
        self.assertEqual(error.call_args.args[1], 403)

    def test_ssh_authenticated_loopback_maintenance_remains_available(self):
        handler = self.local_handler("/local/status", authorization="Bearer private-worker-token")
        self.assertTrue(handler._require_local_client())

    def test_ssh_authenticated_remote_client_still_cannot_use_local_routes(self):
        handler = self.local_handler("/local/profile", authorization="Bearer private-worker-token", host="203.0.113.7")
        with mock.patch.object(self.worker, "_error") as error:
            self.assertFalse(handler._require_local_client())
        self.assertEqual(error.call_args.args[1], 403)

    def test_legacy_local_maintenance_preserves_existing_access(self):
        handler = self.local_handler("/local/status")
        with mock.patch.dict(os.environ, {"PHONE_WORKER_TRANSPORT": "legacy"}):
            self.assertTrue(handler._require_local_client())

    def test_ssh_ping_facade_never_uses_the_local_tcp_measurement(self):
        telemetry = SimpleNamespace(vps_http_ping_snapshot=mock.Mock(return_value={
            "reachable": True, "source": "http_over_ssh", "ping_ms": 101.0}),
            vps_tcp_ping_snapshot=mock.Mock(side_effect=AssertionError("fake local TCP ping")))
        with mock.patch.object(self.worker, "_phone_worker_telemetry_module", return_value=telemetry), \
             mock.patch.object(self.worker.socket, "create_connection", side_effect=AssertionError("local socket")):
            result = self.worker._vps_tcp_ping_snapshot()
        self.assertEqual(result["ping_ms"], 101.0)
        self.assertEqual(telemetry.vps_http_ping_snapshot.call_count, 1)
        telemetry.vps_tcp_ping_snapshot.assert_not_called()

    def test_cached_system_inventory_cannot_replace_a_new_connection_generation(self):
        module = load("ssh_control_plane_test", PHONE.parent / "phone_worker_runtime/control_plane.py")
        base = {"roles": [], "capabilities": [], "health": {}, "status": {"connection": {"mode": "ssh", "generation": 9}},
                "worker_update_transports": []}
        system = {"connection": {"mode": "ssh", "generation": 8}}
        payload = module.build_payload(base, system=system, battery={}, network={}, updater={})
        self.assertEqual(payload["status"]["connection"]["generation"], 9)
        self.assertEqual(system["connection"]["generation"], 8)

    def test_ssh_heartbeat_does_not_synchronously_audit_music_dependencies(self):
        control = SimpleNamespace(build_payload=lambda base, **kwargs: dict(base))
        bridge = SimpleNamespace(estender_payload=lambda payload, **kwargs: dict(payload),
                                 inactive_music_node_snapshot=lambda: {})
        with mock.patch.object(self.worker, "_phone_worker_source_hash", return_value="a" * 64), \
             mock.patch.object(self.worker, "_phone_worker_control_plane_module", return_value=control), \
             mock.patch.object(self.worker, "_music_agent_light_snapshot", return_value={"ok": True, "available": True}) as light, \
             mock.patch.object(self.worker, "_music_agent_snapshot", side_effect=AssertionError("dependency audit")), \
             mock.patch.object(self.worker, "_cached_control_plane_snapshot", return_value={}), \
             mock.patch.object(self.worker, "_bootstrap_updater_snapshot", return_value={}), \
             mock.patch.object(self.worker, "_phone_worker_music_bridge_module", return_value=bridge), \
             mock.patch.object(self.worker, "_ssh_connection_snapshot", return_value={"mode": "ssh", "generation": 10}):
            result = self.worker._core_worker_payload(host="127.0.0.1", port=8766)
        self.assertEqual(result["status"]["connection"]["generation"], 10)
        light.assert_called_once_with()
        self.assertNotEqual(result["health"].get("payload_mode"), "bootstrap")


class HTTPPingContracts(unittest.TestCase):
    def setUp(self):
        self.module = load("ssh_network_telemetry_test", PHONE.parent / "phone_worker_runtime/telemetry.py")
        self.cache = {}
        self.clock = SimpleNamespace(mono=10.0, perf=20.0, wall=100.0)
        self.calls = []

    def ping(self, opener):
        return self.module.vps_http_ping_snapshot(
            timeout=2, cache_ttl=6, auth_parts=lambda: ("http://127.0.0.1:19000", "PRIVATE-TOKEN", "phone-test"),
            ping_cache=self.cache, monotonic=lambda: self.clock.mono,
            perf_counter=lambda: self.clock.perf, wall_clock=lambda: self.clock.wall,
            request_factory=lambda url, **kwargs: (url, kwargs), urlopen=opener,
            mask_ipv4=lambda host: host, short_text=lambda text, **kwargs: str(text))

    def test_http_body_is_read_before_rtt_is_reported_and_success_is_cached(self):
        def open_url(request, timeout):
            self.calls.append((request, timeout))
            self.clock.perf += .080
            return Response(b'{"ok":true,"service":"osaka-phone-ssh","worker_id":"phone-test"}')
        first = self.ping(open_url)
        self.assertEqual(first["source"], "http_over_ssh")
        self.assertEqual(first["ping_ms"], 80.0)
        self.assertTrue(first["reachable"])
        self.assertIn("/core-worker/agent/ping?worker_id=phone-test", self.calls[0][0][0])
        self.assertEqual(self.calls[0][0][1]["headers"]["Authorization"], "Bearer PRIVATE-TOKEN")
        self.assertNotIn("PRIVATE-TOKEN", json.dumps(first))
        self.clock.mono += 2
        cached = self.ping(lambda *a, **kw: self.fail("cache should avoid HTTP"))
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["sample_age_seconds"], 2.0)

    def test_local_listener_without_remote_response_is_not_reachable(self):
        def fail(*args, **kwargs):
            raise TimeoutError("private-token-in-error")
        result = self.ping(fail)
        self.assertFalse(result["reachable"])
        self.assertNotIn("ping_ms", result)
        self.assertNotIn("private-token-in-error", json.dumps(result))

    def test_invalid_or_oversized_http_payload_does_not_produce_rtt(self):
        for body in (b"not JSON", b"[]", b"x" * (256 * 1024 + 1)):
            self.cache.clear()
            result = self.ping(lambda *args, **kwargs: Response(body))
            self.assertFalse(result["reachable"])
            self.assertNotIn("vps_ping_ms", result)


class MusicLightProbeContracts(unittest.TestCase):
    def setUp(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import cogs.musica.runtime_telefone.ponte_worker.telemetria as module
        self.module = module
        self.env = mock.patch.dict(os.environ, {
            "MUSIC_AGENT_TOKEN": "MUSIC-SECRET", "MUSIC_AGENT_HOST": "127.0.0.1", "MUSIC_AGENT_PORT": "8780"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.hooks = SimpleNamespace(load_runtime_env=mock.Mock(), env_float=lambda key, default: default)

    def test_music_light_probe_keeps_readiness_without_dependency_inventory(self):
        body = {"ok": True, "discord_ready": True, "version": "0.3.90", "executor_mode": "voice",
                "voice_dependencies": {"ok": True, "missing": []},
                "guilds": {"123": {"status": "playing", "queue": ["secret text"], "url": "https://signed"}}}
        opener = SimpleNamespace(open=mock.Mock(return_value=Response(json.dumps(body).encode())))
        with mock.patch.object(self.module, "music_voice_dependencies_snapshot", side_effect=AssertionError("inventory")), \
             mock.patch.object(self.module.urllib.request, "build_opener", return_value=opener):
            result = self.module.music_agent_health_snapshot(self.hooks)
        self.assertTrue(result["ok"])
        self.assertTrue(result["available"])
        self.assertEqual(result["guilds"], {"123": {"status": "playing"}})
        self.assertNotIn("MUSIC-SECRET", json.dumps(result))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer MUSIC-SECRET")

    def test_missing_music_token_never_installs_or_persists_a_new_token(self):
        with mock.patch.dict(os.environ, {"MUSIC_AGENT_TOKEN": ""}), \
             mock.patch.object(self.module.urllib.request, "build_opener", side_effect=AssertionError("network")):
            result = self.module.music_agent_health_snapshot(self.hooks)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "music_agent_token_missing")

    def test_remote_music_endpoint_is_rejected_without_sending_credentials(self):
        with mock.patch.dict(os.environ, {"MUSIC_AGENT_HOST": "example.org"}), \
             mock.patch.object(self.module.urllib.request, "build_opener", side_effect=AssertionError("network")):
            result = self.module.music_agent_health_snapshot(self.hooks)
        self.assertEqual(result["reason"], "invalid_local_music_endpoint")


if __name__ == "__main__":
    unittest.main()
