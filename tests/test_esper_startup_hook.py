"""A small hook reuses the verified official engine and its original runtime."""
from __future__ import annotations

import array
import base64
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('ESPER_HOOK_TEST_DLL') and os.environ.get('ESPER_TEST_X64_BINARY'),
                     'requires the built startup hook and verified official x64 engine')
class NativeStartupHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='esper-hook-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = Path(os.environ['ESPER_TEST_X64_BINARY'])
        self.assertEqual(hashlib.sha256(self.cli.read_bytes()).hexdigest(),
                         'd613a715b451d9fff4dfa902e3fa55e3431a595833c61c369622405d21020eee')
        self.environment = {**os.environ, 'DOTNET_GCHeapHardLimit': '40000000',
            'DOTNET_gcServer': '0', 'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT': '1',
            'DOTNET_BUNDLE_EXTRACT_BASE_DIR': str(self.root / 'dotnet')}
        self.hook_environment = {**self.environment,
            'DOTNET_STARTUP_HOOKS': os.environ['ESPER_HOOK_TEST_DLL'],
            'PHONE_WORKER_ESPER_HOOK_PROTOCOL': '1'}
        self.process = subprocess.Popen([str(self.cli), '--_resident'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.hook_environment)
        self.addCleanup(self.close)
        self.ready = self.response()
        self.assertTrue(self.ready['ready'])
        self.assertTrue(self.ready['native_resident'])
        self.assertFalse(self.ready['native_streaming'])
        self.source = self.root / 'source.wav'
        samples = array.array('h', (int(9000 * math.sin(2 * math.pi * 220 * number / 44100)
            + 3500 * math.sin(2 * math.pi * 440 * number / 44100)) for number in range(22050)))
        with wave.open(str(self.source), 'wb') as output:
            output.setparams((1, 2, 44100, 0, 'NONE', ''))
            output.writeframes(samples.tobytes())

    def close(self):
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
        self.assertTrue(select.select([self.process.stdout], [], [], 30)[0], 'native hook response timed out')
        return json.loads(self.process.stdout.readline(65537))

    def send(self, identifier, args):
        self.process.stdin.write(str(identifier).encode() + b'\t' +
            b'\t'.join(base64.b64encode(str(value).encode()) for value in args) + b'\n')
        self.process.stdin.flush()

    def test_multiple_renders_match_official_cli_byte_for_byte_without_restarting(self):
        for number, (pitch, duration, bend) in enumerate((('C4', '240', 'AA#100#'),
                ('F#4', '650', 'AA#100#'), ('C4', '240', 'AA#100#'))):
            cli_output, hook_output = self.root / f'cli-{number}.wav', self.root / f'hook-{number}.wav'
            tail = [pitch, '100', '', '0', duration, '30', '-400', '25', '0', '!140', bend]
            original = subprocess.run([str(self.cli), str(self.source), str(cli_output), *tail],
                env=self.environment, capture_output=True, timeout=30)
            self.assertEqual(original.returncode, 0, original.stderr[-1000:])
            self.send(number, [str(self.source), str(hook_output), *tail])
            result = self.response()
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['process_id'], self.ready['process_id'])
            self.assertEqual(hook_output.read_bytes(), cli_output.read_bytes())

    def test_malformed_and_native_failure_are_recoverable_in_the_same_process(self):
        self.process.stdin.write(b'valid\t%%%\n')
        self.process.stdin.flush()
        first = self.response()
        self.assertFalse(first['ok'])
        self.send('missing', ['/absent-input.wav', str(self.root / 'absent.wav'),
            'C4', '100', '', '0', '240', '30', '-400', '25', '0', '!140', 'AA'])
        second = self.response()
        self.assertFalse(second['ok'])
        self.send('restored', [str(self.source), str(self.root / 'recovered.wav'),
            'C4', '100', '', '0', '240', '30', '-400', '25', '0', '!140', 'AA'])
        final = self.response()
        self.assertTrue(final['ok'], final)
        self.assertEqual(final['process_id'], first['process_id'])
        self.assertTrue((self.root / 'recovered.wav').exists())

    def test_owner_eof_stops_a_request_without_leaving_a_native_process(self):
        self.send('long', [str(self.source), str(self.root / 'long.wav'),
            'C4', '100', '', '0', '20000', '30', '-400', '25', '0', '!140', 'AA'])
        started = time.monotonic()
        self.process.stdin.close()
        self.process.wait(timeout=3)
        self.assertEqual(self.process.returncode, 0)
        self.assertLess(time.monotonic() - started, 3)

    def test_bridge_session_understands_the_embedded_hook_protocol(self):
        spec = importlib.util.spec_from_file_location('tested_startup_bridge',
            ROOT / 'deploy/esper-utau/termux/resampler.py')
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        client = bridge._NativeSession([str(self.cli), '--_resident'], self.hook_environment,
            time.monotonic() + 15, None, ('startup-hook-fixture',))
        try:
            result = client.render([str(self.source), str(self.root / 'bridge.wav'),
                'C4', '100', '', '0', '240', '30', '-400', '25', '0', '!140', 'AA'],
                deadline=time.monotonic() + 15, cancelled=None)
            self.assertTrue((self.root / 'bridge.wav').exists())
            self.assertIsNotNone(client.process.pid)
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
