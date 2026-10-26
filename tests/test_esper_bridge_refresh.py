"""Digest-only migration of installed ESPER commands; no engine execution."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'deploy/esper-utau/termux'
OLD = '49fac2cae6396f3ee8dda7bc6e2ccbe2ae3835d48b9e25b6eeedffb5b955b43b'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.kit = self.base / 'kit'
        self.kit.mkdir()
        self.root = self.base / 'installed runtime'
        (self.root / 'bin').mkdir(parents=True)
        self.source = b'#!/usr/bin/env python3\n# upgraded pinned bridge\n'
        (self.kit / 'resampler.py').write_bytes(self.source)
        for name in ('resampler.py', 'esper-utau-resampler'):
            (self.root / 'bin' / name).write_bytes(self.source)
        self.digest = hashlib.sha256(self.source).hexdigest()
        self.env = self.base / '.phone-worker.env'
        self.helper = load('esper_refresh_activation', 'activate-esper.py')
        self.module = load('esper_refresh_tests', 'refresh-bridge-config.py')
        self.module.TOOLKIT_ROOT = self.kit
        self.module._activation_helper = lambda: self.helper

    def command(self, identity=OLD, *, equal=False, direct=False):
        wrapper = self.root / 'bin' / ('esper-utau-resampler' if direct else 'resampler.py')
        command = [] if direct else ['/usr/bin/python3']
        command += [str(wrapper), '--root', str(self.root), '--container', 'voicepeak-arm64',
                    '--cache-dir', '/analysis-cache/' + OLD, '--timeout', '60']
        command += ['--implementation-id=' + identity] if equal else ['--implementation-id', identity]
        return shlex.join(command)

    def line(self, character, command, *, ending='\r\n'):
        return ('export PHONE_WORKER_' + character + '_RESAMPLER_COMMAND = '
                + self.helper.shell_value(command) + ending).encode()

    def run_refresh(self, *, check=False):
        return self.module.refresh(env=self.env, esper_root=self.root, check=check)

    def test_both_voices_change_digest_only_and_backup_entire_original(self):
        unrelated = (b"# settings\r\nTOKEN='literal $secret `never execute`'\r\n"
                     b'PHONE_WORKER_TETO_FLAGS=B-25\r\nPHONE_WORKER_MIKU_RENDER_THREADS=4\r\n'
                     b'PHONE_WORKER_ESPER_DEADLINE=keep-this-too\r\n')
        first = self.line('TETO', self.command())
        second = self.line('MIKU', self.command(equal=True, direct=True), ending='')
        original = unrelated + first + second
        self.env.write_bytes(original)
        result = self.run_refresh()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['commands_updated'], 2)
        self.assertTrue(result['backup_created'] and result['restart_required'])
        expected = unrelated + first.replace(('--implementation-id ' + OLD).encode(),
                                              ('--implementation-id ' + self.digest).encode())
        expected += second.replace(('--implementation-id=' + OLD).encode(),
                                   ('--implementation-id=' + self.digest).encode())
        self.assertEqual(self.env.read_bytes(), expected)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        backups = list(self.base.glob('.phone-worker.env.bak-esper-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertTrue(all(type(value) in (bool, int) for value in result.values()))
        self.assertNotIn('secret', json.dumps(result))

    def test_idempotent_current_identity_preserves_inode_and_creates_no_backup(self):
        self.env.write_bytes(self.line('TETO', self.command(self.digest)))
        original, inode = self.env.read_bytes(), self.env.stat().st_ino
        for _ in range(2):
            result = self.run_refresh()
            self.assertTrue(result['ok'])
            self.assertFalse(result['worker_configuration_changed'])
            self.assertEqual(result['commands_current'], 1)
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.env.stat().st_ino, inode)
        self.assertEqual(list(self.base.glob('*.bak-esper-*')), [])

    def test_check_reports_pending_without_writing_or_backing_up(self):
        original = self.line('TETO', self.command())
        self.env.write_bytes(original)
        result = self.run_refresh(check=True)
        self.assertTrue(result['ok'] and result['check_only'])
        self.assertEqual(result['commands_pending'], 1)
        self.assertEqual(result['commands_updated'], 0)
        self.assertFalse(result['worker_configuration_changed'])
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(list(self.base.glob('*.bak-esper-*')), [])

    def test_custom_command_is_preserved_without_execution(self):
        marker = self.base / 'should-not-exist'
        custom = "sh -c 'touch " + str(marker) + "'"
        original = self.line('TETO', custom) + self.line('MIKU', self.command())
        self.env.write_bytes(original)
        result = self.run_refresh()
        self.assertTrue(result['ok'])
        self.assertEqual(result['commands_skipped'], 1)
        self.assertFalse(marker.exists())
        self.assertTrue(self.env.read_bytes().startswith(self.line('TETO', custom)))

    def test_malformed_or_unknown_identity_aborts_every_assignment(self):
        for command in (self.command('f' * 64), self.command() + ' --implementation-id ' + OLD,
                        self.command().replace('--implementation-id ' + OLD, '--implementation-id'),
                        self.command().replace('--timeout 60', '--timeout --container example')):
            with self.subTest(command=command):
                original = self.line('TETO', self.command()) + self.line('MIKU', command)
                self.env.write_bytes(original)
                result = self.run_refresh()
                self.assertFalse(result['ok'])
                self.assertTrue(result['validation_failed'])
                self.assertEqual(self.env.read_bytes(), original)
                self.assertEqual(list(self.base.glob('*.bak-esper-*')), [])

    def test_installed_or_convenience_wrapper_edits_leave_env_untouched(self):
        for name in ('resampler.py', 'esper-utau-resampler'):
            with self.subTest(name=name):
                wrapper = self.root / 'bin' / name
                wrapper.write_bytes(b'# local edit\n')
                original = self.line('MIKU', self.command(direct=True))
                self.env.write_bytes(original)
                result = self.run_refresh()
                self.assertFalse(result['ok'])
                self.assertEqual(self.env.read_bytes(), original)
                wrapper.write_bytes(self.source)

    def test_concurrent_env_change_is_preserved(self):
        self.env.write_bytes(self.line('TETO', self.command()))
        publish = self.helper.publish_environment
        replacement = b'TOKEN=concurrent-local-edit\n'

        def race(path, original, snapshot, updated):
            path.write_bytes(replacement)
            return publish(path, original, snapshot, updated)

        with patch.object(self.helper, 'publish_environment', side_effect=race):
            result = self.run_refresh()
        self.assertFalse(result['ok'])
        self.assertEqual(self.env.read_bytes(), replacement)
        self.assertEqual(list(self.base.glob('*.bak-esper-*')), [])

    def test_symlinked_env_is_never_replaced(self):
        actual = self.base / 'actual-env'
        original = self.line('TETO', self.command())
        actual.write_bytes(original)
        self.env.symlink_to(actual)
        result = self.run_refresh()
        self.assertFalse(result['ok'])
        self.assertTrue(self.env.is_symlink())
        self.assertEqual(actual.read_bytes(), original)

    def test_duplicate_env_assignments_refresh_together(self):
        self.env.write_bytes(self.line('TETO', self.command()) * 2)
        result = self.run_refresh()
        self.assertTrue(result['ok'])
        self.assertEqual(result['commands_updated'], 2)
        self.assertEqual(self.env.read_bytes().count(self.digest.encode()), 2)


if __name__ == '__main__':
    unittest.main()
