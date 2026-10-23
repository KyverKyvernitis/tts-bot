"""Restricted SSH provisioning, private forwards and transactional environment edits."""
import base64
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy/oci/ssh_phone_vps.py"
spec = importlib.util.spec_from_file_location("osaka_oci_phone_ssh_test", SOURCE)
ssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssh)


def key(seed=b"a"):
    raw = struct.pack("!I", 11) + b"ssh-ed25519" + struct.pack("!I", 32) + seed * 32
    return "ssh-ed25519 " + base64.b64encode(raw).decode()


def bundle(worker_id="phone-one", **overrides):
    return ssh.validated_bundle(worker_id, "163.176.47.24", host_key=key(), **overrides)


def registry(*rows):
    return {"marker": ssh.MARKER, "schema": 1,
            "workers": {row["worker_id"]: {**row, "bootstrap_origin": "http://127.0.0.1:" + str(row["local_port"]),
                                          "state": "enrolled", "client_public_key": key(b"b")} for row in rows}}


def args(**values):
    defaults = dict(worker_id="phone-one", host="163.176.47.24", ssh_port=22,
                    remote_port=18766, bulk_remote_port=18767, local_port=19000,
                    vps_api_port=10000, dry_run=False, public_key=key(b"b"),
                    public_key_file=None, env_file="unused")
    return SimpleNamespace(**(defaults | values))


