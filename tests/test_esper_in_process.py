"""Resident bridge reuses immutable sources and retains physical cancellation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/esper-utau/termux/resampler.py'


def load():
    spec = importlib.util.spec_from_file_location('esper_in_process_tests', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pcm(seconds=.1, value=900):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
        wav.writeframes(int(value).to_bytes(2, 'little', signed=True) * round(44100 * seconds))
    return output.getvalue()


class InProcessTests(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.bank, self.output = self.base / 'original', self.base / 'output'
        self.bank.mkdir()
        self.output.mkdir()
        self.source = self.bank / 'recording.wav'
        self.source.write_bytes(pcm())
        self.root = self.base / 'runtime'
        self.release = self.root / 'releases/v2.5.0'
        self.release.mkdir(parents=True)
        self.client = self.module.InProcessEsper(root=self.root, cache=self.root / 'cache',
                                                container='voicepeak-arm64')
        self.addCleanup(self.client.close)
        self.calls = []
        self.patchers = [patch.object(self.module, 'runtime_info', return_value={
            'release': self.release, 'engine_sha256': self.module.ENGINE_SHA256,
            'config_sha256': self.module.CONFIG_SHA256, 'engine_elf_arm64': True}),
            patch.object(self.module.shutil, 'which', return_value='/fake/proot-distro'),
            patch.object(self.module, 'run_guest_in_process', side_effect=self.render)]
        for mocker in self.patchers:
            mocker.start()
            self.addCleanup(mocker.stop)

    def args(self, target=None):
        return [str(self.source), str(target or self.output / 'rendered.wav'), 'C4',
                '100', '', '0', '240', '30', '-400', '100', '0', '!140', 'AA']

    def render(self, command, workdir, deadline, **kwargs):
        self.calls.append((command, kwargs))
        (workdir / 'output.wav').write_bytes(pcm())
        return '', ''

    def synthesize(self, **kwargs):
        return self.client.synthesize(self.args(), deadline=time.monotonic() + 2, **kwargs)

    def test_original_recording_is_read_once_and_changes_invalidate_the_cache(self):
        original = self.source.read_bytes()
        with patch.object(self.module, 'source_data', wraps=self.module.source_data) as read:
            first = self.synthesize()
            second = self.synthesize()
            self.assertEqual(read.call_count, 1)
            self.assertEqual(self.client.source_cache_hits, 1)
            self.assertEqual(first['source_cache_key'], second['source_cache_key'])
            self.assertTrue(second['bridge_in_process'])
            self.assertFalse(second['native_resident'])
            self.assertFalse(second['native_streaming'])
            self.source.write_bytes(pcm(value=1100))
            third = self.synthesize()
            self.assertEqual(read.call_count, 2)
            self.assertNotEqual(third['source_cache_key'], first['source_cache_key'])
        self.assertEqual((Path(first['analysis_directory']) / 'source.wav').read_bytes(), original)
        self.assertEqual(set(path.name for path in self.bank.iterdir()), {'recording.wav'})

    def test_private_source_is_not_reread_but_tampering_is_rejected(self):
        first = self.synthesize()
        source = Path(first['analysis_directory']) / 'source.wav'
        read = self.module.read_regular
        private_reads = []
        def observe(path, **kwargs):
            if path == source:
                private_reads.append(path)
            return read(path, **kwargs)
        with patch.object(self.module, 'read_regular', side_effect=observe):
            self.synthesize()
            self.assertFalse(private_reads)
            source.write_bytes(pcm(value=1200))
            with self.assertRaisesRegex(self.module.EsperError, 'fonte privada'):
                self.synthesize()
        self.assertEqual(len(self.calls), 2)

    def test_caller_environment_is_passed_without_global_mutation(self):
        original = dict(os.environ)
        requested = {'PATH': '/caller-path', 'MARKER': 'caller'}
        self.synthesize(environment=requested)
        self.assertEqual(self.calls[0][1]['environment'], requested)
        self.assertEqual(dict(os.environ), original)
        self.assertEqual(requested, {'PATH': '/caller-path', 'MARKER': 'caller'})

    def test_explicit_bank_protection_uses_caller_environment(self):
        nested = self.bank / 'generated'
        nested.mkdir()
        with self.assertRaisesRegex(self.module.EsperError, 'voicebank original'):
            self.client.synthesize(self.args(nested / 'bad.wav'), deadline=time.monotonic() + 2,
                                  environment={'PHONE_WORKER_TETO_VOICEBANK_DIR': str(self.bank)})
        self.assertFalse(self.calls)

    def test_cancellation_and_expired_deadline_do_not_start_native_work(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(self.module.EsperError, 'interrompida'):
            self.synthesize(cancelled=cancelled)
        with self.assertRaises(TimeoutError):
            self.client.synthesize(self.args(), deadline=time.monotonic() - 1)
        self.assertFalse(self.calls)

    def test_cache_budget_is_bounded(self):
        self.client.max_source_bytes = len(pcm())
        for number in range(4):
            self.source.write_bytes(pcm(value=number + 1000))
            self.synthesize()
        self.assertLessEqual(self.client._source_bytes, self.client.max_source_bytes)
        self.assertEqual(len(self.client._sources), 1)

    def test_same_recording_still_has_one_physical_writer(self):
        active = 0
        maximum = 0
        lock = threading.Lock()
        def render(command, workdir, deadline, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(active, maximum)
            time.sleep(.02)
            (workdir / 'output.wav').write_bytes(pcm())
            with lock:
                active -= 1
            return '', ''
        def perform(number):
            return self.client.synthesize(self.args(self.output / f'{number}.wav'),
                                          deadline=time.monotonic() + 2)
        with patch.object(self.module, 'run_guest_in_process', side_effect=render), ThreadPoolExecutor(2) as pool:
            values = list(pool.map(perform, range(2)))
        self.assertTrue(all(value['ok'] for value in values))
        self.assertEqual(maximum, 1)

    def test_native_timeout_closes_process_before_releasing_slot(self):
        host = self.root / 'resident'
        host.mkdir()
        session = type('Session', (), {'identity': 'stamp', 'closed': False,
            'render': lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError('native deadline')),
            'close': lambda owner: setattr(owner, 'closed', True)})()
        self.client._native_slots = [{'busy': False, 'session': session}]
        with patch.object(self.client, '_resident_host', return_value=(host, 'stamp')):
            with self.assertRaises(TimeoutError):
                self.client._run_resident(self.root, self.client.cache, self.client.cache / 'source',
                    self.client.cache / 'job', self.args(), time.monotonic() + 1, None, {})
        self.assertTrue(session.closed)
        self.assertIsNone(self.client._native_slots[0]['session'])
        self.assertFalse(self.client._native_slots[0]['busy'])

    def test_source_lock_wait_observes_cancellation(self):
        path = self.base / 'recording.lock'
        cancelled = threading.Event()
        with self.module.source_lock(path, time.monotonic() + 2):
            timer = threading.Timer(.025, cancelled.set)
            timer.start()
            start = time.monotonic()
            try:
                with self.assertRaisesRegex(self.module.EsperError, 'interrompida'):
                    with self.module.source_lock(path, time.monotonic() + 2, cancelled=cancelled):
                        self.fail('busy source lock was acquired')
            finally:
                timer.join()
        self.assertLess(time.monotonic() - start, .3)

    def test_embedded_hook_is_content_addressed_and_tampering_is_rejected(self):
        data = b'MZ' + bytes(2046)
        digest = hashlib.sha256(data).hexdigest()
        with patch.object(self.module, 'RESIDENT_HOOK_BASE64', base64.b64encode(data).decode()), \
             patch.object(self.module, 'RESIDENT_HOOK_SHA256', digest):
            path, identity = self.client._resident_host(self.root, time.monotonic() + 1)
            self.assertEqual((path / 'Esper.StartupHook.dll').read_bytes(), data)
            self.assertEqual(identity[0], 'startup-hook')
            again, same = self.client._resident_host(self.root, time.monotonic() + 1)
            self.assertEqual((again, same), (path, identity))
            (path / 'Esper.StartupHook.dll').write_bytes(data[:-1] + b'X')
            with self.assertRaisesRegex(self.module.EsperError, 'instalado mudou'):
                self.client._resident_host(self.root, time.monotonic() + 1)


class NativeSessionTests(unittest.TestCase):
    def test_two_requests_reuse_one_native_process_and_close_reaps_it(self):
        bridge = load()
        program = '''import json,os,sys
print(json.dumps({'protocol':1,'ready':True,'native_resident':True,'source_commit':%r}),flush=True)
for line in sys.stdin:
    value=json.loads(line)
    print(json.dumps({'id':value['id'],'ok':True,'process_id':os.getpid()}),flush=True)
''' % bridge.RESIDENT_SOURCE_COMMIT
        session = bridge._NativeSession([sys.executable, '-u', '-c', program], dict(os.environ),
                                        time.monotonic() + 2, None, 'test')
        process = session.process
        try:
            session.render(['test'] * 13, deadline=time.monotonic() + 2, cancelled=None)
            session.render(['again'] * 13, deadline=time.monotonic() + 2, cancelled=None)
            self.assertIs(session.process, process)
            self.assertIsNone(process.poll())
        finally:
            session.close()
        self.assertIsNotNone(process.poll())

    def test_startup_hook_transport_preserves_unicode_and_argument_boundaries(self):
        bridge = load()
        program = '''import base64,json,os,sys
print(json.dumps({'protocol':1,'ready':True,'native_resident':True,'source_commit':%r,'request_encoding':'tsv-base64-v1'}),flush=True)
for line in sys.stdin:
    fields=line.rstrip('\\n').split('\\t')
    args=[base64.b64decode(value,validate=True).decode('utf8') for value in fields[1:]]
    assert len(args)==13 and args[0]=='少女A com espaço' and args[4]==''
    print(json.dumps({'id':fields[0],'ok':True}),flush=True)
''' % bridge.RESIDENT_SOURCE_COMMIT
        session = bridge._NativeSession([sys.executable, '-u', '-c', program], dict(os.environ),
                                        time.monotonic() + 2, None, 'test')
        try:
            args = ['少女A com espaço'] + ['value'] * 12
            args[4] = ''
            session.render(args, deadline=time.monotonic() + 2, cancelled=None)
        finally:
            session.close()


if __name__ == '__main__':
    unittest.main()
