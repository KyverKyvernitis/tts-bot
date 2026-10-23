"""The SSH transport uses only owned masters and preserves worker execution.

SSH/HTTP are simulated at their boundaries: no network, real server or provider
is needed to exercise migration, recovery, key pinning and state persistence.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy/termux/phone-worker/phone_worker_runtime/ssh_transport.py"


def key(seed=1):
    raw = b"".join(struct.pack("!I", len(x)) + x for x in (b"ssh-ed25519", bytes([seed]) * 32))
    return "ssh-ed25519 " + base64.b64encode(raw).decode()


class SSHTransportContracts(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("ssh_transport_contract_" + str(id(self)), SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory(prefix="osaka-ssh-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env_path = self.home / ".phone-worker.env"
        self.env_path.write_text(
            "CORE_WORKER_ID=phone-test\nCORE_WORKER_TOKEN=CORE-SENTINEL\n"
            "PHONE_WORKER_TOKEN=PHONE-SENTINEL\nPHONE_WORKER_TRANSPORT=ssh\n"
            "TTS_EDGE_SOURCE_IPS=10.0.0.174,10.0.0.237\nCUSTOM_SETTING='a b'\n"
        )
        self.env_path.chmod(0o600)
        self.env = {
            "PHONE_WORKER_ENV": str(self.env_path),
            "CORE_WORKER_ID": "phone-test", "CORE_WORKER_TOKEN": "CORE-SENTINEL",
            "PHONE_WORKER_TOKEN": "PHONE-SENTINEL", "PHONE_WORKER_TRANSPORT": "ssh",
            "CORE_WORKER_SSH_PRIVATE_DIR": str(self.home / ".ssh/osaka"),
            "PHONE_WORKER_STATE_DIR": str(self.home / "state"),
            "PHONE_WORKER_DIR": str(self.home / "phone-worker"),
        }
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(self.module, "environment", side_effect=lambda extra=None: {**self.env, **(extra or {})}))
        self.config = {
            "schema": self.module.SCHEMA, "worker_id": "phone-test", "ssh_host": "vps.example.org",
            "ssh_port": 22, "ssh_user": "osaka-phone-ssh", "ssh_host_key": key(1),
            "remote_port": 18766, "bulk_remote_port": 18767, "local_port": 19000, "vps_api_port": 10000,
        }
        self.locations = self.module.paths(self.env)
        self.module.private_dir(self.locations["private"])
        self.module.private_dir(self.locations["state_root"])
        self.module.atomic_write(self.locations["config"], self.module.json_bytes(self.config))
        self.module.atomic_write(self.locations["identity"], b"FAKE PRIVATE KEY NEVER EMITTED")
        self.module.atomic_write(self.locations["known_hosts"], "# " + self.module.MARKER + "\nvps.example.org " + key(1) + "\n")

    def write_state(self, value):
        self.module.atomic_write(self.locations["state"], self.module.json_bytes(value))

    def state(self):
        return self.module.read_json(self.locations["state"])

    def connected_state(self, *, worker_port=8766, now=100):
        return {
            "mode": "ssh", "state": "connected", "generation": 3, "reconnect_count": 1,
            "updated_at": now, "last_ok_at": now, "rtt_ms": 92.2,
            "interactive_state": "connected", "bulk_state": "connected",
            "worker_ready": True, "vps_ready": True,
            "channels": {name: {"pid": 510 + i, "started_at": now - 20,
                "worker_port": worker_port, "reverse_ready": True, "state": "connected"}
                for i, name in enumerate(("interactive", "bulk"))},
        }

    def worker_body(self):
        return {"ok": True, "worker_id": "phone-test", "runtime_kind": "termux",
                "tts_agent": {"ok": True, "available_engines": ["edge", "teto"], "stream_protocol": 2}}

    def test_public_bundle_accepts_wrapper_but_rejects_options_and_overlay_host(self):
        self.assertEqual(self.module.bundle_config({"ok": True, "bundle": self.config}), self.config)
        for value in ("-oProxyCommand=sh", "host;touch-file", "http://vps.example.org", "100.103.240.118", "phone.tailnet.ts.net"):
            with self.subTest(host=value), self.assertRaises(ValueError):
                self.module.bundle_config({**self.config, "ssh_host": value})

    def test_bundle_rejects_ssh_option_injection_user_and_duplicate_ports(self):
        for update in ({"ssh_user": "root -oProxyCommand=sh"}, {"remote_port": 22},
                       {"bulk_remote_port": 18766}, {"local_port": 0}, {"worker_id": "../other"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.module.bundle_config({**self.config, **update})

    def test_public_key_validation_rejects_authorized_key_options_or_wrong_type(self):
        self.assertEqual(self.module.public_key(key() + " comment"), key())
        for value in ('command="sh" ' + key(), "ssh-rsa AAAA", key() + "\n" + key(2), "ssh-ed25519 !!!!"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.public_key(value)

    def test_literal_env_is_parsed_without_running_shell(self):
        marker = self.home / "must-not-exist"
        candidate = self.home / "literal.env"
        candidate.write_text("VALUE='with spaces'\nLITERAL='$(touch " + str(marker) + ")'\n# ignored\n")
        result = self.module.read_env(candidate)
        self.assertEqual(result["VALUE"], "with spaces")
        self.assertIn("$(touch", result["LITERAL"])
        self.assertFalse(marker.exists())

    def test_prepare_preserves_private_key_and_emits_only_public_material(self):
        bundle = self.home / "bundle.json"
        bundle.write_text(json.dumps({"ok": True, "bundle": self.config}))
        before = self.locations["identity"].read_bytes()
        with mock.patch.object(self.module, "run", return_value=SimpleNamespace(returncode=0, stdout=key(2) + "\n")) as command:
            result = self.module.prepare(bundle, self.env)
        self.assertEqual(self.locations["identity"].read_bytes(), before)
        self.assertEqual(result["client_public_key"], key(2))
        self.assertNotIn(before.decode(), json.dumps(result))
        self.assertEqual(command.call_count, 1)
        self.assertIn("-y", command.call_args.args[0])
        self.assertEqual(stat.S_IMODE(self.locations["known_hosts"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.locations["config"].stat().st_mode), 0o600)

    def test_prepare_rejects_changed_pinned_host_without_modifying_key_or_config(self):
        bundle = self.home / "bundle.json"
        bundle.write_text(json.dumps({**self.config, "ssh_host_key": key(3)}))
        before = {name: path.read_bytes() for name, path in self.locations.items() if path.is_file()}
        with mock.patch.object(self.module, "run", side_effect=AssertionError("must not generate/replace key")), self.assertRaises(ValueError):
            self.module.prepare(bundle, self.env)
        for name, raw in before.items():
            self.assertEqual(self.locations[name].read_bytes(), raw)

    def test_private_file_rejects_group_access_hard_link_and_symbolic_link(self):
        path = self.home / "private"
        path.write_text("private")
        path.chmod(0o640)
        with self.assertRaises(PermissionError):
            self.module.private_file(path)
        path.chmod(0o600)
        alias = self.home / "alias"
        os.link(path, alias)
        with self.assertRaises(PermissionError):
            self.module.private_file(path)
        alias.unlink()
        alias.symlink_to(path)
        with self.assertRaises(PermissionError):
            self.module.private_file(alias)

    def test_private_directory_refuses_symlink_without_chmodding_target(self):
        target = self.home / "target"
        target.mkdir(mode=0o755)
        alias = self.home / "alias"
        alias.symlink_to(target, target_is_directory=True)
        mode = stat.S_IMODE(target.stat().st_mode)
        with self.assertRaises(PermissionError):
            self.module.private_dir(alias / "child")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
        self.assertFalse((target / "child").exists())

    def test_channels_use_separate_masters_and_only_bulk_has_vps_forward(self):
        with mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/ssh"):
            interactive = self.module.master_command(self.config, self.locations, "interactive", 8766)
            bulk = self.module.master_command(self.config, self.locations, "bulk", 8766)
        self.assertNotEqual(interactive[interactive.index("-S") + 1], bulk[bulk.index("-S") + 1])
        for args in (interactive, bulk):
            for required in ("Compression=no", "StrictHostKeyChecking=yes", "ExitOnForwardFailure=yes", "IdentitiesOnly=yes", "BatchMode=yes"):
                self.assertIn(required, args)
            self.assertIn("-N", args)
            self.assertIn("-T", args)
            self.assertNotIn("-D", args)
            self.assertEqual(args[-1], "osaka-phone-ssh@vps.example.org")
        self.assertNotIn("-L", interactive)
        self.assertIn("127.0.0.1:19000:127.0.0.1:10000", bulk)
        self.assertIn("127.0.0.1:18766:127.0.0.1:8766", interactive)
        self.assertIn("127.0.0.1:18767:127.0.0.1:8766", bulk)

    def test_control_path_must_fit_unix_socket_limit(self):
        locations = {**self.locations, "private": Path("/" + "a" * 110)}
        with self.assertRaises(ValueError):
            self.module.control_path(self.config, locations, "interactive")

    def test_native_openssh_accepts_forward_and_security_options_without_connecting(self):
        if not shutil.which("ssh"):
            self.skipTest("OpenSSH client is not installed")
        args = self.module.master_command(self.config, self.locations, "bulk", 8766)
        args.insert(-1, "-G")  # Print evaluated configuration; no network I/O.
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(line.partition(" ")[::2] for line in result.stdout.splitlines())
        self.assertEqual(values["compression"], "no")
        self.assertIn(values["stricthostkeychecking"], {"true", "yes"})
        self.assertEqual(values["serveraliveinterval"], "5")
        self.assertEqual(values["serveralivecountmax"], "2")
        self.assertIn("19000", values["localforward"])
        self.assertIn("18767", values["remoteforward"])

    def test_move_reverse_does_not_exit_master_or_cancel_vps_local_forward(self):
        with mock.patch.object(self.module, "run", return_value=SimpleNamespace(returncode=0)) as command:
            self.module.move_reverse(self.config, self.locations, "bulk", 8766, 8768)
        self.assertEqual(command.call_count, 2)
        commands = [call.args[0] for call in command.call_args_list]
        self.assertIn("cancel", commands[0]); self.assertIn("forward", commands[1])
        self.assertIn("127.0.0.1:18767:127.0.0.1:8768", commands[1])
        self.assertFalse(any("exit" in args or "-L" in args for args in commands))

    def test_failed_reverse_update_is_reported_without_silent_success(self):
        with mock.patch.object(self.module, "run", side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=255)]):
            with self.assertRaises(RuntimeError):
                self.module.move_reverse(self.config, self.locations, "interactive", 8766, 8768)

    def test_running_channels_keep_generation_stable_and_do_not_spawn(self):
        self.write_state(self.connected_state())
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", return_value=({"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}, 83.4)), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("duplicate master")), \
             mock.patch.object(self.module, "move_reverse", side_effect=AssertionError("unnecessary rebind")):
            result = self.module.ensure(self.env)
        self.assertEqual(result["generation"], 3)
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["rtt_ms"], 83.4)
        self.assertTrue(result["vps_ready"])

    def test_cold_boot_vps_ready_does_not_depend_on_worker_backend(self):
        self.write_state(self.connected_state())
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, None)), \
             mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", return_value=({"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}, 91.1)), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("duplicate master")):
            result = self.module.ensure(self.env)
        self.assertTrue(result["vps_ready"])
        self.assertFalse(result["worker_ready"])
        self.assertEqual(result["state"], "worker_unavailable")

    def test_vps_liveness_is_authenticated_and_does_not_depend_on_bot_cogs(self):
        response = {"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh", "timestamp": 100}
        with mock.patch.object(self.module, "http_json", return_value=(response, 83)) as probe:
            result, latency = self.module.vps_ping(self.config, self.env)
        self.assertEqual(result, response)
        self.assertEqual(latency, 83)
        self.assertIn("127.0.0.1:19000/core-worker/agent/ping?worker_id=phone-test", probe.call_args.args[0])
        self.assertEqual(probe.call_args.args[1], "CORE-SENTINEL")
        self.assertNotIn("healthy", response)

    def test_wrong_vps_identity_does_not_confirm_liveness(self):
        self.write_state(self.connected_state())
        response = {"ok": True, "worker_id": "another-phone", "service": "osaka-phone-ssh"}
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", return_value=(response, 1)), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("duplicate master")):
            result = self.module.ensure(self.env)
        self.assertFalse(result["vps_ready"])
        self.assertIsNone(result["rtt_ms"])
        self.assertEqual(result["state"], "reconnecting")

    def test_parallel_ensure_does_not_spawn_while_another_ensure_owns_lock(self):
        self.write_state(self.connected_state())
        with self.locations["lock"].open("w+") as lock:
            lock_path_mode = stat.S_IMODE(self.locations["lock"].stat().st_mode)
            self.locations["lock"].chmod(0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(self.module.time, "time", return_value=100), \
                 mock.patch.object(self.module, "local_worker", side_effect=AssertionError("concurrent health")), \
                 mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("concurrent master")):
                result = self.module.ensure(self.env)
        self.assertEqual(result["generation"], 3)

    def test_port_8768_is_applied_to_both_reverse_channels_without_restarting(self):
        self.write_state(self.connected_state())
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8768, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", return_value=({"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}, 60)), \
             mock.patch.object(self.module, "move_reverse") as rebind, \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("must preserve masters")), \
             mock.patch.object(self.module.os, "kill", side_effect=AssertionError("must preserve worker/music")):
            result = self.module.ensure(self.env)
        self.assertEqual(rebind.call_count, 2)
        self.assertEqual({call.args[2] for call in rebind.call_args_list}, {"interactive", "bulk"})
        self.assertTrue(all(call.args[3:] == (8766, 8768) for call in rebind.call_args_list))
        self.assertEqual(result["generation"], 4)

    def test_initial_connection_spawns_only_two_owned_ssh_clients(self):
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, None)), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=[SimpleNamespace(pid=1001), SimpleNamespace(pid=1002)]) as spawn:
            result = self.module.ensure(self.env)
        self.assertEqual(spawn.call_count, 2)
        for call in spawn.call_args_list:
            self.assertNotIn("shell", call.kwargs)
            self.assertTrue(call.kwargs["start_new_session"])
            self.assertIn("-N", call.args[0])
            self.assertFalse(any("phone_worker.py" in value for value in call.args[0]))
        self.assertEqual(result["generation"], 1)
        self.assertFalse(result["vps_ready"])

    def test_recovery_backoff_does_not_spawn_or_signal_unowned_process(self):
        state = self.connected_state(now=100)
        state["channels"]["interactive"]["pid"] = 666
        self.write_state(state)
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", side_effect=lambda c, p, name: name == "bulk"), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.random, "uniform", return_value=1), \
             mock.patch.object(self.module, "http_json", return_value=({"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}, 80)), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("backoff not elapsed")), \
             mock.patch.object(self.module.os, "kill", side_effect=AssertionError("unowned process")):
            result = self.module.ensure(self.env)
        self.assertEqual(result["interactive_state"], "reconnecting")
        self.assertEqual(self.state()["channels"]["interactive"]["next_retry"], 110)
        self.assertEqual(result["generation"], 3)

    def test_long_outage_bounds_both_channels_and_keeps_pairing_and_workers(self):
        clock = {"now": 100.0}
        starts = []
        before = {name: self.locations[name].read_bytes() for name in ("config", "identity", "known_hosts")}
        env_before = self.env_path.read_bytes()
        def spawn(args, **kwargs):
            starts.append((clock["now"], "bulk" if "-L" in args else "interactive"))
            return SimpleNamespace(pid=1000000 + len(starts))
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module.random, "uniform", return_value=1.2), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.os, "kill", side_effect=AssertionError("must preserve worker/music")), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=spawn):
            # Each invocation reloads the persistent state, as the watchdog does.
            for seconds in range(0, 1801, 5):
                clock["now"] = 100 + seconds
                self.module.ensure(self.env)
        self.assertEqual(starts[:2], [(100, "interactive"), (100, "bulk")])
        self.assertGreater(len(starts), 20)
        for endpoint, _ in starts:
            self.assertLessEqual(sum(endpoint - 35 < at <= endpoint for at, _ in starts), 4)
        for channel in ("interactive", "bulk"):
            times = [at for at, name in starts if name == channel]
            self.assertTrue(all(b - a >= 60 for a, b in zip(times[-5:], times[-4:])))
        self.assertTrue(self.state()["worker_ready"])
        self.assertFalse(self.state()["vps_ready"])
        for name, raw in before.items():
            self.assertEqual(self.locations[name].read_bytes(), raw)
        self.assertEqual(self.env_path.read_bytes(), env_before)

    def test_reconnect_does_not_reset_shared_budget_or_channel_deadline(self):
        self.write_state({"updated_at": 120, "ssh_starts": [100, 100, 110, 110],
                          "channels": {"interactive": {"next_retry": 125}}})
        with mock.patch.object(self.module.time, "time", return_value=120), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("shared budget exhausted")):
            self.module.reconnect(self.env)
            self.assertEqual(self.state()["channels"]["interactive"]["next_retry"], 125)
            self.module.ensure(self.env)
        self.assertEqual(self.state()["ssh_starts"], [100, 100, 110, 110])
        self.assertEqual(self.state()["channels"]["bulk"]["next_retry"], 135)
        with mock.patch.object(self.module.time, "time", return_value=135), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=[SimpleNamespace(pid=1001), SimpleNamespace(pid=1002)]) as spawn:
            self.module.ensure(self.env)
        self.assertEqual(spawn.call_count, 2)

    def test_start_reservation_survives_interrupted_ensure(self):
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=KeyboardInterrupt) as spawn:
            for _ in range(4):
                with self.assertRaises(KeyboardInterrupt):
                    self.module.ensure(self.env)
            result = self.module.ensure(self.env)
        self.assertEqual(spawn.call_count, 4)
        self.assertEqual(self.state()["ssh_starts"], [100] * 4)
        self.assertEqual(result["state"], "reconnecting")

    def test_slow_health_checks_budget_actual_launch_times(self):
        clock = {"now": 0}
        starts = []
        def worker(config, env):
            if clock["now"] == 0:
                clock["now"] = 7
            return 8766, self.worker_body()
        def spawn(args, **kwargs):
            starts.append(clock["now"])
            return SimpleNamespace(pid=1000000 + len(starts))
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module, "local_worker", side_effect=worker), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=spawn):
            self.module.ensure(self.env)
            self.assertEqual(self.state()["channels"]["bulk"]["started_at"], 7)
            # Admission remains global even if channel records are recreated.
            for second in (10, 35):
                clock["now"] = second
                state = self.state()
                state["channels"] = {}
                self.write_state(state)
                self.module.ensure(self.env)
        self.assertEqual(starts, [7, 7, 10, 10])
        for endpoint in starts:
            self.assertLessEqual(sum(endpoint - 35 < at <= endpoint for at in starts), 4)

    def test_failed_interactive_channel_preserves_working_bulk_and_vps(self):
        self.write_state(self.connected_state())
        clock = {"now": 100}
        starts = []
        def spawn(args, **kwargs):
            self.assertNotIn("-L", args)
            starts.append(clock["now"])
            return SimpleNamespace(pid=1000000 + len(starts))
        response = {"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module.random, "uniform", return_value=1), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", side_effect=lambda c, p, name: name == "bulk"), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module, "http_json", return_value=(response, 1)), \
             mock.patch.object(self.module, "run", side_effect=AssertionError("must preserve working bulk")), \
             mock.patch.object(self.module.os, "kill", side_effect=AssertionError("must not signal worker/music")), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=spawn):
            for second in range(100, 401, 5):
                clock["now"] = second
                result = self.module.ensure(self.env)
                self.assertTrue(result["vps_ready"])
                self.assertEqual(result["bulk_state"], "connected")
        self.assertEqual(self.state()["channels"]["bulk"]["pid"], 511)
        self.assertGreater(len(starts), 3)

    def test_failed_attempt_does_not_reuse_stderr_from_an_older_process(self):
        for channel in ("interactive", "bulk"):
            self.module.atomic_write(self.locations["private"] / (channel + ".log"), "old failure must not be reused\n")
        clock = {"now": 100}
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=[SimpleNamespace(pid=1001), SimpleNamespace(pid=1002)]):
            self.module.ensure(self.env)
            clock["now"] = 105
            result = self.module.ensure(self.env)
        self.assertIn("SSH encerrou", result["last_error"])
        self.assertNotIn("old failure", result["last_error"])

    def test_clock_reversal_preserves_budget_and_bounds_existing_deadlines(self):
        self.write_state({"updated_at": 1000, "ssh_starts": [995] * 4,
                          "channels": {"interactive": {"next_retry": 1005}}})
        with mock.patch.object(self.module.time, "time", return_value=-100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("must retain window")):
            self.module.ensure(self.env)
        state = self.state()
        self.assertEqual(state["ssh_starts"], [-105] * 4)
        self.assertEqual(state["channels"]["interactive"]["next_retry"], -95)
        self.assertEqual(state["channels"]["bulk"]["next_retry"], -70)
        with mock.patch.object(self.module.time, "time", return_value=-65), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=[SimpleNamespace(pid=1001), SimpleNamespace(pid=1002)]) as spawn:
            self.module.ensure(self.env)
        self.assertEqual(spawn.call_count, 2)

    def test_legacy_future_deadline_cannot_block_recovery_indefinitely(self):
        self.write_state({"channels": {"interactive": {"next_retry": 10 ** 20}}})
        with mock.patch.object(self.module.time, "time", return_value=100), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", return_value=SimpleNamespace(pid=1001)) as spawn:
            self.module.ensure(self.env)
        self.assertEqual(spawn.call_count, 1)  # Only the other channel starts.
        self.assertEqual(self.state()["channels"]["interactive"]["next_retry"], 160)

    def test_failure_budget_resets_only_after_sustained_authenticated_readiness(self):
        state = self.connected_state()
        for record in state["channels"].values():
            record["failures"] = 3
        self.write_state(state)
        clock = {"now": 100}
        response = {"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", return_value=(response, 1)) as probe, \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("must retain masters")):
            for second in (100, 105, 110, 120, 130, 139, 140):
                clock["now"] = second
                probe.side_effect = OSError("temporary network failure") if second == 105 else None
                self.module.ensure(self.env)
                expected = 0 if second == 140 else 3
                self.assertEqual(self.state()["channels"]["bulk"]["failures"], expected)

    def test_watchdog_suspension_requires_new_readiness_samples(self):
        state = self.connected_state()
        state["healthy_since"] = 80
        for record in state["channels"].values():
            record["failures"] = 3
        self.write_state(state)
        clock = {"now": 200}
        response = {"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", return_value=(response, 1)):
            for second in (200, 215, 230):
                clock["now"] = second
                self.module.ensure(self.env)
                self.assertEqual(self.state()["channels"]["bulk"]["failures"], 0 if second == 230 else 3)

    def test_failed_master_surfaces_sanitized_stderr_and_preserves_it_while_waiting(self):
        clock = {"now": 100}
        def spawn(args, **kwargs):
            kwargs["stderr"].write(b"ssh: Connection refused Bearer CORE-SENTINEL PHONE-SENTINEL\n")
            return SimpleNamespace(pid=1001 if "-L" in args else 1002)
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module.random, "uniform", return_value=1), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=False), \
             mock.patch.object(self.module, "owned_pid", return_value=False), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=spawn):
            self.module.ensure(self.env)
            clock["now"] = 105
            result = self.module.ensure(self.env)
            self.assertIn("Connection refused", result["last_error"])
            self.assertNotIn("SENTINEL", json.dumps(result))
            clock["now"] = 110
            self.assertEqual(self.module.ensure(self.env)["last_error"], result["last_error"])

    def test_unresponsive_api_does_not_interrupt_live_ssh_or_transfers(self):
        self.write_state(self.connected_state())
        clock = {"now": 100}
        with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", side_effect=urllib.error.URLError("connection refused")), \
             mock.patch.object(self.module, "run", side_effect=AssertionError("API failure must not interrupt SSH transfers")), \
             mock.patch.object(self.module.os, "kill", side_effect=AssertionError("must not signal worker/music")), \
             mock.patch.object(self.module.subprocess, "Popen", side_effect=AssertionError("must not duplicate live masters")):
            for second in range(100, 701, 5):
                clock["now"] = second
                result = self.module.ensure(self.env)
                self.assertEqual(result["state"], "reconnecting")
                self.assertFalse(result["vps_ready"])

    def test_api_errors_and_wrong_identity_do_not_recycle_live_ssh(self):
        for error in (urllib.error.HTTPError("http://localhost", 503, "restarting", {}, None),
                      ValueError("VPS não confirmou o ping autenticado")):
            with self.subTest(error=type(error).__name__):
                self.write_state(self.connected_state())
                clock = {"now": 100}
                with mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
                     mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
                     mock.patch.object(self.module, "master_alive", return_value=True), \
                     mock.patch.object(self.module, "http_json", side_effect=error), \
                     mock.patch.object(self.module, "run", side_effect=AssertionError("API response proves SSH works")):
                    for second in range(100, 701, 5):
                        clock["now"] = second
                        self.module.ensure(self.env)

    def test_local_worker_rejects_wrong_id_and_apk_before_using_fallback_port(self):
        replies = [({**self.worker_body(), "worker_id": "another-phone"}, 1),
                   ({**self.worker_body(), "runtime_kind": "apk"}, 1)]
        with mock.patch.object(self.module, "http_json", side_effect=replies):
            port, body = self.module.local_worker(self.config, self.env)
        self.assertEqual(port, 8766)
        self.assertIsNone(body)

    def test_runtime_port_hint_cannot_override_authenticated_wrong_identity(self):
        runtime = {"worker_id": "phone-test", "pid": 900, "http_port": 8768, "control_plane_alive": True}
        self.module.atomic_write(self.locations["state_root"] / "runtime-status.json", self.module.json_bytes(runtime))
        good = {**self.worker_body(), "pid": 900}
        def response(url, token="", **kwargs):
            return (good if ":8768/" in url else {**good, "worker_id": "other"}, 1)
        with mock.patch.object(self.module, "pid_alive", return_value=True), \
             mock.patch.object(self.module, "http_json", side_effect=response) as probe:
            port, body = self.module.local_worker(self.config, self.env)
        self.assertEqual(port, 8768)
        self.assertEqual(body["worker_id"], "phone-test")
        self.assertEqual(probe.call_count, 1)

    def test_snapshot_is_read_only_redacted_and_cannot_keep_stale_connection_ready(self):
        state = self.connected_state(now=100)
        state["private_key"] = "MUST NOT EXPOSE"
        state["last_error"] = "Bearer PHONE-SENTINEL"
        self.write_state(state)
        with mock.patch.object(self.module.time, "time", return_value=131), \
             mock.patch.object(self.module, "run", side_effect=AssertionError("snapshot executed SSH")), \
             mock.patch.object(self.module, "http_json", side_effect=AssertionError("snapshot probed HTTP")):
            snapshot = self.module.connection_snapshot()
        self.assertEqual(snapshot["state"], "stale")
        self.assertFalse(snapshot["worker_ready"])
        self.assertNotIn("MUST NOT EXPOSE", json.dumps(snapshot))
        self.assertNotIn("PHONE-SENTINEL", json.dumps(snapshot))

    def test_activation_preserves_tokens_source_ips_and_custom_settings(self):
        before = self.env_path.read_text()
        connected = {"vps_ready": True, "interactive_state": "connected", "state": "connected"}
        with mock.patch.object(self.module, "idle_worker"), mock.patch.object(self.module, "ensure", return_value=connected), \
             mock.patch.object(self.module, "run", side_effect=AssertionError("default activation must not restart")):
            result = self.module.activate(self.env)
        after = self.env_path.read_text()
        self.assertTrue(result["ok"])
        self.assertTrue(result["worker_restart_required"])
        self.assertFalse(result["worker_applied"])
        values = self.module.read_env(self.env_path)
        self.assertEqual(values["CORE_WORKER_VPS_URL"], "http://127.0.0.1:19000")
        self.assertEqual(values["CORE_WORKER_ENDPOINT"], "http://127.0.0.1:18766")
        self.assertEqual(values["PHONE_WORKER_TOKEN"], "PHONE-SENTINEL")
        self.assertEqual(values["CORE_WORKER_TOKEN"], "CORE-SENTINEL")
        self.assertEqual(values["TTS_EDGE_SOURCE_IPS"], "10.0.0.174,10.0.0.237")
        self.assertEqual(values["CUSTOM_SETTING"], "a b")
        self.assertEqual((self.locations["private"] / "before-ssh.env").read_text(), before)
        self.assertNotIn("PHONE-SENTINEL", json.dumps(result))

    def test_invalid_pairing_or_active_work_prevents_env_mutation(self):
        before = self.env_path.read_bytes()
        with self.assertRaises(ValueError):
            self.module.activate({**self.env, "CORE_WORKER_ID": "wrong-phone"})
        with mock.patch.object(self.module, "idle_worker", side_effect=RuntimeError("música ativa")), \
             mock.patch.object(self.module, "ensure", side_effect=AssertionError("must not modify transport")), self.assertRaises(RuntimeError):
            self.module.activate(self.env)
        self.assertEqual(self.env_path.read_bytes(), before)

    def test_apply_worker_uses_bootstrap_restart_once_and_hands_off_watch(self):
        worker = Path(self.env["PHONE_WORKER_DIR"])
        worker.mkdir()
        (worker / "phone_worker_bootstrap.py").write_text("# fixture\n")
        connected = {"vps_ready": True, "interactive_state": "connected", "state": "connected"}
        with mock.patch.object(self.module, "idle_worker"), \
             mock.patch.object(self.module, "ensure", return_value=connected), \
             mock.patch.object(self.module, "worker_has_environment", return_value=True), \
             mock.patch.object(self.module, "replace_watch") as handoff, \
             mock.patch.object(self.module, "run", return_value=SimpleNamespace(returncode=0)) as command:
            result = self.module.activate(self.env, apply_worker=True)
        self.assertTrue(result["worker_applied"])
        self.assertFalse(result["worker_restart_required"])
        command.assert_called_once()
        self.assertIn("phone_worker_bootstrap.py", command.call_args.args[0][1])
        handoff.assert_called_once()

    def test_apply_worker_restarts_only_when_the_running_environment_is_old(self):
        worker = Path(self.env["PHONE_WORKER_DIR"])
        worker.mkdir()
        (worker / "phone_worker_bootstrap.py").write_text("# fixture\n")
        connected = {"vps_ready": True, "interactive_state": "connected", "state": "connected"}
        with mock.patch.object(self.module, "idle_worker"), \
             mock.patch.object(self.module, "ensure", return_value=connected), \
             mock.patch.object(self.module, "worker_has_environment", side_effect=[False, True]), \
             mock.patch.object(self.module, "replace_watch"), \
             mock.patch.object(self.module, "run", return_value=SimpleNamespace(returncode=0)) as command:
            result = self.module.activate(self.env, apply_worker=True)
        self.assertTrue(result["worker_applied"])
        self.assertEqual(command.call_count, 2)
        self.assertIn("--force-restart", command.call_args.args[0])

    def test_watch_handoff_kills_only_confirmed_legacy_watch_if_term_is_ignored(self):
        worker = Path(self.env["PHONE_WORKER_DIR"])
        worker.mkdir()
        (worker / "phone-worker-watch.pid").write_text("12345\n")
        proc_path = Path("/proc/12345/cmdline")
        command_bytes = b"bash\x00/home/phone-worker/watch-phone-worker.sh\x00"
        alive = {"value": True}
        signals = []
        original_read = Path.read_bytes
        def read(path):
            return command_bytes if path == proc_path else original_read(path)
        def kill(pid, signum):
            signals.append((pid, signum))
            if signum == self.module.signal.SIGKILL:
                alive["value"] = False
        with mock.patch.object(Path, "read_bytes", read), \
             mock.patch.object(self.module, "pid_alive", side_effect=lambda pid: alive["value"]), \
             mock.patch.object(self.module.time, "sleep"), \
             mock.patch.object(self.module.os, "kill", side_effect=kill), \
             mock.patch.object(self.module.subprocess, "Popen") as start:
            self.module.replace_watch(worker, self.env)
        self.assertEqual(signals, [(12345, self.module.signal.SIGTERM), (12345, self.module.signal.SIGKILL)])
        start.assert_called_once()
        self.assertNotIn("ssh", start.call_args.args[0])

    def test_watch_handoff_refuses_worker_pid_without_sending_signal(self):
        worker = Path(self.env["PHONE_WORKER_DIR"])
        worker.mkdir()
        (worker / "phone-worker-watch.pid").write_text("12345\n")
        proc_path = Path("/proc/12345/cmdline")
        original_read = Path.read_bytes
        def read(path):
            return b"python\x00phone_worker.py\x00" if path == proc_path else original_read(path)
        with mock.patch.object(Path, "read_bytes", read), \
             mock.patch.object(self.module, "pid_alive", return_value=True), \
             mock.patch.object(self.module.os, "kill", side_effect=AssertionError("worker must not be signaled")), \
             self.assertRaises(RuntimeError):
            self.module.replace_watch(worker, self.env)

    def test_failed_connection_preserves_existing_environment(self):
        before = self.env_path.read_bytes()
        with mock.patch.object(self.module, "idle_worker"), \
             mock.patch.object(self.module, "ensure", return_value={"vps_ready": False}), \
             mock.patch.object(self.module.time, "monotonic", side_effect=[0, 0, 2]), \
             mock.patch.object(self.module.time, "sleep"), self.assertRaises(RuntimeError):
            self.module.activate(self.env, wait_seconds=1)
        self.assertEqual(self.env_path.read_bytes(), before)

    def test_idle_guard_rejects_tts_job_music_and_unverified_existing_worker(self):
        self.module.atomic_write(self.locations["state_root"] / "runtime-status.json", self.module.json_bytes({"pid": 900}))
        with mock.patch.object(self.module, "local_worker", return_value=(8766, None)), \
             mock.patch.object(self.module, "pid_alive", return_value=True), self.assertRaises(RuntimeError):
            self.module.idle_worker(self.config, self.env)
        for extra in ({"tts_agent": {"active": 1}}, {"core_worker_jobs": {"active": True}},
                      {"music_agent": {"guilds": {"1": {"status": "playing"}}}}):
            body = {"worker_id": "phone-test", **extra}
            with self.subTest(extra=extra), \
                 mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
                 mock.patch.object(self.module, "http_json", return_value=(body, 1)), self.assertRaises(RuntimeError):
                self.module.idle_worker(self.config, self.env)

    def test_reconnect_exits_only_selected_master_without_signaling_worker(self):
        with mock.patch.object(self.module, "master_alive", return_value=True), \
             mock.patch.object(self.module, "run", return_value=SimpleNamespace(returncode=0)) as command, \
             mock.patch.object(self.module.os, "kill", side_effect=AssertionError("must not signal worker")):
            result = self.module.reconnect(self.env, channel="bulk")
        self.assertTrue(result["ok"])
        command.assert_called_once()
        args = command.call_args.args[0]
        self.assertIn(str(self.module.control_path(self.config, self.locations, "bulk")), args)
        self.assertIn("exit", args)

    def test_diagnostic_is_sanitized_without_private_key_or_restart(self):
        self.module.atomic_write(self.locations["private"] / "interactive.log", "network failed Bearer PHONE-SENTINEL")
        with mock.patch.object(self.module, "local_worker", return_value=(8766, self.worker_body())), \
             mock.patch.object(self.module, "http_json", return_value=({"ok": True, "worker_id": "phone-test", "service": "osaka-phone-ssh"}, 88.2)), \
             mock.patch.object(self.module, "run", side_effect=AssertionError("diagnostic executed a command")):
            result = self.module.diagnostic(self.env)
        encoded = json.dumps(result)
        self.assertTrue(result["ok"])
        self.assertNotIn("PHONE-SENTINEL", encoded)
        self.assertNotIn("FAKE PRIVATE KEY", encoded)
        self.assertEqual(result["vps"]["rtt_ms"], 88.2)

    def test_http_probe_uses_no_proxy_and_cannot_forward_auth_to_redirect(self):
        response = SimpleNamespace(status=200)
        response.read = mock.Mock(return_value=b'{"ok":true}')
        manager = mock.MagicMock()
        manager.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = manager
        with mock.patch.object(self.module.urllib.request, "build_opener", return_value=opener) as factory:
            body, _ = self.module.http_json("http://127.0.0.1:8766/health", "PHONE-SENTINEL")
        self.assertTrue(body["ok"])
        handlers = factory.call_args.args
        self.assertTrue(any(isinstance(x, self.module.NoRedirect) for x in handlers))
        self.assertTrue(any(isinstance(x, self.module.urllib.request.ProxyHandler) and x.proxies == {} for x in handlers))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer PHONE-SENTINEL")
        self.assertIsNone(self.module.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_cli_status_does_not_need_ssh_binary(self):
        with mock.patch.object(self.module, "run", side_effect=AssertionError("SSH called")), \
             mock.patch("sys.stdout", io.StringIO()) as output:
            status = self.module.main(["status"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "ssh")


if __name__ == "__main__":
    unittest.main()