class KeyAndPolicyTests(unittest.TestCase):
    def test_key_accepts_comment_but_rejects_options_multiple_keys_and_bad_blobs(self):
        self.assertEqual(ssh.public_key(key() + " phone\n"), key())
        for value in ("command=sh " + key(), key() + "\n" + key(b"b"), "ssh-rsa AAAA",
                      "ssh-ed25519 !!!", "ssh-ed25519 " + base64.b64encode(b"short").decode()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ssh.public_key(value)

    def test_bundle_is_public_and_rejects_shell_options_or_ambiguous_ports(self):
        value = bundle()
        self.assertEqual(value["ssh_user"], "osaka-phone-ssh")
        self.assertNotIn("token", json.dumps(value))
        self.assertEqual(value["ssh_host_key"], key())
        for host in ("-oProxyCommand=sh", "https://example.com", "host name", "a;false"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                ssh.validated_bundle("phone-one", host, host_key=key())
        for values in ({"remote_port": 10000}, {"local_port": 80}, {"ssh_port": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                bundle(**values)

    def test_each_public_key_can_forward_only_its_registered_ports(self):
        one = bundle()
        two = bundle("phone-two", remote_port=18770, bulk_remote_port=18771, local_port=19001)
        text = ssh.authorized_keys(registry(one, two)).decode()
        first = next(line for line in text.splitlines() if line.endswith("osaka:phone-one"))
        self.assertIn('permitopen="127.0.0.1:10000"', first)
        self.assertIn('permitlisten="127.0.0.1:18766"', first)
        self.assertIn('permitlisten="127.0.0.1:18767"', first)
        self.assertNotIn("18770", first)
        self.assertIn('restrict,port-forwarding,command="/usr/sbin/nologin"', first)
        config = ssh.ssh_config(registry(one)).decode()
        self.assertIn("GatewayPorts no", config)
        self.assertIn("MaxSessions 0", config)
        self.assertIn("ClientAliveInterval 15", config)
        self.assertIn("ClientAliveCountMax 2", config)
        self.assertTrue(config.startswith("# " + ssh.MARKER + "\nMatch User osaka-phone-ssh\n"))
        self.assertNotIn("0.0.0.0", config)

    def test_effective_server_policy_must_enforce_private_ports_and_key_only_auth(self):
        row = registry(bundle())
        effective = {
            "passwordauthentication": "no", "kbdinteractiveauthentication": "no",
            "authenticationmethods": "publickey", "pubkeyauthentication": "yes",
            "permittty": "no", "gatewayports": "no", "allowagentforwarding": "no",
            "x11forwarding": "no", "forcecommand": "/usr/sbin/nologin", "allowtcpforwarding": "yes",
            "allowstreamlocalforwarding": "no", "permittunnel": "no", "maxsessions": "0",
            "clientaliveinterval": "15", "clientalivecountmax": "2",
            "authorizedkeysfile": str(ssh.HOME_PATH / ".ssh/authorized_keys"),
            "permitlisten": "127.0.0.1:18766 127.0.0.1:18767", "permitopen": "127.0.0.1:10000"}
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(stdout="\n".join(k + " " + v for k, v in effective.items()))
        ssh.verify_effective_ssh(row, runner=runner)
        self.assertTrue(any("-t" in call for call in calls))
        for field, bad in (("gatewayports", "yes"), ("permitopen", "any"), ("permitlisten", "any"),
                           ("authenticationmethods", "any"), ("maxsessions", "10"),
                           ("clientaliveinterval", "0"), ("clientalivecountmax", "3")):
            old = effective[field]
            effective[field] = bad
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                ssh.verify_effective_ssh(row, runner=runner)
            effective[field] = old


class NativeServerPolicyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sshd") and shutil.which("ssh-keygen"), "native OpenSSH server and keygen required")
    def test_generated_match_policy_passes_native_sshd_syntax_and_effective_checks(self):
        # Client ssh -G does not validate server Match directives. Exercise the
        # real server parser and resulting restrictions without opening a socket
        # or touching /etc/ssh, even when the dedicated account is not installed.
        with tempfile.TemporaryDirectory(prefix="osaka-native-sshd-") as temporary:
            root = Path(temporary)
            host_key = root / "host_key"
            generated = subprocess.run([shutil.which("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            config = root / "sshd_config"
            rows = registry(bundle())
            config.write_text(f"HostKey {host_key}\nPidFile {root / 'pid'}\nUsePAM no\n" + ssh.ssh_config(rows).decode())
            calls = []
            def native(argv, **kwargs):
                command = [shutil.which("sshd"), "-f", str(config), *argv[1:]]
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls.append(argv)
                return result
            ssh.verify_effective_ssh(rows, runner=native)
            self.assertTrue(any("-t" in command for command in calls))
            self.assertTrue(any("-T" in command for command in calls))
            # The forwarding account's dead-peer deadline must not become a
            # global SSH policy or close an administrator's idle session.
            admin = native([shutil.which("sshd"), "-T", "-C",
                            "user=ubuntu,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22"])
            effective = dict(line.split(" ", 1) for line in admin.stdout.splitlines())
            self.assertEqual(effective["clientaliveinterval"], "0")
            self.assertEqual(effective["clientalivecountmax"], "3")
            self.assertNotEqual(effective["forcecommand"], "/usr/sbin/nologin")


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="phone-ssh-test-", dir=ROOT.parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / ".env"
        self.path.write_text("PHONE_WORKER_TOKEN=keep-secret\nTTS_EDGE_SOURCE_IPS=10.0.0.174,10.0.0.237\nOTHER=value\n")
        self.path.chmod(0o600)
        # Managed executors can map the read-only filesystem root to nobody.
        # Normalize only that mount-owner observation, without relaxing the
        # installer's production rule requiring root-owned system ancestors.
        root_uid = Path("/").lstat().st_uid
        if root_uid != 0:
            original_lstat, original_fstat = Path.lstat, os.fstat
            def mapped(info):
                if info.st_uid != root_uid:
                    return info
                values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                values["st_uid"] = 0
                return SimpleNamespace(**values)
            one = patch.object(Path, "lstat", lambda path: mapped(original_lstat(path)))
            two = patch.object(ssh.os, "fstat", lambda descriptor: mapped(original_fstat(descriptor)))
            one.start(); two.start()
            self.addCleanup(one.stop); self.addCleanup(two.stop)

    def test_existing_user_owned_environment_is_read_and_replaced_without_ownership_change(self):
        path, info = ssh.environment_path(self.path)
        raw = ssh.environment_bytes(path, info)
        result = ssh.updated_env(raw, {"PHONE_WORKER_HOST": "127.0.0.1"})
        ssh.atomic_write(path, result, uid=info.st_uid, gid=info.st_gid, mode=0o600, expected=info)
        self.assertEqual(path.stat().st_uid, info.st_uid)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("PHONE_WORKER_TOKEN=keep-secret\n", path.read_text())
        self.assertIn("TTS_EDGE_SOURCE_IPS=10.0.0.174,10.0.0.237\n", path.read_text())
        self.assertEqual(list(self.root.glob(".osaka-*")), [])

    def test_symlink_hardlink_and_writable_environment_are_refused(self):
        link = self.root / "link.env"
        link.symlink_to(self.path)
        with self.assertRaises(RuntimeError):
            ssh.environment_path(link)
        hardlink = self.root / "hard.env"
        os.link(self.path, hardlink)
        with self.assertRaises(RuntimeError):
            ssh.environment_path(self.path)
        hardlink.unlink()
        self.path.chmod(0o666)
        with self.assertRaises(RuntimeError):
            ssh.environment_path(self.path)

    def test_world_writable_parent_is_refused_even_when_file_is_private(self):
        parent = self.root / "unsafe"
        parent.mkdir()
        target = parent / ".env"
        target.write_text("TOKEN=private\n")
        target.chmod(0o600)
        parent.chmod(0o777)
        with self.assertRaises(RuntimeError):
            ssh.environment_path(target)
        parent.chmod(0o700)

    def test_concurrent_environment_edit_is_not_overwritten(self):
        path, info = ssh.environment_path(self.path)
        path.write_text("OTHER=edited-during-health\n")
        with self.assertRaises(RuntimeError):
            ssh.atomic_write(path, b"replacement", uid=info.st_uid, gid=info.st_gid, expected=info)
        self.assertEqual(path.read_text(), "OTHER=edited-during-health\n")

    def test_renamed_parent_cannot_redirect_atomic_replacement(self):
        original = self.root / "original"
        original.mkdir()
        target = original / ".env"
        target.write_text("owned-content")
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / ".env").write_text("foreign-content")
        info = target.stat()
        moved = self.root / "moved"
        replace = os.replace
        def swap(source, destination, **kwargs):
            original.rename(moved)
            original.symlink_to(foreign, target_is_directory=True)
            return replace(source, destination, **kwargs)
        with patch.object(ssh.os, "replace", side_effect=swap):
            ssh.atomic_write(target, b"new-owned-content", uid=info.st_uid, gid=info.st_gid, expected=info)
        self.assertEqual((foreign / ".env").read_text(), "foreign-content")
        self.assertEqual((moved / ".env").read_text(), "new-owned-content")

    def test_edit_removes_duplicate_transport_lines_but_preserves_user_settings(self):
        raw = self.path.read_bytes() + b"export PHONE_WORKER_HOST=old\nPHONE_WORKER_HOST=older\n"
        new = ssh.updated_env(raw, {"PHONE_WORKER_HOST": "127.0.0.1"}).decode()
        self.assertEqual(new.count("PHONE_WORKER_HOST="), 1)
        self.assertIn("OTHER=value\n", new)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="phone-ssh-transactions-", dir=ROOT.parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("CONFIG_PATH", self.root / "registry.json"),
                            ("DROPIN_PATH", self.root / "ssh.conf"),
                            ("HOME_PATH", self.root / "account"),
                            ("BACKUP_PATH", self.root / "backups"),
                            ("HOST_PUBLIC_KEY_PATH", self.root / "server.pub")):
            self.stack.enter_context(patch.object(ssh, name, value))
        ssh.HOST_PUBLIC_KEY_PATH.write_text(key())
        self.stack.enter_context(patch.object(ssh, "guarded_path"))
        def write(path, raw, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.write = self.stack.enter_context(patch.object(ssh, "atomic_write", side_effect=write))

    def test_rejected_server_config_restores_only_installer_files_and_never_reloads(self):
        rows = registry(bundle())
        before = {path: raw for path, raw in ssh.registry_changes(rows).items()}
        for path, raw in before.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        rows["workers"]["phone-one"]["client_public_key"] = key(b"c")
        runner = Mock()
        with patch.object(ssh, "verify_effective_ssh", side_effect=RuntimeError("unsafe config")):
            with self.assertRaises(RuntimeError):
                ssh.commit_registry(rows, runner=runner)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        runner.assert_not_called()

    def test_identical_configuration_does_not_rewrite_or_reload(self):
        rows = registry(bundle())
        for path, raw in ssh.registry_changes(rows).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        runner = Mock()
        with patch.object(ssh, "verify_effective_ssh") as verify:
            self.assertFalse(ssh.commit_registry(rows, runner=runner))
        verify.assert_called_once()
        self.write.assert_not_called()
        runner.assert_not_called()

    def test_refresh_updates_only_policy_and_preserves_all_registered_devices(self):
        rows = registry(bundle(), bundle("phone-two", remote_port=18770, bulk_remote_port=18771, local_port=19001))
        rows["workers"]["phone-one"]["state"] = "active"
        for path, raw in ssh.registry_changes(rows).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        old = ssh.DROPIN_PATH.read_bytes().replace(b"    ClientAliveInterval 15\n    ClientAliveCountMax 2\n", b"")
        ssh.DROPIN_PATH.write_bytes(old)
        registry_before = ssh.CONFIG_PATH.read_bytes()
        keys_before = (ssh.HOME_PATH / ".ssh/authorized_keys").read_bytes()
        runner = Mock()
        with patch.object(ssh, "verify_effective_ssh") as verify, patch.object(ssh, "ensure_account") as account:
            result = ssh.refresh(args(), runner=runner)
            again = ssh.refresh(args(), runner=runner)
        self.assertTrue(result["changed"])
        self.assertFalse(again["changed"])
        self.assertEqual(ssh.CONFIG_PATH.read_bytes(), registry_before)
        self.assertEqual((ssh.HOME_PATH / ".ssh/authorized_keys").read_bytes(), keys_before)
        self.assertEqual(ssh.read_registry(), rows)
        self.assertEqual(self.write.call_count, 1)
        self.assertEqual(self.write.call_args.args[0], ssh.DROPIN_PATH)
        self.assertEqual(verify.call_count, 2)
        runner.assert_called_once_with(["systemctl", "reload", "ssh.service"])
        account.assert_not_called()

    def test_refresh_missing_registry_is_a_noop_and_unknown_worker_is_refused(self):
        runner = Mock()
        result = ssh.refresh(args(), runner=runner)
        self.assertEqual(result["state"], "not_configured")
        self.assertFalse(result["changed"])
        self.assertFalse(ssh.CONFIG_PATH.exists())
        ssh.CONFIG_PATH.write_bytes(ssh.dump_json(registry(bundle())))
        with self.assertRaises(ValueError):
            ssh.refresh(args(worker_id="unknown"), runner=runner)
        runner.assert_not_called()
        self.write.assert_not_called()

    def test_refresh_dry_run_never_writes_or_reloads_and_rejects_foreign_policy(self):
        ssh.CONFIG_PATH.write_bytes(ssh.dump_json(registry(bundle())))
        runner = Mock()
        result = ssh.refresh(args(dry_run=True), runner=runner)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["changed"])
        ssh.DROPIN_PATH.write_text("# Owned by the administrator\n")
        with self.assertRaises(RuntimeError):
            ssh.refresh(args(dry_run=True), runner=runner)
        runner.assert_not_called()
        self.write.assert_not_called()

    def test_refresh_rejected_policy_restores_previous_bytes_without_reload(self):
        rows = registry(bundle())
        for path, raw in ssh.registry_changes(rows).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        previous = ssh.DROPIN_PATH.read_bytes().replace(b"    ClientAliveInterval 15\n    ClientAliveCountMax 2\n", b"")
        ssh.DROPIN_PATH.write_bytes(previous)
        runner = Mock()
        with patch.object(ssh, "verify_effective_ssh", side_effect=RuntimeError("policy not applied")):
            with self.assertRaises(RuntimeError):
                ssh.refresh(args(), runner=runner)
        self.assertEqual(ssh.DROPIN_PATH.read_bytes(), previous)
        self.assertEqual(ssh.read_registry(), rows)
        runner.assert_not_called()

    def test_refresh_reload_failure_restores_previous_managed_configuration(self):
        rows = registry(bundle())
        for path, raw in ssh.registry_changes(rows).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        previous = ssh.DROPIN_PATH.read_bytes().replace(b"    ClientAliveInterval 15\n    ClientAliveCountMax 2\n", b"")
        ssh.DROPIN_PATH.write_bytes(previous)
        runner = Mock(side_effect=RuntimeError("reload failed"))
        with patch.object(ssh, "verify_effective_ssh"):
            with self.assertRaises(RuntimeError):
                ssh.refresh(args(), runner=runner)
        self.assertEqual(ssh.DROPIN_PATH.read_bytes(), previous)
        self.assertEqual(ssh.read_registry(), rows)
        runner.assert_called_once_with(["systemctl", "reload", "ssh.service"])

    def test_foreign_dropin_is_refused_before_account_creation(self):
        ssh.DROPIN_PATH.write_text("# Administrator-owned configuration\n")
        with patch.object(ssh, "ensure_account") as account, patch.object(ssh, "available_listeners"):
            with self.assertRaises(RuntimeError):
                ssh.prepare(args())
        account.assert_not_called()

    def test_prepare_dry_run_has_no_account_registry_or_service_writes(self):
        with patch.object(ssh, "ensure_account") as account, patch.object(ssh, "commit_registry") as commit, \
                patch.object(ssh, "available_listeners"):
            result = ssh.prepare(args(dry_run=True))
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        account.assert_not_called()
        commit.assert_not_called()
        self.write.assert_not_called()

    def test_enrollment_preserves_other_devices_and_has_no_private_key(self):
        rows = registry(bundle(), bundle("phone-two", remote_port=18770, bulk_remote_port=18771, local_port=19001))
        ssh.CONFIG_PATH.write_bytes(ssh.dump_json(rows))
        with patch.object(ssh, "commit_registry") as commit:
            result = ssh.enroll(args(public_key=key(b"c")))
        after = commit.call_args.args[0]
        self.assertEqual(after["workers"]["phone-two"], rows["workers"]["phone-two"])
        self.assertTrue(result["key_rotated"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_activation_preserves_tokens_multi_ip_and_asks_for_controlled_restart(self):
        rows = registry(bundle())
        ssh.CONFIG_PATH.write_bytes(ssh.dump_json(rows))
        env = self.root / ".env"
        env.write_text("PHONE_WORKER_TOKEN=existing-secret\nTTS_EDGE_SOURCE_IPS=10.0.0.174,10.0.0.237\nOTHER=unchanged\n")
        env.chmod(0o600)
        with patch.object(ssh, "health_check", return_value={"both_channels": True}):
            result = ssh.activate(args(env_file=str(env)))
        current = ssh.parse_env(env.read_bytes())
        self.assertEqual(current["PHONE_WORKER_TOKEN"], "existing-secret")
        self.assertEqual(current["TTS_EDGE_SOURCE_IPS"], "10.0.0.174,10.0.0.237")
        self.assertEqual(current["CORE_WORKER_SSH_LOCAL_PORT"], "19000")
        self.assertEqual(current["PHONE_WORKER_TRANSPORT"], "ssh")
        self.assertTrue(result["bot_restart_required"])
        self.assertNotIn("existing-secret", json.dumps(result))
        self.assertEqual(ssh.read_registry()["workers"]["phone-one"]["state"], "active")

    def test_failed_health_never_changes_environment_or_marks_active(self):
        ssh.CONFIG_PATH.write_bytes(ssh.dump_json(registry(bundle())))
        env = self.root / ".env"
        env.write_text("PHONE_WORKER_TOKEN=secret\nOTHER=unchanged\n")
        before = env.read_bytes()
        with patch.object(ssh, "health_check", side_effect=RuntimeError("wrong device")):
            with self.assertRaises(RuntimeError):
                ssh.activate(args(env_file=str(env)))
        self.assertEqual(env.read_bytes(), before)
        self.write.assert_not_called()

    def test_rollback_restores_only_ssh_routing_and_preserves_later_config_changes(self):
        backup = ssh.BACKUP_PATH / "phone-one.ssh-base.env"
        backup.parent.mkdir()
        values = {"PHONE_WORKER_TRANSPORT": "ssh", "PHONE_WORKER_HOST": "127.0.0.1",
                  "PHONE_WORKER_PORT": "18766", "PHONE_WORKER_BULK_PORT": "18767",
                  "PHONE_WORKER_ID": "phone-one", "CORE_WORKER_SSH_LOCAL_PORT": "19000",
                  "PHONE_WORKER_SCHEME": "http", "PHONE_WORKER_ENABLED": "true"}
        backup.write_bytes(ssh.updated_env(b"PHONE_WORKER_TOKEN=old-secret\nOTHER=old\n", values))
        env = self.root / ".env"
        env.write_text("PHONE_WORKER_TOKEN=new-secret\nOTHER=new\nPHONE_WORKER_PORT=9999\nTTS_EDGE_SOURCE_IPS=10.0.0.237\n")
        ssh.rollback(args(env_file=str(env)))
        after = ssh.parse_env(env.read_bytes())
        self.assertEqual(after["PHONE_WORKER_PORT"], "18766")
        self.assertEqual(after["PHONE_WORKER_TOKEN"], "new-secret")
        self.assertEqual(after["OTHER"], "new")
        self.assertEqual(after["TTS_EDGE_SOURCE_IPS"], "10.0.0.237")


class HealthTests(unittest.TestCase):
    def payload(self, worker_id="phone-one", pid=321):
        return {"ok": True, "worker_id": worker_id, "pid": pid, "runtime_kind": "termux",
                "tts_agent": {"ok": True, "available_engines": ["teto", "edge"], "stream_protocol": 2}}

    def opener(self, *values):
        responses = []
        for value in values:
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.status = 200
            response.read.return_value = json.dumps(value).encode()
            responses.append(response)
        return SimpleNamespace(open=Mock(side_effect=responses))

    def test_activation_checks_both_authenticated_channels_without_synthesis(self):
        opener = self.opener(self.payload(), self.payload())
        result = ssh.health_check(bundle(), "existing-secret", opener=opener)
        self.assertTrue(result["both_channels"])
        calls = opener.open.call_args_list
        self.assertEqual([call.args[0].full_url for call in calls], [
            "http://127.0.0.1:18766/tts-agent/health", "http://127.0.0.1:18767/tts-agent/health"])
        self.assertTrue(all(call.args[0].get_header("Authorization") == "Bearer existing-secret" for call in calls))

    def test_wrong_device_wrong_process_and_missing_stream_protocol_are_refused(self):
        for first, second in ((self.payload("wrong"), self.payload()),
                              (self.payload(), self.payload(pid=654)),
                              ({**self.payload(), "tts_agent": {"ok": True, "available_engines": ["edge"], "stream_protocol": 1}}, self.payload())):
            with self.subTest(first=first, second=second), self.assertRaises(RuntimeError):
                ssh.health_check(bundle(), "secret", opener=self.opener(first, second))


if __name__ == "__main__":
    unittest.main()
