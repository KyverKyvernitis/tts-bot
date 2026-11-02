"""Prove loaded configuration without relying on unrestricted Android /proc."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_phone_worker_tts_lifecycle import tts  # noqa: F401
from test_straycat_migration import migration  # noqa: F401


def test_configuration_route_is_authenticated_and_does_not_load_voicebanks(tts, migration, monkeypatch):
    worker, handler = tts.worker, tts.handler
    monkeypatch.setenv('PHONE_WORKER_MIKU_FLAGS', 'g-5 B0')
    monkeypatch.setenv('PHONE_WORKER_PURURU_VOICEBANK_DIR', '/bancos/Púrpura')
    monkeypatch.setattr(worker, '_turbo_dependency_snapshot', lambda: pytest.fail('configuration probe must be cheap'))
    responses = []
    monkeypatch.setattr(worker, '_json_response', lambda _handler, status, body: responses.append((status, body)))
    handler.path = '/tts-agent/configuration'
    handler._require_auth = lambda: False
    handler.do_GET()
    assert not responses
    handler._require_auth = lambda: True
    handler.do_GET()
    status, body = responses.pop()
    assert int(status) == 200
    assert body['pid'] == os.getpid()
    assert body['runtime_directory'] == str(Path(worker.__file__).resolve().parent)
    assert body['renderer_configuration_sha256'] == migration.renderer_configuration_digest(dict(os.environ))
    before = body['renderer_configuration_sha256']
    monkeypatch.setenv('PHONE_WORKER_TOKEN', 'secret-one')
    monkeypatch.setenv('CORE_WORKER_TOKEN', 'secret-two')
    assert worker._ttv_renderer_configuration_digest() == before
    monkeypatch.setenv('PHONE_WORKER_ANNA_LENGTH_MODE', 'post-consonant')
    assert worker._ttv_renderer_configuration_digest() != before
    assert 'secret' not in json.dumps(body)


@pytest.fixture
def live_worker(migration, tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    # A real Python child has the same entrypoint/cwd/environment shape as the
    # Termux service; no mocked /proc reads or external service is involved.
    script = runtime / 'phone_worker.py'
    script.write_text('import time\nwhile True: time.sleep(1)\n')
    values = {
        'PHONE_WORKER_DIR': str(runtime), 'PHONE_WORKER_STATE_DIR': str(state),
        'PHONE_WORKER_MIKU_ENABLED': 'true', 'PHONE_WORKER_MIKU_FLAGS': 'g-5 B0',
        'PHONE_WORKER_PURURU_RESAMPLER_COMMAND': '/native/straycat-rs',
        'PHONE_WORKER_TOKEN': 'private-do-not-report',
    }
    child = subprocess.Popen([sys.executable, 'phone_worker.py'], cwd=runtime,
                             env={**os.environ, **values})
    (runtime / 'phone-worker.pid').write_text(str(child.pid))
    payload = {'pid': child.pid, 'started_at': time.time(), 'updated_at': time.time(),
               'runtime_kind': 'termux', 'source': 'termux-phone-worker',
               'control_plane_alive': True, 'http_port': 8766}
    (state / 'runtime-status.json').write_text(json.dumps(payload))
    try:
        yield values, runtime, state, child, payload
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_real_process_environment_and_runtime_must_match(migration, live_worker):
    values, runtime, state, child, payload = live_worker
    result = migration.process_snapshot(values, runtime)
    assert result['pid'] == child.pid
    assert result['identity_verified'] and result['runtime_matches'] and result['configuration_verified']
    assert 'private-do-not-report' not in json.dumps(result)
    stale = migration.process_snapshot({**values, 'PHONE_WORKER_MIKU_FLAGS': 'different'}, runtime)
    assert not stale['configuration_verified']
    assert stale['configuration_mismatches'] == ['PHONE_WORKER_MIKU_FLAGS']
    assert not migration.process_snapshot(values, runtime / 'other')['runtime_matches']
    payload['http_port'] = None
    (state / 'runtime-status.json').write_text(json.dumps(payload))
    assert not migration.process_snapshot(values, runtime)['identity_verified']


def test_android_proc_permission_denial_requires_endpoint_attestation(migration, live_worker, monkeypatch):
    values, runtime, state, child, payload = live_worker
    original_open, original_readlink = Path.open, os.readlink

    def restricted_open(path, *args, **kwargs):
        if str(path) == f'/proc/{child.pid}/environ':
            raise PermissionError('Android restriction')
        return original_open(path, *args, **kwargs)

    def restricted_link(path, *args, **kwargs):
        if str(path) == f'/proc/{child.pid}/cwd':
            raise PermissionError('Android restriction')
        return original_readlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', restricted_open)
    monkeypatch.setattr(os, 'readlink', restricted_link)
    result = migration.process_snapshot(values, runtime)
    assert result['identity_verified']
    assert not result['runtime_matches'] and not result['configuration_verified']
    assert result['runtime_pending_health'] and result['configuration_pending_health']
    configuration = {'pid': child.pid, 'process_started_at': payload['started_at'],
        'runtime_directory': str(runtime),
        'renderer_configuration_sha256': migration.renderer_configuration_digest(values)}
    migration.confirm_configuration(configuration, values, result)


def test_reused_unrelated_pid_is_not_a_live_start_lock(migration, live_worker):
    values, runtime, state, child, payload = live_worker
    lock = runtime / '.phone-worker-start.lock'
    lock.mkdir()
    (lock / 'owner.pid').write_text(str(child.pid))
    assert migration.startup_lock_state(values) == {'exists': True, 'owner_alive': False, 'owner_pid': None}
    assert lock.exists()
