"""Activate a reversible Pururu profile without changing banks or other voices."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_straycat_migration import migration, installed, _health, _mock_worker
from test_straycat_configuration_attestation import live_worker


@pytest.fixture
def profile_install(migration, installed):
    migration.migrate(installed.env, runtime_dir=installed.runtime)
    (installed.runtime / 'phone_worker.py').write_text('PHONE_WORKER_VERSION = "1.11.51"\n')
    return SimpleNamespace(**vars(installed), before=installed.env.read_bytes())


def activate(migration, install, **kwargs):
    return migration.migrate_pururu_profile(install.env, runtime_dir=install.runtime, **kwargs)


def other_lines(migration, raw):
    return [line for line in raw.splitlines(keepends=True)
            if not (match := migration.KEY.match(line.decode())) or match[1] not in migration.PURURU_PROFILE_KEYS]


def test_activation_changes_only_seven_profile_keys_and_has_private_exact_backup(migration, profile_install):
    report, previous = activate(migration, profile_install)
    updated, values = migration.read_environment(profile_install.env)
    assert report['mode'] == 'pururu_profile' and report['profile_action'] == 'activate'
    assert report['voices'] == ['pururu-purpura'] and report['voice_profile'] == migration.PURURU_PROFILE
    assert report['previous_voice_profile'] == 'legacy'
    assert values['PHONE_WORKER_PURURU_PROFILE'] == migration.PURURU_PROFILE
    for suffix, value in migration.PURURU_PROFILE_DEFAULTS.items():
        assert values['PHONE_WORKER_PURURU_' + suffix] == value
    assert other_lines(migration, updated) == other_lines(migration, profile_install.before)
    backup = Path(report['configuration_backup'])
    assert backup.read_bytes() == profile_install.before
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(profile_install.env.stat().st_mode) == 0o600
    assert not report['bank_downloaded'] and not report['bank_files_changed']
    assert report['other_voice_settings_preserved'] and report['personal_settings_preserved']
    assert all(Path(path).read_bytes() == raw for path, raw in profile_install.bank_contents.items())
    assert 'phone-token-DO-NOT-PRINT' not in json.dumps(report)


def test_rerun_preserves_active_profile_custom_overrides_and_needs_no_new_backup(migration, profile_install):
    activate(migration, profile_install)
    raw, _ = migration.read_environment(profile_install.env)
    edited = migration.updated_environment(raw, {'PHONE_WORKER_PURURU_FLAGS': '',
        'PHONE_WORKER_PURURU_BASE_PITCH': 'D4', 'PHONE_WORKER_PURURU_MODULATION': '7'})
    profile_install.env.write_bytes(edited)
    report, _ = activate(migration, profile_install)
    assert not report['configuration_changed'] and report['configuration_backup'] is None
    assert profile_install.env.read_bytes() == edited


def test_active_profile_missing_key_gets_own_default_not_teto_default(migration, profile_install):
    activate(migration, profile_install)
    raw, _ = migration.read_environment(profile_install.env)
    profile_install.env.write_bytes(migration.updated_environment(raw, {}, remove_keys={'PHONE_WORKER_PURURU_FLAGS'}))
    migration.migrate(profile_install.env, runtime_dir=profile_install.runtime)
    _, values = migration.read_environment(profile_install.env)
    assert values['PHONE_WORKER_PURURU_FLAGS'] == 'g5 B50'
    assert values['PHONE_WORKER_PURURU_TEMPO'] == '125'


def test_future_shared_migration_preserves_all_active_pururu_acoustic_choices(migration, profile_install):
    activate(migration, profile_install)
    raw, _ = migration.read_environment(profile_install.env)
    updates = {'PHONE_WORKER_PURURU_' + suffix: ('custom ' + suffix if suffix == 'FLAGS' else str(number + 1))
               for number, suffix in enumerate(migration.PURURU_PROFILE_DEFAULTS)}
    profile_install.env.write_bytes(migration.updated_environment(raw, updates))
    migration.migrate(profile_install.env, runtime_dir=profile_install.runtime)
    _, values = migration.read_environment(profile_install.env)
    assert all(values[key] == value for key, value in updates.items())


def test_dry_run_is_byte_preserving_and_profile_requires_new_worker(migration, installed):
    migration.migrate(installed.env, runtime_dir=installed.runtime)
    before = installed.env.read_bytes()
    with pytest.raises(migration.MigrationError, match='1.11.51'):
        activate(migration, installed, dry_run=True)
    assert installed.env.read_bytes() == before
    (installed.runtime / 'phone_worker.py').write_text('PHONE_WORKER_VERSION = "1.11.51"\n')
    report, _ = activate(migration, installed, dry_run=True)
    assert report['dry_run'] and report['configuration_changed']
    assert report['configuration_backup'] is None and installed.env.read_bytes() == before


@pytest.mark.parametrize('updates', [
    {'PHONE_WORKER_PURURU_ENABLED': 'false'},
    {'PHONE_WORKER_PURURU_VOICEBANK_DIR': '/missing/pururu-bank'},
    {'PHONE_WORKER_PURURU_RESAMPLER_COMMAND': 'python /legacy/resampler.py'},
    {'PHONE_WORKER_PURURU_NATIVE_RESAMPLER_SHA256': '0' * 64},
    {'PHONE_WORKER_PURURU_PROFILE': 'unknown-profile'},
])
def test_invalid_profile_prerequisites_never_change_config(migration, profile_install, updates):
    raw, _ = migration.read_environment(profile_install.env)
    profile_install.env.write_bytes(migration.updated_environment(raw, updates))
    before = profile_install.env.read_bytes()
    with pytest.raises(migration.MigrationError):
        activate(migration, profile_install)
    assert profile_install.env.read_bytes() == before


def test_restore_recovers_profile_values_and_absence_preserving_unrelated_current_changes(migration, profile_install):
    raw, _ = migration.read_environment(profile_install.env)
    profile_install.env.write_bytes(migration.updated_environment(raw, {}, remove_keys={
        'PHONE_WORKER_PURURU_TEMPO', 'PHONE_WORKER_PURURU_MODULATION'}))
    _, before_values = migration.read_environment(profile_install.env)
    activated, _ = activate(migration, profile_install)
    raw, _ = migration.read_environment(profile_install.env)
    later_updates = {'PHONE_WORKER_TOKEN': 'new-private-token', 'PHONE_WORKER_MIKU_FLAGS': 'g12 B60',
                     'PHONE_WORKER_PURURU_RENDER_THREADS': '4'}
    profile_install.env.write_bytes(migration.updated_environment(raw, later_updates))
    changed_before_restore = profile_install.env.read_bytes()
    report, _ = activate(migration, profile_install, restore_backup=activated['configuration_backup'])
    restored, values = migration.read_environment(profile_install.env)
    assert report['profile_action'] == 'restore' and report['voice_profile'] == 'legacy'
    assert {key: values.get(key) for key in migration.PURURU_PROFILE_KEYS} == {
        key: before_values.get(key) for key in migration.PURURU_PROFILE_KEYS}
    assert all(values[key] == value for key, value in later_updates.items())
    assert other_lines(migration, restored) == other_lines(migration, changed_before_restore)
    assert Path(report['configuration_backup']).read_bytes() == changed_before_restore
    second, _ = activate(migration, profile_install, restore_backup=activated['configuration_backup'])
    assert not second['configuration_changed'] and second['configuration_backup'] is None


def test_restore_dry_run_and_invalid_backup_are_non_mutating(migration, profile_install, tmp_path):
    report, _ = activate(migration, profile_install)
    before = profile_install.env.read_bytes()
    dry, _ = activate(migration, profile_install, restore_backup=report['configuration_backup'], dry_run=True)
    assert dry['configuration_changed'] and profile_install.env.read_bytes() == before
    foreign = tmp_path / 'foreign.env'
    foreign.write_text('PHONE_WORKER_TOKEN="do-not-print"\n')
    with pytest.raises(migration.MigrationError, match='não contém'):
        activate(migration, profile_install, restore_backup=foreign)
    assert profile_install.env.read_bytes() == before


def test_restore_can_recover_an_invalid_current_marker(migration, profile_install):
    activated, _ = activate(migration, profile_install)
    raw, _ = migration.read_environment(profile_install.env)
    profile_install.env.write_bytes(migration.updated_environment(
        raw, {'PHONE_WORKER_PURURU_PROFILE': 'mistyped-profile'}))
    report, _ = activate(migration, profile_install, restore_backup=activated['configuration_backup'])
    assert report['previous_voice_profile'] == 'mistyped-profile'
    assert report['voice_profile'] == 'legacy'
    _, values = migration.read_environment(profile_install.env)
    assert 'PHONE_WORKER_PURURU_PROFILE' not in values


def test_activation_uses_existing_compare_and_swap_windows(migration, profile_install, monkeypatch):
    original = migration.publish_environment

    def raced(path, before, after, **kwargs):
        path.write_bytes(before + b'PHONE_WORKER_MIKU_TEMPO=99\n')
        return original(path, before, after, **kwargs)

    monkeypatch.setattr(migration, 'publish_environment', raced)
    with pytest.raises(migration.MigrationError, match='mudou'):
        activate(migration, profile_install)
    assert profile_install.env.read_bytes().endswith(b'PHONE_WORKER_MIKU_TEMPO=99\n')


def test_profile_warmup_checks_status_and_audio_profile_header(migration, monkeypatch):
    voice = 'pururu-purpura'
    health = _health([voice], version='1.11.51')
    health['tts_agent']['teto']['ttv_voices'][0]['voice_profile'] = migration.PURURU_PROFILE
    headers = {'X-Core-Worker-Selected-Engine': 'teto', 'X-Core-Worker-TTV-Voice-Id': voice,
               'X-Core-Worker-TTV-Language': 'pt-BR', 'X-Core-Worker-TTV-Voice-Profile': migration.PURURU_PROFILE}
    requests, _ = _mock_worker(migration, monkeypatch, health, response_headers=headers)
    diagnostics = {}
    rows = migration.warmup({'PHONE_WORKER_PURURU_PROFILE': migration.PURURU_PROFILE}, [voice],
        diagnostics=diagnostics, minimum_worker=(1, 11, 51))
    assert rows[0]['voice_profile'] == migration.PURURU_PROFILE
    assert diagnostics['voices'][0]['voice_profile'] == migration.PURURU_PROFILE
    assert len(requests) == 2
    assert json.loads(requests[1][0].data)['timeout_seconds'] == 120


@pytest.mark.parametrize('where', ['status', 'audio'])
def test_wrong_profile_never_passes_verification(migration, monkeypatch, where):
    voice = 'pururu-purpura'
    health = _health([voice], version='1.11.51')
    health['tts_agent']['teto']['ttv_voices'][0]['voice_profile'] = (
        'legacy' if where == 'status' else migration.PURURU_PROFILE)
    requests, _ = _mock_worker(migration, monkeypatch, health)
    with pytest.raises(migration.MigrationError):
        migration.warmup({'PHONE_WORKER_PURURU_PROFILE': migration.PURURU_PROFILE}, [voice],
            minimum_worker=(1, 11, 51))
    assert len(requests) == (1 if where == 'status' else 2)


def test_cli_profile_warms_only_pururu_after_saved_env_is_verified(migration, profile_install, monkeypatch, capsys):
    calls = []

    def restart(values, runtime, diagnostics):
        calls.append('restart')
        assert values['PHONE_WORKER_PURURU_PROFILE'] == migration.PURURU_PROFILE
        assert values['_configuration_path'] == str(profile_install.env)
        return {'pid': 123}

    def warmup(values, voices, **kwargs):
        calls.append('warmup')
        assert voices == ['pururu-purpura']
        assert kwargs['minimum_worker'] == (1, 11, 51)
        assert kwargs['expected_voice_profiles'] == {'pururu-purpura': migration.PURURU_PROFILE}
        return [{'voice_id': 'pururu-purpura', 'voice_profile': migration.PURURU_PROFILE, 'ok': True}]

    monkeypatch.setattr(migration, 'restart_worker', restart)
    monkeypatch.setattr(migration, 'warmup', warmup)
    assert migration.main(['--env', str(profile_install.env), '--runtime-dir', str(profile_install.runtime),
                           '--pururu-profile', migration.PURURU_PROFILE]) == 0
    report = json.loads(capsys.readouterr().out)
    assert calls == ['restart', 'warmup']
    assert report['phone_synthesis_verified'] and report['voices'] == ['pururu-purpura']


@pytest.mark.parametrize('option', ['--dry-run', '--no-restart'])
def test_cli_profile_skips_live_claims_without_restart(migration, profile_install, monkeypatch, capsys, option):
    monkeypatch.setattr(migration, 'restart_worker', Mock(side_effect=AssertionError('unexpected restart')))
    monkeypatch.setattr(migration, 'warmup', Mock(side_effect=AssertionError('unexpected warmup')))
    assert migration.main(['--env', str(profile_install.env), '--runtime-dir', str(profile_install.runtime),
                           '--pururu-profile', migration.PURURU_PROFILE, option]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report['phone_synthesis_verified']


def test_new_attestation_covers_profile_but_legacy_attestation_remains_compatible(migration):
    values = {'PHONE_WORKER_PURURU_FLAGS': 'g5 B50'}
    first = migration.renderer_configuration_digest(values)
    marked = {**values, 'PHONE_WORKER_PURURU_PROFILE': migration.PURURU_PROFILE}
    assert migration.renderer_configuration_digest(marked) != first
    process = {'pid': 123, 'started_at': 456}
    old = {'pid': 123, 'process_started_at': 456, 'version': '1.11.50',
           'renderer_configuration_sha256': migration.renderer_configuration_digest(values, include_profiles=False)}
    migration.confirm_configuration(old, values, process)


def test_removed_settings_must_not_accept_a_worker_with_stale_exported_values(migration, live_worker):
    values, runtime, _state, _child, _payload = live_worker
    expected = dict(values)
    expected.pop('PHONE_WORKER_MIKU_FLAGS')
    result = migration.process_snapshot(expected, runtime)
    assert result['identity_verified']
    assert not result['configuration_verified']
    assert 'PHONE_WORKER_MIKU_FLAGS' in result['configuration_mismatches']


def test_wrapper_defaults_to_activation_and_accepts_restore(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wrapper = tmp_path / 'usar-perfil-pururu.sh'
    wrapper.write_bytes((root / 'deploy/pururu-profile/termux/usar-perfil-pururu.sh').read_bytes())
    helper = tmp_path / 'configurar-straycat.py'
    helper.write_text('import json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    activated = subprocess.run(['bash', str(wrapper), '--dry-run'], capture_output=True, text=True, check=True)
    assert json.loads(activated.stdout) == ['--pururu-profile', 'pururu-ptbr-v1', '--dry-run']
    restored = subprocess.run(['bash', str(wrapper), '--restore-pururu-profile', '/a/backup'],
                              capture_output=True, text=True, check=True)
    assert json.loads(restored.stdout) == ['--restore-pururu-profile', '/a/backup']
