"""Resident host integrity and optional native CLI equivalence checks."""
from __future__ import annotations

import array
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/esper-utau/termux/build-resident.py'


def builder():
    spec = importlib.util.spec_from_file_location('test_resident_builder', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResidentManifestTests(unittest.TestCase):
    def setUp(self):
        self.module = builder()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'resident'
        self.directory.mkdir()
        self.config = b'verified fixture config\n'
        (self.directory / 'esper-config.ini').write_bytes(self.config)
        (self.directory / 'Esper.Resident').write_bytes(b'fixture executable')
        self.manifest = {'protocol': 1, 'source_commit': self.module.SOURCE_COMMIT,
            'engine_sha256': self.module.ENGINE_SHA256,
            'config_sha256': hashlib.sha256(self.config).hexdigest(),
            'files': {name: hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
                      for name in ('esper-config.ini', 'Esper.Resident')}}
        self.config_patch = patch.object(self.module, 'CONFIG_SHA256', self.manifest['config_sha256'])
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.publish()

    def publish(self):
        (self.directory / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_valid_manifest_is_idempotent_without_building_or_mutating_runtime(self):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with patch.object(self.module.subprocess, 'run', side_effect=AssertionError('unneeded build')):
            result = self.module.build(root=self.directory.parent, dotnet='missing', output=self.directory)
        self.assertTrue(result['ok'])
        self.assertEqual(result['action'], 'already_installed')
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_tampered_runtime_is_refused(self):
        (self.directory / 'Esper.Resident').write_bytes(b'changed executable')
        with self.assertRaisesRegex(ValueError, 'digest_mismatch'):
            self.module.inspect(self.directory)

    def test_manifest_cannot_reach_files_outside_runtime(self):
        self.manifest['files']['../external'] = 'a' * 64
        self.publish()
        with self.assertRaisesRegex(ValueError, 'manifest_path'):
            self.module.inspect(self.directory)

    def test_symlinked_runtime_member_is_refused(self):
        executable = self.directory / 'Esper.Resident'
        actual = self.directory.parent / 'actual'
        actual.write_bytes(executable.read_bytes())
        executable.unlink()
        executable.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, 'invalid_resident_file'):
            self.module.inspect(self.directory)

    def test_unknown_engine_or_protocol_cannot_be_activated(self):
        for key, value in (('protocol', 2), ('engine_sha256', 'f' * 64), ('source_commit', 'unknown')):
            previous = self.manifest[key]
            self.manifest[key] = value
            self.publish()
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, 'manifest'):
                self.module.inspect(self.directory)
            self.manifest[key] = previous


@unittest.skipUnless(os.environ.get('ESPER_RESIDENT_TEST_EXECUTABLE') and os.environ.get('ESPER_TEST_X64_BINARY'),
                     'requires compiled resident host and official x64 CLI')
class NativeResidentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.host = Path(os.environ['ESPER_RESIDENT_TEST_EXECUTABLE'])
        self.cli = Path(os.environ['ESPER_TEST_X64_BINARY'])
        self.assertEqual(hashlib.sha256(self.cli.read_bytes()).hexdigest(),
                         'd613a715b451d9fff4dfa902e3fa55e3431a595833c61c369622405d21020eee')
        self.environment = {**os.environ, 'DOTNET_GCHeapHardLimit': '40000000',
            'DOTNET_gcServer': '0', 'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT': '1',
            'DOTNET_BUNDLE_EXTRACT_BASE_DIR': str(self.directory / 'dotnet')}
        self.process = subprocess.Popen([str(self.host), '--server'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.environment)
        self.addCleanup(self.stop)
        self.ready = self.response()
        self.assertTrue(self.ready['ready'])
        self.assertTrue(self.ready['native_resident'])
        self.assertFalse(self.ready['native_streaming'])

    def stop(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def response(self):
        self.assertTrue(select.select([self.process.stdout], [], [], 30)[0], 'native host response timed out')
        return json.loads(self.process.stdout.readline(65537))

    def request(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()
        return self.response()

    def test_repeated_native_renders_match_pinned_cli_pcm_exactly_in_one_process(self):
        source = self.directory / 'source.wav'
        samples = array.array('h', (int(9000 * math.sin(2 * math.pi * 220 * index / 44100)
            + 3500 * math.sin(2 * math.pi * 440 * index / 44100)) for index in range(22050)))
        with wave.open(str(source), 'wb') as writer:
            writer.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
            writer.writeframes(samples.tobytes())
        for number, (pitch, length, bend) in enumerate((('C4', '240', 'AA#100#'),
                ('F#4', '650', 'AA#100#'), ('C4', '240', 'AA#100#'))):
            cli_output, host_output = self.directory / f'cli-{number}.wav', self.directory / f'host-{number}.wav'
            tail = [pitch, '100', '', '0', length, '30', '-400', '25', '0', '!140', bend]
            completed = subprocess.run([str(self.cli), str(source), str(cli_output), *tail],
                env=self.environment, capture_output=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
            result = self.request({'id': number, 'args': [str(source), str(host_output), *tail]})
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['process_id'], self.ready['process_id'])
            self.assertEqual(host_output.read_bytes(), cli_output.read_bytes())

    def test_bad_request_recovers_without_corrupting_protocol_or_restarting_native_process(self):
        for value in ({'id': {}, 'args': []}, {'id': 'valid', 'args': []}, {'id': 2, 'args': [None] * 13}):
            result = self.request(value)
            self.assertFalse(result['ok'])
            self.assertEqual(result['process_id'], self.ready['process_id'])

    def test_owner_pipe_eof_stops_resident_host(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.assertEqual(self.process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
