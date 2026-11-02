"""Migrate installed TTV voices to Teto's verified native engine without losing settings."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import stat
import struct
import subprocess
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
ACOUSTIC_SETTINGS = (
    'BASE_PITCH', 'SPEECH_RATE', 'VELOCITY', 'MODULATION', 'TEMPO', 'FLAGS',
    'RENDER_THREADS', 'FRAGMENT_CACHE_MB', 'PCM_CACHE_MB', 'NATIVE_ANALYSIS_CACHE_MB',
)


@pytest.fixture
def migration(monkeypatch, tmp_path):
    path = ROOT / 'deploy/ttv-straycat/termux/configurar-straycat.py'
    spec = importlib.util.spec_from_file_location('test_straycat_migration_module', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(module, 'report_path', lambda: tmp_path / 'report.json')
    return module


def _elf(machine=62):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', header, 18, machine)
    return bytes(header) + b'fixture native engine bytes'


@pytest.fixture
def installed(migration, monkeypatch, tmp_path):
    monkeypatch.setattr(migration.platform, 'machine', lambda: 'x86_64')
    engine = tmp_path / 'native engine' / 'straycat-rs'
    engine.parent.mkdir()
    engine.write_bytes(_elf())
    engine.chmod(0o700)
    probes = []

    def probe(command, **kwargs):
        probes.append((command, kwargs))
        assert command == [str(engine), '--version']
        assert kwargs == {'capture_output': True, 'timeout': 5, 'check': False}
        return subprocess.CompletedProcess(command, 0, b'straycat-rs 1.1.0\n', b'')

    monkeypatch.setattr(migration.subprocess, 'run', probe)
    runtime = tmp_path / 'worker runtime'
    (runtime / 'teto_renderer').mkdir(parents=True)
    (runtime / 'phone_worker.py').write_text('PHONE_WORKER_VERSION = "1.11.49"\n')
    (runtime / 'teto_renderer/renderer.py').write_text('def _native_source_lease(): pass\n')
    (runtime / 'start-phone-worker.sh').write_text('#!/bin/bash\nexit 0\n')
    banks = {}
    for voice in ('TETO', 'MIKU', 'ANNA', 'PURURU'):
        bank = tmp_path / ('bank ' + voice.lower())
        bank.mkdir()
        (bank / 'oto.ini').write_text('voice.wav=a,1,30,-90,15,7\n')
        (bank / 'voice.wav').write_bytes(b'preexisting bank audio ' + voice.encode())
        banks[voice] = bank
    raw = (
        '# Personal phone settings; preserve accents: olá\n'
        'UNRELATED_SECRET="a private token $still_literal"\n'
        'BOT_SELECTED_VOICE="anna-nyui"\n'
        'PHONE_WORKER_TOKEN="phone-token-DO-NOT-PRINT"\n'
        'PHONE_WORKER_PORT=8766\n'
        'PHONE_WORKER_HOST="https://external.invalid"\n'
        f'PHONE_WORKER_DIR={json.dumps(str(runtime))}\n'
        '  export PHONE_WORKER_TETO_ENABLED = "true"  # original Teto formatting\n'
        'PHONE_WORKER_TETO_BACKEND=utau\n'
        f'PHONE_WORKER_TETO_RESAMPLER_COMMAND={json.dumps(shlex.join([str(engine)]))}\n'
        f'PHONE_WORKER_TETO_VOICEBANK_DIR={json.dumps(str(banks["TETO"]))}\n'
        'PHONE_WORKER_TETO_BASE_PITCH="D#4" # preserve inline comment\n'
        'PHONE_WORKER_TETO_SPEECH_RATE=1.15\n'
        'PHONE_WORKER_TETO_VELOCITY=87\n'
        'PHONE_WORKER_TETO_MODULATION=11\n'
        'PHONE_WORKER_TETO_TEMPO=152\n'
        'PHONE_WORKER_TETO_FLAGS="g-5 B0"\n'
        'PHONE_WORKER_TETO_RENDER_THREADS=3\n'
        'PHONE_WORKER_TETO_FRAGMENT_CACHE_MB=192\n'
        'PHONE_WORKER_TETO_PCM_CACHE_MB=160\n'
        'PHONE_WORKER_TETO_NATIVE_ANALYSIS_CACHE_MB=96\n'
        'PHONE_WORKER_TETO_LENGTH_MODE=legacy\n'
        'PHONE_WORKER_TETO_ALIAS_PREFIX="private-prefix"\n'
        'PHONE_WORKER_TETO_PHONEMIZER="private-phonemizer"\n'
        '# Existing banks and identity remain the user choices.\n'
    )
    for prefix in ('MIKU', 'ANNA', 'PURURU'):
        raw += (
            f'PHONE_WORKER_{prefix}_ENABLED=true\n'
            f'PHONE_WORKER_{prefix}_VOICEBANK_DIR={json.dumps(str(banks[prefix]))}\n'
            f'PHONE_WORKER_{prefix}_ALIAS_PREFIX="{prefix.lower()}-prefix"\n'
            f'PHONE_WORKER_{prefix}_ALIAS_SUFFIX="{prefix.lower()}-suffix"\n'
            f'PHONE_WORKER_{prefix}_PHONEMIZER="{prefix.lower()}-phonemizer"\n'
            f'PHONE_WORKER_{prefix}_LANGUAGE="pt-BR"\n'
            f'PHONE_WORKER_{prefix}_RESAMPLER_COMMAND="old wrapper --custom"\n'
            f'PHONE_WORKER_{prefix}_BASE_PITCH=C2\n'
        )
    env = tmp_path / 'phone.env'
    env.write_bytes(raw.encode())
    before = {str(path): path.read_bytes() for bank in banks.values() for path in bank.iterdir()}
    return SimpleNamespace(engine=engine, probes=probes, runtime=runtime, banks=banks,
                           env=env, raw=raw.encode(), bank_contents=before)


def _migrate(migration, installed, **kwargs):
    return migration.migrate(installed.env, runtime_dir=installed.runtime, **kwargs)


def test_migration_clones_only_acoustics_and_keeps_settings_banks_and_secrets(migration, installed):
    report, original_values = _migrate(migration, installed)
    assert report['ok'] and report['configuration_changed']
    assert report['voices'] == ['hatsune-miku', 'anna-nyui', 'pururu-purpura']
    assert report['engine_sha256'] == hashlib.sha256(installed.engine.read_bytes()).hexdigest()
    assert report['teto_settings_preserved']
    assert not report['bank_downloaded'] and not report['bank_files_changed']
    assert report['pururu_language'] == 'pt-BR'
    assert 'phone-token-DO-NOT-PRINT' not in json.dumps(report)
    backup = Path(report['configuration_backup'])
    assert backup.read_bytes() == installed.raw
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(installed.env.stat().st_mode) == 0o600
    updated, values = migration.read_environment(installed.env)
    expected_updated = {'PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'}
    for prefix in ('MIKU', 'ANNA', 'PURURU'):
        root = 'PHONE_WORKER_' + prefix + '_'
        assert shlex.split(values[root + 'RESAMPLER_COMMAND']) == [str(installed.engine)]
        assert values[root + 'NATIVE_RESAMPLER_SHA256'] == report['engine_sha256']
        assert values[root + 'LENGTH_MODE'] == 'post-consonant'
        for suffix in ACOUSTIC_SETTINGS:
            assert values[root + suffix] == original_values['PHONE_WORKER_TETO_' + suffix]
        assert values[root + 'VOICEBANK_DIR'] == str(installed.banks[prefix])
        for suffix in ('ALIAS_PREFIX', 'ALIAS_SUFFIX', 'PHONEMIZER', 'LANGUAGE', 'ENABLED'):
            assert values[root + suffix] == original_values[root + suffix]
        expected_updated.update(root + suffix for suffix in (
            'RESAMPLER_COMMAND', 'NATIVE_RESAMPLER_SHA256', 'LENGTH_MODE', *ACOUSTIC_SETTINGS))
    assert {key for key, value in values.items() if original_values.get(key) != value} == expected_updated
    for line in installed.raw.decode().splitlines(keepends=True):
        match = migration.KEY.match(line)
        if not match or match[1] not in expected_updated:
            assert line.encode() in updated
    assert values['PHONE_WORKER_TETO_LENGTH_MODE'] == 'legacy'
    assert all(Path(path).read_bytes() == data for path, data in installed.bank_contents.items())


def test_dry_run_is_reviewable_and_idempotent_migration_has_only_one_backup(migration, installed):
    dry_report, _ = _migrate(migration, installed, dry_run=True)
    assert dry_report['ok'] and dry_report['dry_run']
    assert dry_report['configuration_changed'] and dry_report['configuration_backup'] is None
    assert installed.env.read_bytes() == installed.raw
    assert not list(installed.env.parent.glob('phone.env.bak-straycat-*'))
    first, _ = _migrate(migration, installed)
    migrated = installed.env.read_bytes()
    second, _ = _migrate(migration, installed)
    assert first['configuration_changed']
    assert second['ok'] and not second['configuration_changed']
    assert second['configuration_backup'] is None
    assert installed.env.read_bytes() == migrated
    assert len(list(installed.env.parent.glob('phone.env.bak-straycat-*'))) == 1


def test_disabled_voices_keep_their_original_commands_and_acoustics(migration, installed):
    raw = installed.raw.replace(b'PHONE_WORKER_ANNA_ENABLED=true', b'PHONE_WORKER_ANNA_ENABLED=false')
    raw = raw.replace(b'PHONE_WORKER_PURURU_ENABLED=true', b'PHONE_WORKER_PURURU_ENABLED=false')
    installed.env.write_bytes(raw)
    report, _ = _migrate(migration, installed)
    assert report['voices'] == ['hatsune-miku']
    updated = installed.env.read_bytes()
    for line in raw.splitlines(keepends=True):
        if line.startswith((b'PHONE_WORKER_ANNA_', b'PHONE_WORKER_PURURU_')):
            assert line in updated
    assert b'PHONE_WORKER_ANNA_NATIVE_RESAMPLER_SHA256' not in updated
    assert b'PHONE_WORKER_PURURU_NATIVE_RESAMPLER_SHA256' not in updated


@pytest.mark.parametrize('prefix', ['MIKU', 'ANNA', 'PURURU'])
@pytest.mark.parametrize('home_variable', ['$HOME', '${HOME}'])
def test_home_relative_custom_bank_paths_are_validated_without_rewriting_them(
        migration, installed, prefix, home_variable):
    relative = 'my custom banks/' + prefix.lower()
    bank = migration.Path.home() / relative
    bank.mkdir(parents=True)
    recording = bank / 'voice.wav'
    recording.write_bytes(b'personal bank recording')
    key = f'PHONE_WORKER_{prefix}_VOICEBANK_DIR'
    configured = home_variable + '/' + relative
    line = key + '=' + json.dumps(configured) + '\n'
    raw = ''.join(line if existing.startswith(key + '=') else existing
                  for existing in installed.raw.decode().splitlines(keepends=True)).encode()
    installed.env.write_bytes(raw)
    report, _ = _migrate(migration, installed)
    assert report['ok']
    assert line.encode() in installed.env.read_bytes()
    assert recording.read_bytes() == b'personal bank recording'
    assert not report['bank_downloaded'] and not report['bank_files_changed']


@pytest.mark.parametrize('prefix', ['MIKU', 'ANNA'])
def test_existing_english_only_custom_bank_is_used_and_preserved(migration, installed, prefix):
    primary = f'PHONE_WORKER_{prefix}_VOICEBANK_DIR'
    fallback = f'PHONE_WORKER_{prefix}_ENGLISH_VOICEBANK_DIR'
    relative = 'english custom banks/' + prefix.lower()
    bank = migration.Path.home() / relative
    bank.mkdir(parents=True)
    (bank / 'oto.ini').write_text('voice.wav=a,1,30,-90,15,7\n')
    configured = '${HOME}/' + relative
    fallback_line = fallback + '=' + json.dumps(configured) + '\n'
    raw = ''.join(line for line in installed.raw.decode().splitlines(keepends=True)
                  if not line.startswith(primary + '=')) + fallback_line
    installed.env.write_bytes(raw.encode())
    report, _ = _migrate(migration, installed)
    assert report['ok']
    updated, values = migration.read_environment(installed.env)
    assert primary not in values
    assert values[fallback] == configured
    assert fallback_line.encode() in updated
    assert (bank / 'oto.ini').read_text() == 'voice.wav=a,1,30,-90,15,7\n'


@pytest.mark.parametrize('prefix', ['MIKU', 'ANNA'])
def test_native_bank_path_takes_precedence_over_an_unavailable_english_fallback(
        migration, installed, prefix):
    line = f'PHONE_WORKER_{prefix}_ENGLISH_VOICEBANK_DIR="/unavailable/old-bank"\n'
    installed.env.write_bytes(installed.raw + line.encode())
    report, _ = _migrate(migration, installed)
    assert report['ok']
    assert line.encode() in installed.env.read_bytes()


def test_pururu_does_not_use_an_english_bank_as_its_portuguese_source(migration, installed):
    primary = 'PHONE_WORKER_PURURU_VOICEBANK_DIR'
    raw = ''.join(line for line in installed.raw.decode().splitlines(keepends=True)
                  if not line.startswith(primary + '='))
    raw += 'PHONE_WORKER_PURURU_ENGLISH_VOICEBANK_DIR=' + json.dumps(str(installed.banks['PURURU'])) + '\n'
    installed.env.write_bytes(raw.encode())
    with pytest.raises(migration.MigrationError, match='pururu-purpura'):
        _migrate(migration, installed)
    assert installed.env.read_bytes() == raw.encode()
    assert not list(installed.env.parent.glob('phone.env.bak-straycat-*'))


@pytest.mark.parametrize('version', ['1.11.47', '1.10.99', '1.9.999'])
def test_old_installed_runtime_is_rejected_before_touching_env(migration, installed, version):
    (installed.runtime / 'phone_worker.py').write_text(f'PHONE_WORKER_VERSION = "{version}"\n')
    with pytest.raises(migration.MigrationError, match='1.11.49'):
        _migrate(migration, installed)
    assert installed.env.read_bytes() == installed.raw
    assert installed.probes == []
    assert not list(installed.env.parent.glob('phone.env.bak-straycat-*'))


def test_version_alone_without_native_runtime_support_is_rejected(migration, installed):
    (installed.runtime / 'teto_renderer/renderer.py').write_text('# older renderer\n')
    with pytest.raises(migration.MigrationError, match='1.11.49'):
        _migrate(migration, installed)
    assert installed.env.read_bytes() == installed.raw


@pytest.mark.parametrize('case', ['script', 'wrong-architecture', 'not-executable', 'wrapper'])
def test_non_native_wrong_architecture_and_wrapper_engines_are_rejected(migration, installed, case):
    if case == 'script':
        installed.engine.write_bytes(b'#!/bin/sh\necho straycat-rs 1.1.0\n')
    elif case == 'wrong-architecture':
        installed.engine.write_bytes(_elf(machine=183))
    elif case == 'not-executable':
        installed.engine.chmod(0o600)
    else:
        raw = migration.updated_environment(installed.raw, {
            'PHONE_WORKER_TETO_RESAMPLER_COMMAND': 'python ' + shlex.quote(str(installed.engine)),
        })
        installed.env.write_bytes(raw)
    before = installed.env.read_bytes()
    with pytest.raises(migration.MigrationError, match='ELF|nativo'):
        _migrate(migration, installed)
    assert installed.env.read_bytes() == before
    assert installed.probes == []


@pytest.mark.parametrize('returncode,stdout', [(1, b'straycat-rs 1.1.0'), (0, b'other engine 1.1.0')])
def test_native_executable_must_confirm_its_straycat_identity(migration, installed, monkeypatch, returncode, stdout):
    monkeypatch.setattr(migration.subprocess, 'run', lambda command, **kwargs:
                        subprocess.CompletedProcess(command, returncode, stdout, b''))
    with pytest.raises(migration.MigrationError, match='confirmou'):
        _migrate(migration, installed)
    assert installed.env.read_bytes() == installed.raw


@pytest.mark.parametrize('read_number', [1, 2])
def test_configuration_cas_preserves_concurrent_edit_at_each_publication_check(
        migration, installed, monkeypatch, read_number):
    current = installed.raw + b'USER_SETTING=concurrently-edited\n'
    original_read = migration.regular_bytes
    reads = 0

    def raced_read(path, maximum):
        nonlocal reads
        if Path(path) == installed.env:
            reads += 1
            if reads == read_number:
                installed.env.write_bytes(current)
        return original_read(path, maximum)

    monkeypatch.setattr(migration, 'regular_bytes', raced_read)
    with pytest.raises(migration.MigrationError, match='mudou'):
        migration.publish_environment(installed.env, installed.raw, installed.raw + b'NEW_SETTING=1\n')
    assert installed.env.read_bytes() == current
    assert not list(installed.env.parent.glob('.ttv-straycat-*'))
    backups = list(installed.env.parent.glob('phone.env.bak-straycat-*'))
    assert len(backups) == (read_number - 1)
    if backups:
        assert backups[0].read_bytes() == installed.raw
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


class _Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


def _health(voices, **changes):
    rows = [{'id': voice, 'ready': True, 'native_resampler_verified': True,
             'length_mode': 'post-consonant'} for voice in voices]
    health = {'version': '1.11.49', 'tts_agent': {'teto': {'ttv_voices': rows}}}
    health.update(changes)
    return health


def _mock_worker(migration, monkeypatch, health, *, response_headers=None, audio=None):
    requests, handlers = [], []

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            if urlsplit(request.full_url).path == '/tts-agent/health':
                return _Response(json.dumps(health).encode())
            body = json.loads(request.data)
            headers = {'X-Core-Worker-Selected-Engine': 'teto',
                       'X-Core-Worker-TTV-Voice-Id': body['ttv_voice_id'],
                       'X-Core-Worker-TTV-Language': 'pt-BR'}
            if response_headers is not None:
                headers = response_headers
            return _Response(audio if audio is not None else b'RIFF' + b'\0' * 96, headers)

    def build_opener(*arguments):
        handlers.extend(arguments)
        return Opener()

    monkeypatch.setattr(migration.urllib.request, 'build_opener', build_opener)
    return requests, handlers


def test_warmup_uses_authenticated_loopback_only_and_explicit_ttv_engine(migration, installed, monkeypatch, capsys):
    voices = ['hatsune-miku', 'anna-nyui', 'pururu-purpura']
    requests, handlers = _mock_worker(migration, monkeypatch, _health(voices))
    _, values = migration.read_environment(installed.env)
    state = installed.env.parent / 'state'
    state.mkdir()
    (state / 'runtime-status.json').write_text(json.dumps({
        'control_plane_alive': True, 'http_port': 9876, 'host': 'https://external.invalid',
    }))
    values['PHONE_WORKER_STATE_DIR'] = str(state)
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    result = migration.warmup(values, voices)
    assert [row['voice_id'] for row in result] == voices
    assert all(row['ok'] for row in result)
    assert len(requests) == 4
    assert any(isinstance(handler, migration.urllib.request.ProxyHandler) and handler.proxies == {}
               for handler in handlers)
    assert any(isinstance(handler, migration.NoRedirect) for handler in handlers)
    for request, _ in requests:
        assert urlsplit(request.full_url).scheme == 'http'
        assert urlsplit(request.full_url).hostname == '127.0.0.1'
        assert urlsplit(request.full_url).port == 9876
        assert request.get_header('Authorization') == 'Bearer phone-token-DO-NOT-PRINT'
        assert 'phone-token-DO-NOT-PRINT' not in request.full_url
        if request.data:
            body = json.loads(request.data)
            assert body['engine'] == body['preferred_engine'] == 'teto'
            assert body['ttv_voice_id'] in voices
            assert body['ttv_language'] == 'pt-BR'
            assert body['ttv_preview'] is True
            assert body['cache_mode'] == 'prefer'
            assert not any(key.startswith('fallback') for key in body)
    captured = capsys.readouterr()
    assert 'phone-token-DO-NOT-PRINT' not in captured.out + captured.err


@pytest.mark.parametrize('field,value', [
    ('ready', False), ('native_resampler_verified', False),
    ('length_mode', 'legacy'), ('id', 'kasane-teto'),
])
def test_warmup_requires_ready_verified_native_health_before_synthesis(migration, monkeypatch, field, value):
    voice = 'pururu-purpura'
    health = _health([voice])
    health['tts_agent']['teto']['ttv_voices'][0][field] = value
    requests, _ = _mock_worker(migration, monkeypatch, health)
    with pytest.raises(migration.MigrationError, match='Straycat nativo'):
        migration.warmup({'PHONE_WORKER_PORT': '8766'}, [voice])
    assert len(requests) == 1


def test_warmup_rejects_a_stale_live_worker_even_with_a_new_runtime_on_disk(migration, monkeypatch):
    requests, _ = _mock_worker(migration, monkeypatch, _health(['anna-nyui'], version='1.11.47'))
    with pytest.raises(migration.MigrationError, match='1.11.49'):
        migration.warmup({'PHONE_WORKER_PORT': '8766'}, ['anna-nyui'])
    assert len(requests) == 1


@pytest.mark.parametrize('headers', [
    {},
    {'X-Core-Worker-Selected-Engine': 'teto',
     'X-Core-Worker-TTV-Voice-Id': 'kasane-teto', 'X-Core-Worker-TTV-Language': 'pt-BR'},
    {'X-Core-Worker-Selected-Engine': 'teto',
     'X-Core-Worker-TTV-Voice-Id': 'anna-nyui', 'X-Core-Worker-TTV-Language': 'en-US'},
    {'X-Core-Worker-Selected-Engine': 'edge',
     'X-Core-Worker-TTV-Voice-Id': 'anna-nyui', 'X-Core-Worker-TTV-Language': 'pt-BR'},
    {'X-Core-Worker-TTV-Voice-Id': 'anna-nyui', 'X-Core-Worker-TTV-Language': 'pt-BR'},
])
def test_warmup_rejects_fallback_or_audio_without_requested_voice_and_portuguese_headers(
        migration, monkeypatch, headers):
    _mock_worker(migration, monkeypatch, _health(['anna-nyui']), response_headers=headers)
    with pytest.raises(migration.MigrationError, match='amostra'):
        migration.warmup({'PHONE_WORKER_PORT': '8766'}, ['anna-nyui'])


def test_warmup_rejects_empty_audio(migration, monkeypatch):
    _mock_worker(migration, monkeypatch, _health(['anna-nyui']), audio=b'')
    with pytest.raises(migration.MigrationError, match='amostra'):
        migration.warmup({'PHONE_WORKER_PORT': '8766'}, ['anna-nyui'])


def test_warmup_cannot_follow_a_redirect(migration):
    with pytest.raises(migration.MigrationError, match='redirecionar'):
        migration.NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://external.invalid')


def test_cli_restarts_then_warms_teto_and_enabled_voices_by_default(migration, installed, monkeypatch, capsys):
    calls = []
    process = {'pid': 123, 'configuration_verified': True}

    def restart(values, runtime, diagnostics):
        calls.append(('restart', values, runtime))
        assert values['_configuration_path'] == str(installed.env)
        assert values['PHONE_WORKER_MIKU_RESAMPLER_COMMAND'] == shlex.join([str(installed.engine)])
        return process

    def warmup(values, voices, **kwargs):
        calls.append(('warmup', values, voices))
        assert kwargs['expected_process'] is process
        assert kwargs['engine_sha'] == hashlib.sha256(installed.engine.read_bytes()).hexdigest()
        assert installed.env.read_bytes() != installed.raw
        return [{'voice_id': voice, 'ok': True} for voice in voices]

    monkeypatch.setattr(migration, 'restart_worker', restart)
    monkeypatch.setattr(migration, 'warmup', warmup)
    assert migration.main(['--env', str(installed.env), '--runtime-dir', str(installed.runtime)]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report['ok'] and report['phone_synthesis_verified']
    assert [call[0] for call in calls] == ['restart', 'warmup']
    assert report['voices'] == ['hatsune-miku', 'anna-nyui', 'pururu-purpura']
    assert calls[1][2] == ['kasane-teto', 'hatsune-miku', 'anna-nyui', 'pururu-purpura']
    assert 'phone-token-DO-NOT-PRINT' not in captured.out + captured.err


@pytest.mark.parametrize('option', ['--dry-run', '--no-restart'])
def test_cli_dry_run_or_no_restart_does_not_restart_or_claim_live_synthesis(
        migration, installed, monkeypatch, capsys, option):
    def unexpected_warmup(*args, **kwargs):
        pytest.fail('worker warmup must not run for ' + option)

    monkeypatch.setattr(migration, 'warmup', unexpected_warmup)
    assert migration.main(['--env', str(installed.env), '--runtime-dir', str(installed.runtime), option]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report['phone_synthesis_verified']
    if option == '--dry-run':
        assert installed.env.read_bytes() == installed.raw


def test_cli_redacts_auth_token_from_warmup_errors(migration, installed, monkeypatch, capsys):
    def failed_warmup(values, voices, **kwargs):
        raise ValueError('worker refused Authorization: Bearer ' + values['PHONE_WORKER_TOKEN'])

    monkeypatch.setattr(migration, 'restart_worker', lambda *args: {'pid': 123})
    monkeypatch.setattr(migration, 'warmup', failed_warmup)
    assert migration.main(['--env', str(installed.env), '--runtime-dir', str(installed.runtime)]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not report['ok']
    assert '[oculto]' in report['error']
    assert 'phone-token-DO-NOT-PRINT' not in captured.out + captured.err
