"""Real process isolation for catalog writers and watcher kit upgrades."""
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location('cd_process_guard_cli', HERE / 'import-midi.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def child_module(source: Path) -> str:
    return ("import importlib.util, pathlib, sys\n"
            f"sys.path.insert(0, {str(source)!r})\n"
            f"spec = importlib.util.spec_from_file_location('guard_child', {str(source / 'import-midi.py')!r})\n"
            "cli = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(cli)\n")


class ProcessGuardTests(unittest.TestCase):
    def child_line(self, child):
        ready, _, _ = select.select([child.stdout], [], [], 10)
        self.assertTrue(ready, 'child did not reach lock/watch startup')
        line = child.stdout.readline()
        self.assertTrue(line, child.stderr.read() if child.poll() is not None else 'child returned no output')
        return line

    def test_real_process_lock_rejects_same_root_and_releases_without_unlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'state'
            source = child_module(HERE) + (
                "with cli.data_root_lock(pathlib.Path(sys.argv[1])):\n"
                "    print('locked', flush=True)\n"
                "    input()\n")
            child = subprocess.Popen([sys.executable, '-c', source, str(root)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(self.child_line(child).strip(), 'locked')
                lock_path = root / '.import-midi.lock'
                inode = lock_path.stat().st_ino
                with self.assertRaises(cli.ImporterGuardError) as denied:
                    with cli.data_root_lock(root):
                        self.fail('concurrent writer entered the catalog')
                self.assertEqual(denied.exception.code, 'importer_already_running')
                self.assertEqual(denied.exception.pid, child.pid)
                self.assertEqual(denied.exception.report()['importer_version'], cli.IMPORTER_VERSION)
                with cli.data_root_lock(Path(temporary) / 'independent-state'):
                    pass
                child.communicate('\n', timeout=10)
                self.assertEqual(child.returncode, 0)
                with cli.data_root_lock(root):
                    self.assertEqual(lock_path.stat().st_ino, inode)
                    self.assertEqual(json.loads(lock_path.read_text())['pid'], os.getpid())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)

    def test_alias_path_cannot_create_a_second_lock_for_same_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, alias = base / 'state', base / 'alias'
            root.mkdir()
            alias.symlink_to(root, target_is_directory=True)
            with cli.data_root_lock(root):
                with self.assertRaises(cli.ImporterGuardError):
                    with cli.data_root_lock(alias):
                        self.fail('directory alias bypassed lock')

    def test_abandoned_pid_file_does_not_block_after_process_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.import-midi.lock').write_text('{"pid":99999999,"importer_version":"old"}')
            with cli.data_root_lock(root):
                self.assertEqual(json.loads((root / '.import-midi.lock').read_text())['pid'], os.getpid())

    def test_hash_snapshot_detects_rewrite_even_if_mtime_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'module.py'
            path.write_text('version = 1\n')
            old_stat = path.stat()
            loaded = cli._source_snapshot(root)
            cli._check_sources(root, loaded)
            path.write_text('version = 2\n')
            os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            with self.assertRaises(cli.ImporterGuardError) as changed:
                cli._check_sources(root, loaded)
            self.assertEqual(changed.exception.code, 'importer_code_changed')
            self.assertEqual(changed.exception.changed_files, ['module.py'])

    def test_snapshot_checks_nested_additions_and_deletions_but_ignores_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'module.py'
            original.write_text('version = 1\n')
            loaded = cli._source_snapshot(root)
            (root / 'imported.json').write_text('{"song":"complete"}')
            (root / '__pycache__').mkdir()
            (root / '__pycache__/ignored.py').write_text('ignored = 1')
            cli._check_sources(root, loaded)
            nested = root / 'provider'
            nested.mkdir()
            (nested / 'search.py').write_text('version = 2')
            original.unlink()
            with self.assertRaises(cli.ImporterGuardError) as changed:
                cli._check_sources(root, loaded)
            self.assertEqual(changed.exception.changed_files, ['module.py', 'provider/search.py'])

    def test_real_watch_rejects_force_then_exits_when_installed_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            kit = base / 'kit'
            shutil.copytree(HERE, kit, ignore=shutil.ignore_patterns('__pycache__'))
            state = base / 'state'
            argv = [str(kit / 'import-midi.py'), '--watch', '--inbox', str(base / 'inbox'),
                    '--output', str(base / 'output'), '--data-root', str(state),
                    '--report', str(base / 'output/report.json'), '--no-sample']
            source = child_module(kit) + (
                "original_sleep = cli.time.sleep\n"
                "cli.time.sleep = lambda _: original_sleep(0.05)\n"
                f"sys.argv = {argv!r}\n"
                "raise SystemExit(cli.main())\n")
            child = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(self.child_line(child).strip(), '{')
                before = (state / 'imported.json').read_bytes()
                force_argv = [word for word in argv if word != '--watch'] + ['--force']
                force = subprocess.run([sys.executable, *force_argv], capture_output=True,
                                       text=True, timeout=10)
                self.assertEqual(force.returncode, 2, force.stderr)
                denied = json.loads(force.stdout)
                self.assertEqual(denied['error'], 'importer_already_running')
                self.assertEqual(denied['pid'], child.pid)
                self.assertEqual((state / 'imported.json').read_bytes(), before)
                changed_path = kit / 'worker_config.py'
                changed_path.write_bytes(changed_path.read_bytes() + b'\n# installed source revision changed\n')
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 2, stderr)
                self.assertIn('"error": "importer_code_changed"', stdout)
                self.assertIn('worker_config.py', stdout)
                with cli.data_root_lock(state):
                    pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
