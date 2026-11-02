"""Exercise recovery from busy launchers and Android process inspection limits."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

from test_straycat_migration import _health, _mock_worker, installed, migration


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


@pytest.fixture
def clock(migration, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(migration.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(migration.time, 'sleep', clock.sleep)
    return clock


def _values(migration, installed):
    _, values = migration.read_environment(installed.env)
    values['_configuration_path'] = str(installed.env)
    return values


def _process(runtime, *, ready=True, pending=False, pid=201, started_at=1700000000.5):
    result = {
        'pid': pid, 'started_at': started_at, 'http_port': 8768,
        'identity_verified': True, 'runtime_matches': ready,
        'configuration_verified': ready, 'configuration_mismatches': [],
        'expected_runtime': str(Path(runtime).resolve()),
    }
    if pending:
        result.update(runtime_matches=False, configuration_verified=False,
                      runtime_pending_health=True, configuration_pending_health=True)
    elif not ready:
        result['configuration_mismatches'] = ['PHONE_WORKER_MIKU_RESAMPLER_COMMAND']
    return result


def _configuration(migration, values, process):
    return {
        'pid': process['pid'], 'process_started_at': process['started_at'],
        'renderer_configuration_sha256': migration.renderer_configuration_digest(values),
        'runtime_directory': process['expected_runtime'],
    }


def test_live_launcher_lock_does_not_hide_an_already_matching_worker(
        migration, installed, monkeypatch, clock):
    lock = installed.runtime / '.phone-worker-start.lock'
    lock.mkdir()
    owner = lock / 'owner.pid'
    owner.write_text('897\n')
    values = _values(migration, installed)
    monkeypatch.setattr(migration, 'process_snapshot', lambda *args: _process(installed.runtime))
    monkeypatch.setattr(migration, 'startup_lock_state', lambda *args: {
        'exists': True, 'owner_alive': True, 'owner_pid': 897,
    })
    monkeypatch.setattr(migration.subprocess, 'run', lambda *args, **kwargs:
                        pytest.fail('a matching worker must not be restarted'))
    diagnostics = {}

    process = migration.restart_worker(values, str(installed.runtime), diagnostics, budget=5)

    assert process['configuration_verified']
    assert diagnostics['restart_attempts'] == 0
    assert diagnostics['process_restarted'] is False
    assert clock.now == 0
    assert owner.read_text() == '897\n'


def test_busy_launcher_finishes_before_saved_configuration_is_restarted(
        migration, installed, monkeypatch, clock):
    values = _values(migration, installed)
    launches = []

    def run(command, **kwargs):
        launches.append((clock.now, command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(migration.subprocess, 'run', run)
    monkeypatch.setattr(migration, 'process_snapshot', lambda *args:
                        _process(installed.runtime, ready=bool(launches)))
    monkeypatch.setattr(migration, 'startup_lock_state', lambda *args: {
        'exists': clock.now < 3, 'owner_alive': clock.now < 3, 'owner_pid': 897,
    })
    diagnostics = {}

    process = migration.restart_worker(values, str(installed.runtime), diagnostics, budget=10)

    assert process['configuration_verified']
    assert len(launches) == 1
    launched_at, command, kwargs = launches[0]
    assert launched_at == 3
    assert command == ['bash', str(installed.runtime / 'start-phone-worker.sh'), '--restart']
    assert kwargs['env']['PHONE_WORKER_ENV'] == str(installed.env)
    assert kwargs['timeout'] <= 7
    assert diagnostics['launcher_exit_code'] == 0
    assert diagnostics['stage'] == 'process_verified'


def test_exit_zero_cannot_confirm_a_worker_still_using_the_old_configuration(
        migration, installed, monkeypatch, clock):
    values = _values(migration, installed)
    launches = []

    def run(command, **kwargs):
        launches.append(clock.now)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(migration.subprocess, 'run', run)
    monkeypatch.setattr(migration, 'process_snapshot', lambda *args:
                        _process(installed.runtime, ready=False))
    monkeypatch.setattr(migration, 'startup_lock_state', lambda *args: {
        'exists': False, 'owner_alive': False, 'owner_pid': None,
    })
    diagnostics = {}

    with pytest.raises(migration.MigrationError, match='nova configuração'):
        migration.restart_worker(values, str(installed.runtime), diagnostics, budget=100)

    assert len(launches) == 3
    assert all(later - earlier >= 35 for earlier, later in zip(launches, launches[1:]))
    assert clock.now == 100
    assert diagnostics['launcher_exit_code'] == 0
    assert not diagnostics['process']['configuration_verified']
    assert diagnostics['process']['configuration_mismatches']


def test_slow_first_startup_gets_time_to_finish_without_repeated_restarts(
        migration, installed, monkeypatch, clock):
    values = _values(migration, installed)
    launches = []

    def run(command, **kwargs):
        launches.append(clock.now)
        return subprocess.CompletedProcess(command, 1)

    def snapshot(*args):
        process = _process(installed.runtime, ready=clock.now >= 35)
        process['identity_verified'] = clock.now >= 35
        return process

    monkeypatch.setattr(migration.subprocess, 'run', run)
    monkeypatch.setattr(migration, 'process_snapshot', snapshot)
    monkeypatch.setattr(migration, 'startup_lock_state', lambda *args: {
        'exists': False, 'owner_alive': False, 'owner_pid': None,
    })
    diagnostics = {}

    process = migration.restart_worker(values, str(installed.runtime), diagnostics, budget=60)

    assert launches == [0]
    assert clock.now == 35
    assert process['configuration_verified']
    assert diagnostics['stage'] == 'process_verified'


def test_persistent_live_lock_is_bounded_and_preserved(
        migration, installed, monkeypatch, clock):
    lock = installed.runtime / '.phone-worker-start.lock'
    lock.mkdir()
    owner = lock / 'owner.pid'
    owner.write_text('897\n')
    values = _values(migration, installed)
    monkeypatch.setattr(migration, 'process_snapshot', lambda *args:
                        _process(installed.runtime, ready=False))
    monkeypatch.setattr(migration, 'startup_lock_state', lambda *args: {
        'exists': True, 'owner_alive': True, 'owner_pid': 897,
    })
    monkeypatch.setattr(migration.subprocess, 'run', lambda *args, **kwargs:
                        pytest.fail('an active launcher must finish before another launch'))
    diagnostics = {}

    with pytest.raises(migration.MigrationError, match='nenhum lock vivo foi apagado'):
        migration.restart_worker(values, str(installed.runtime), diagnostics, budget=2.25)

    assert clock.now == 2.25
    assert diagnostics['restart_attempts'] == 0
    assert diagnostics['startup_lock']['owner_alive']
    assert owner.read_text() == '897\n'


@pytest.mark.parametrize('valid_attestation', [True, False])
def test_android_pending_process_needs_attestation_even_when_warmup_is_disabled(
        migration, installed, monkeypatch, clock, capsys, valid_attestation):
    probes = []
    real_restart = migration.restart_worker

    def configuration(values, process):
        probes.append(process['pid'])
        response = _configuration(migration, values, process)
        if not valid_attestation:
            response['renderer_configuration_sha256'] = '0' * 64
        return response

    monkeypatch.setattr(migration, 'process_snapshot', lambda *args:
                        _process(installed.runtime, pending=True))
    monkeypatch.setattr(migration, 'startup_lock_state', lambda *args: {
        'exists': True, 'owner_alive': True, 'owner_pid': 897,
    })
    monkeypatch.setattr(migration, 'local_configuration', configuration)
    monkeypatch.setattr(migration, 'restart_worker', lambda values, runtime, diagnostics:
                        real_restart(values, runtime, diagnostics, budget=1))
    monkeypatch.setattr(migration, 'warmup', lambda *args, **kwargs:
                        pytest.fail('--no-warmup must skip synthesis'))

    result = migration.main(['--env', str(installed.env), '--runtime-dir', str(installed.runtime),
                             '--no-warmup'])

    report = json.loads(capsys.readouterr().out)
    assert probes
    assert result == (0 if valid_attestation else 1)
    assert report['ok'] is valid_attestation
    assert report['phone_synthesis_verified'] is False
    assert report['diagnostics']['restart_attempts'] == 0
    if valid_attestation:
        assert report['diagnostics']['process']['configuration_verified_via'] == \
            'authenticated_local_endpoint'
        assert report['diagnostics']['process']['runtime_matches']
    else:
        assert report['diagnostics']['configuration_probe_error'] == 'MigrationError'
        assert not report['diagnostics']['process']['configuration_verified']


def test_android_configuration_probe_is_authenticated_loopback_without_proxy_or_redirect(
        migration, monkeypatch):
    captured = []
    handlers = []
    payload = {'pid': 201, 'renderer_configuration_sha256': 'fixture'}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, maximum):
            assert maximum <= 65536
            return json.dumps(payload).encode()

    class Opener:
        def open(self, request, timeout):
            captured.append((request, timeout))
            return Response()

    def build_opener(*arguments):
        handlers.extend(arguments)
        return Opener()

    monkeypatch.setattr(migration.urllib.request, 'build_opener', build_opener)
    monkeypatch.setenv('HTTP_PROXY', 'http://untrusted.invalid:8888')

    assert migration.local_configuration({'PHONE_WORKER_PORT': '8766',
        'PHONE_WORKER_TOKEN': 'configuration-probe-secret'}, {'http_port': 8768}) == payload

    request, timeout = captured[0]
    assert request.full_url == 'http://127.0.0.1:8768/tts-agent/configuration'
    assert request.get_header('Authorization') == 'Bearer configuration-probe-secret'
    assert request.get_method() == 'GET'
    assert timeout == 2
    assert any(isinstance(handler, migration.urllib.request.ProxyHandler) and handler.proxies == {}
               for handler in handlers)
    assert any(isinstance(handler, migration.NoRedirect) for handler in handlers)


@pytest.mark.parametrize('changed_field,changed_value', [
    ('pid', 202), ('process_started_at', 1700000001.5),
    ('runtime_directory', '/different/release'), ('renderer_configuration_sha256', '0' * 64),
])
def test_warmup_rejects_another_process_runtime_or_configuration_before_synthesis(
        migration, monkeypatch, changed_field, changed_value):
    values = {'PHONE_WORKER_PORT': '8766'}
    process = _process('/approved/release')
    health = _health(['anna-nyui'], **_configuration(migration, values, process))
    health[changed_field] = changed_value
    requests, _ = _mock_worker(migration, monkeypatch, health)

    with pytest.raises(migration.MigrationError):
        migration.warmup(values, ['anna-nyui'], expected_process=process)

    assert len(requests) == 1
    assert urlsplit(requests[0][0].full_url).path == '/tts-agent/health'


def test_not_ready_voice_keeps_individual_errors_and_resource_diagnostics(
        migration, monkeypatch):
    voices = ['kasane-teto', 'hatsune-miku', 'anna-nyui', 'pururu-purpura']
    health = _health(voices)
    teto = health['tts_agent']['teto']
    teto['resources'] = {'ok': False, 'reason': 'memória disponível baixa',
                         'memory_available_mb': 900, 'minimum_memory_mb': 1200}
    teto['ttv_voices'][2].update(ready=False, last_error='Straycat nativo indisponível')
    requests, _ = _mock_worker(migration, monkeypatch, health)
    diagnostics = {}

    with pytest.raises(migration.MigrationError, match='anna-nyui'):
        migration.warmup({}, voices, diagnostics=diagnostics)

    assert len(requests) == 1
    assert diagnostics['stage'] == 'voices_not_ready'
    assert [voice['id'] for voice in diagnostics['voices']] == voices
    assert diagnostics['voices'][2]['last_error'] == 'Straycat nativo indisponível'
    assert diagnostics['voices'][0]['ready'] is True
    assert diagnostics['resources'] == teto['resources']


def test_synthesis_failure_records_the_voice_and_successful_samples_before_it(
        migration, monkeypatch):
    voices = ['kasane-teto', 'hatsune-miku', 'anna-nyui', 'pururu-purpura']
    _mock_worker(migration, monkeypatch, _health(voices))
    original_build = migration.urllib.request.build_opener

    def build_opener(*arguments):
        opener = original_build(*arguments)
        original_open = opener.open

        def open_request(request, timeout):
            if request.data and json.loads(request.data)['ttv_voice_id'] == 'anna-nyui':
                raise URLError('synthesis stopped')
            return original_open(request, timeout)

        opener.open = open_request
        return opener

    monkeypatch.setattr(migration.urllib.request, 'build_opener', build_opener)
    diagnostics = {}

    with pytest.raises(URLError, match='synthesis stopped'):
        migration.warmup({}, voices, diagnostics=diagnostics)

    assert diagnostics['stage'] == 'warming_voice'
    assert diagnostics['warming_voice'] == 'anna-nyui'
    assert [row['voice_id'] for row in diagnostics['completed_warmup']] == voices[:2]
    assert all(row['ok'] for row in diagnostics['completed_warmup'])


def test_warmup_preserves_tetos_total_length_mode_and_verifies_all_four_voices(
        migration, monkeypatch):
    voices = ['kasane-teto', 'hatsune-miku', 'anna-nyui', 'pururu-purpura']
    values = {'PHONE_WORKER_TETO_LENGTH_MODE': 'total', 'PHONE_WORKER_PORT': '8766'}
    original_values = dict(values)
    process = _process('/approved/release')
    health = _health(voices, **_configuration(migration, values, process))
    health['tts_agent']['teto']['ttv_voices'][0]['length_mode'] = 'total'
    requests, _ = _mock_worker(migration, monkeypatch, health)
    diagnostics = {}

    results = migration.warmup(values, voices, expected_process=process, diagnostics=diagnostics)

    assert values == original_values
    assert [row['voice_id'] for row in results] == voices
    assert len(requests) == 5
    assert diagnostics['stage'] == 'verified'
    assert 'warming_voice' not in diagnostics
    assert diagnostics['voices'][0]['length_mode'] == 'total'
    assert all(row['length_mode'] == 'post-consonant' for row in diagnostics['voices'][1:])


def test_configuration_digest_includes_inherited_settings_with_file_precedence(
        migration, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_MIKU_FLAGS', 'g-5 B0')
    assert migration.renderer_configuration_digest({}) == \
        migration.renderer_configuration_digest({'PHONE_WORKER_MIKU_FLAGS': 'g-5 B0'})
    assert migration.renderer_configuration_digest({'PHONE_WORKER_MIKU_FLAGS': 'g10'}) != \
        migration.renderer_configuration_digest({})


def test_cli_redacts_tokens_in_nested_diagnostics_and_the_saved_report(
        migration, installed, monkeypatch, capsys):
    phone_secret = 'phone-token-DO-NOT-PRINT'
    core_secret = 'core-token-DO-NOT-PRINT'
    monkeypatch.setenv('CORE_WORKER_TOKEN', core_secret)

    def restart(values, runtime, diagnostics):
        diagnostics['voices'] = [{'id': 'anna-nyui', 'last_error': 'failed ' + phone_secret}]
        diagnostics['resources'] = {'details': ['auth ' + core_secret,
                                               {'nested_error': phone_secret + core_secret}]}
        return {'pid': 201}

    def warmup(*args, **kwargs):
        raise ValueError('failed ' + phone_secret + ' ' + core_secret)

    monkeypatch.setattr(migration, 'restart_worker', restart)
    monkeypatch.setattr(migration, 'warmup', warmup)

    assert migration.main(['--env', str(installed.env), '--runtime-dir', str(installed.runtime)]) == 1

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    saved = Path(report['diagnostic_report']).read_text()
    for output in (captured.out, captured.err, saved):
        assert phone_secret not in output
        assert core_secret not in output
    assert report['diagnostics']['voices'][0]['last_error'] == 'failed [oculto]'
    assert report['diagnostics']['resources']['details'][1]['nested_error'] == '[oculto][oculto]'
    assert json.loads(saved) == report
    assert report['phone_synthesis_verified'] is False
