"""OCI address persistence guards using only fixtures and mocked commands."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "deploy/oci/ensure_secondary_ip.py"
spec = importlib.util.spec_from_file_location("oci_secondary_ip_tests", SOURCE)
oci = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = oci
spec.loader.exec_module(oci)


def row(name, *addresses):
    return {"ifname": name, "addr_info": [{"local": ip, "prefixlen": prefix} for ip, prefix in addresses]}


class FakeIP:
    def __init__(self, rows, *, race=False, fail_add=False, changed_default=False):
        self.rows = rows
        self.race = race
        self.fail_add = fail_add
        self.changed_default = changed_default
        self.calls = []
        self.route_reads = 0

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1:] == ["-j", "-4", "address", "show"]:
            output = json.dumps(self.rows)
        elif argv[1:] == ["-4", "route", "show", "default"]:
            self.route_reads += 1
            output = "default via 10.0.0.1 dev ens3\ndefault via 10.0.0.1 dev ens3 proto dhcp src 10.0.0.174 metric 100\n"
            if self.changed_default and self.route_reads > 1:
                output += "default via 10.0.0.2 dev ens3 metric 200\n"
        elif argv[1:3] == ["address", "add"]:
            if not self.fail_add:
                self.rows[0]["addr_info"].append({"local": "10.0.0.237", "prefixlen": 32})
            return SimpleNamespace(returncode=2 if self.race or self.fail_add else 0, stdout="", stderr="")
        else:
            raise AssertionError("unexpected command: " + repr(argv))
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    @property
    def mutations(self):
        return [argv for argv in self.calls if "add" in argv]


class AddressGuards(unittest.TestCase):
    def test_existing_prefix_and_duplicate_defaults_are_preserved(self):
        runner = FakeIP([row("ens3", ("10.0.0.174", 24), ("10.0.0.237", 24))])
        result = oci.ensure_address(oci.Config(), ip="/mock/ip", runner=runner, wait=False)
        self.assertFalse(result["address_added_now"])
        self.assertTrue(result["secondary_already_present"])
        self.assertEqual(result["existing_prefixes"], [24])
        self.assertEqual(len(result["default_routes_before"]), 2)
        self.assertTrue(result["default_routes_unchanged"])
        self.assertEqual(runner.mutations, [])

    def test_missing_secondary_adds_only_host_alias(self):
        runner = FakeIP([row("ens3", ("10.0.0.174", 24))])
        result = oci.ensure_address(oci.Config(), ip="/mock/ip", runner=runner, wait=False)
        self.assertTrue(result["address_added_now"])
        self.assertEqual(runner.mutations, [["/mock/ip", "address", "add", "10.0.0.237/32", "dev", "ens3", "noprefixroute"]])

    def test_concurrent_native_add_is_not_claimed_as_ours(self):
        runner = FakeIP([row("ens3", ("10.0.0.174", 24))], race=True)
        result = oci.ensure_address(oci.Config(), ip="/mock/ip", runner=runner, wait=False)
        self.assertFalse(result["address_added_now"])

    def test_failed_add_requires_confirmed_address(self):
        runner = FakeIP([row("ens3", ("10.0.0.174", 24))], fail_add=True)
        with self.assertRaises(RuntimeError):
            oci.ensure_address(oci.Config(), ip="/mock/ip", runner=runner, wait=False)

    def test_other_interface_or_ambiguous_primary_prevents_mutation(self):
        for rows in ([row("ens4", ("10.0.0.174", 24))],
                     [row("ens3", ("10.0.0.174", 24)), row("ens4", ("10.0.0.237", 24))],
                     [row("ens3", ("10.0.0.174", 24)), row("ens4", ("10.0.0.174", 24))],
                     [row("ens3", ("10.0.0.237", 32))]):
            with self.subTest(rows=rows):
                runner = FakeIP(rows)
                with self.assertRaises(RuntimeError):
                    oci.ensure_address(oci.Config(), ip="/mock/ip", runner=runner, wait=False)
                self.assertEqual(runner.mutations, [])

    def test_external_route_change_is_reported_without_route_writes(self):
        runner = FakeIP([row("ens3", ("10.0.0.174", 24), ("10.0.0.237", 32))], changed_default=True)
        result = oci.ensure_address(oci.Config(), ip="/mock/ip", runner=runner, wait=False)
        self.assertFalse(result["default_routes_unchanged"])
        self.assertEqual(runner.mutations, [])

    def test_invalid_parameters_rejected_without_commands(self):
        for config in (oci.Config(secondary_ip="10.0.0.174"), oci.Config(primary_ip="147.15.16.46"),
                       oci.Config(interface="ens3;false"), oci.Config(wait_seconds=31)):
            with self.subTest(config=config), patch.object(oci, "run") as command:
                with self.assertRaises(ValueError):
                    oci.ensure_address(config, ip="/mock/ip", wait=False)
                command.assert_not_called()


class InstallationGuards(unittest.TestCase):
    def install_context(self, stack, *, events):
        stack.enter_context(patch.object(oci.os, "geteuid", return_value=0))
        stack.enter_context(patch.object(oci, "inspect", return_value={
            "unreadable_paths": [], "possible_native_references": []}))
        stack.enter_context(patch.object(oci, "tool", side_effect=lambda name: "/mock/" + name))
        stack.enter_context(patch.object(oci, "validate_owned_target", side_effect=lambda path, **kw: events.append(("validate", path))))
        stack.enter_context(patch.object(oci, "validate_unit_registration", side_effect=lambda value: events.append(("unit_guard", value))))
        command = stack.enter_context(patch.object(oci, "run", side_effect=lambda argv, **kw:
            events.append(("command", argv)) or SimpleNamespace(returncode=0, stdout="", stderr="")))
        write = stack.enter_context(patch.object(oci, "atomic_install", side_effect=lambda path, text, mode:
            events.append(("write", path, text, mode))))
        ensure = stack.enter_context(patch.object(oci, "ensure_address", return_value={
            "ok": True, "address_added_now": False, "secondary_already_present": True,
            "default_routes_unchanged": True}))
        return command, write, ensure

    def test_install_dry_run_validates_without_system_writes_or_enable(self):
        events = []
        with contextlib.ExitStack() as stack:
            command, write, ensure = self.install_context(stack, events=events)
            report = oci.install(oci.Config(), dry_run=True)
            self.assertTrue(report["dry_run"])
            self.assertFalse(report["persistence_configured"])
            write.assert_not_called()
            ensure.assert_not_called()
            commands = [call.args[0] for call in command.call_args_list]
            self.assertTrue(any("verify" in argv for argv in commands))
            self.assertFalse(any("enable" in argv or "daemon-reload" in argv for argv in commands))

    def test_install_twice_only_enables_own_unit_and_preserves_existing_address(self):
        events = []
        with contextlib.ExitStack() as stack:
            command, write, ensure = self.install_context(stack, events=events)
            for _ in range(2):
                report = oci.install(oci.Config())
                self.assertTrue(report["persistence_configured"])
                self.assertFalse(report["address_added_now"])
                self.assertFalse(report["reboot_restoration_tested"])
            self.assertEqual(ensure.call_count, 2)
            writes = write.call_args_list
            self.assertEqual(len(writes), 6)
            self.assertEqual([call.args[2] for call in writes], [0o755, 0o644, 0o644] * 2)
            stored = json.loads(writes[1].args[1])
            self.assertEqual(stored["managed_by"], oci.CONFIG_MARKER)
            self.assertEqual(stored["primary_ip"], "10.0.0.174")
            self.assertEqual(stored["secondary_ip"], "10.0.0.237")
            first_write = next(index for index, event in enumerate(events) if event[0] == "write")
            self.assertEqual(sum(event[0] == "validate" for event in events[:first_write]), 3)
            self.assertTrue(any(event[0] == "unit_guard" for event in events[:first_write]))
            self.assertTrue(any(event[0] == "command" and "verify" in event[1] for event in events[:first_write]))
            for call in command.call_args_list:
                argv = call.args[0]
                self.assertNotIn("restart", argv)
                if "enable" in argv:
                    self.assertEqual(argv, ["/mock/systemctl", "enable", "--now", oci.UNIT_NAME])

    def test_symlink_destination_cannot_replace_another_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(oci, "validate_parent_chain"):
            target = Path(folder) / "owned.py"
            target.write_text("# " + oci.MARKER + "\n")
            link = Path(folder) / "symlink.py"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                oci.validate_owned_target(link)
            self.assertEqual(target.read_text(), "# " + oci.MARKER + "\n")

    def test_dry_run_for_ensure_cannot_mutate(self):
        with patch.object(oci, "ensure_address") as ensure, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(oci.main(["ensure", "--dry-run"]), 1)
            ensure.assert_not_called()

    def test_native_references_do_not_expose_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "network.yaml"
            target.write_text("addresses: [10.0.0.237/32]\npassword: dummy-secret-never-print\n")
            (root / "comment.yaml").write_text("# addresses: [10.0.0.237/32]\n")
            (root / "link.yaml").symlink_to(target)
            result = oci.native_references("10.0.0.237", roots=[root])
            self.assertEqual(result["possible_native_references"], [str(target)])
            self.assertNotIn("dummy-secret", json.dumps(result))
            self.assertEqual(result["unreadable_paths"], [])

    def test_external_references_block_before_writes(self):
        report = {"unreadable_paths": [], "possible_native_references": ["/etc/netplan/example.yaml"]}
        with patch.object(oci.os, "geteuid", return_value=0), patch.object(oci, "inspect", return_value=report), \
             patch.object(oci, "atomic_install") as write, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                oci.install(oci.Config())
            write.assert_not_called()

    def test_unknown_destination_blocks_before_writes(self):
        report = {"unreadable_paths": [], "possible_native_references": []}
        with patch.object(oci.os, "geteuid", return_value=0), patch.object(oci, "inspect", return_value=report), \
             patch.object(oci, "validate_owned_target", side_effect=RuntimeError("unowned destination")), \
             patch.object(oci, "atomic_install") as write:
            with self.assertRaises(RuntimeError):
                oci.install(oci.Config())
            write.assert_not_called()

    def test_owned_target_requires_marker_and_secure_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(oci, "validate_parent_chain"):
            target = Path(folder) / "helper.py"
            target.write_text("# " + oci.MARKER + "\n")
            secure = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
            with patch.object(Path, "lstat", return_value=secure):
                oci.validate_owned_target(target)
                target.write_text("other installation\n")
                with self.assertRaises(RuntimeError):
                    oci.validate_owned_target(target)
            insecure = SimpleNamespace(st_mode=stat.S_IFREG | 0o666, st_uid=0)
            with patch.object(Path, "lstat", return_value=insecure):
                with self.assertRaises(RuntimeError):
                    oci.validate_owned_target(target)

    def test_foreign_unit_fragment_or_dropin_is_rejected(self):
        for stdout in ("FragmentPath=/usr/lib/systemd/system/other.service\n", "DropInPaths=/etc/systemd/override.conf\n"):
            with self.subTest(stdout=stdout), patch.object(oci, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout)):
                with self.assertRaises(RuntimeError):
                    oci.validate_unit_registration("/mock/systemctl")

    def test_unit_never_removes_address_or_isolates_network(self):
        unit = SOURCE.with_name(oci.UNIT_NAME).read_text()
        self.assertIn("After=network-online.target", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertNotIn("ExecStop", unit)
        self.assertNotIn("PrivateNetwork", unit)
        wrapper = SOURCE.with_name("install-secondary-ip.sh").read_text()
        self.assertIn("sudo -n", wrapper)
        self.assertNotIn("sudo -v", wrapper)


if __name__ == "__main__":
    unittest.main()
