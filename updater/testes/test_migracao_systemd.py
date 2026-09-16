from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / 'updater/sistema/instalar.sh'
NEW_UNITS = ('bot-updater.service', 'bot-updater.timer', 'bot-updater.path', 'bot-updater-alert@.service')
OLD_UNITS = ('tts-bot-updater.service', 'tts-bot-updater.timer', 'tts-bot-updater.path', 'tts-bot-alert@.service')

SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
state_path = Path(os.environ['MOCK_STATE'])
data = json.loads(state_path.read_text())
data['calls'].append(args)
state_path.write_text(json.dumps(data))
units = data['units']
command = args[0]
unit = args[-1]
root = Path(os.environ['SYSTEMD_DIR'])
if os.environ.get('MOCK_FAIL') == ' '.join(args):
    raise SystemExit(5)
if command == 'is-enabled':
    raise SystemExit(0 if units.get(unit, {}).get('enabled') else 1)
if command == 'is-active':
    raise SystemExit(0 if units.get(unit, {}).get('active') else 3)
if command == 'list-units':
    raise SystemExit(0)
if command == 'show':
    prop = args[args.index('-p') + 1]
    if prop == 'LoadState': print('loaded' if (root / unit).is_file() else 'not-found')
    elif prop == 'FragmentPath': print(root / unit)
    elif prop == 'ActiveState': print(units.get(unit, {}).get('state', 'active' if units.get(unit, {}).get('active') else 'inactive'))
    raise SystemExit(0)
for unit in [arg for arg in args[1:] if not arg.startswith('-')]:
    record = units.setdefault(unit, {})
    if command in ('enable', 'disable'): record['enabled'] = command == 'enable'
    if command == 'start' or command == 'enable' and '--now' in args: record['active'] = True
    if command == 'stop' or command == 'disable' and '--now' in args: record['active'] = False
state_path.write_text(json.dumps(data))
'''


def installation(tmp_path: Path, *, timer=True, path=True, migrated=False):
    repo = tmp_path / 'repo'
    shutil.copytree(ROOT / 'updater/sistema', repo / 'updater/sistema')
    shutil.copytree(ROOT / 'updater/sudoers', repo / 'updater/sudoers')
    templates = repo / 'deploy/systemd/vps'
    templates.mkdir(parents=True)
    shutil.copy(ROOT / 'deploy/systemd/vps/tts-bot.service', templates)
    systemd = tmp_path / 'etc/systemd'
    sudoers = tmp_path / 'etc/sudoers'
    systemd.mkdir(parents=True)
    sudoers.mkdir()
    # A VPS ainda possui a família antiga. Os arquivos de referência não são
    # dependências do produto e continuam no fixture após a Wave 47b.
    for old, new in zip(OLD_UNITS, NEW_UNITS):
        content = (ROOT / 'updater/sistema' / new).read_text()
        content = content.replace('bot-updater.service', 'tts-bot-updater.service')
        (systemd / old).write_text(content)
    (sudoers / 'tts-bot-updater-start').write_text('# permissão antiga\n')
    (systemd / 'tts-bot.service').write_text('[Unit]\nOnFailure=tts-bot-alert@%n.service\n')
    if migrated:
        (systemd / 'tts-bot.service').write_text('[Unit]\nOnFailure=bot-updater-alert@%n.service\n')
        for unit in NEW_UNITS:
            shutil.copy(ROOT / 'updater/sistema' / unit, systemd / unit)
        shutil.copy(ROOT / 'updater/sudoers/bot-updater-start', sudoers)
        receipt = repo / 'data/updater/systemd-migration.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'ready': True, 'service': 'bot-updater.service'}))
    prefix = 'bot-updater' if migrated else 'tts-bot-updater'
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'calls': [], 'units': {
        f'{prefix}.timer': {'enabled': timer, 'active': timer},
        f'{prefix}.path': {'enabled': path, 'active': path},
        f'{prefix}.service': {'state': 'activating'},
    }}))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, script in {'systemctl': SYSTEMCTL, 'sudo': '#!/bin/sh\nexit 1\n',
                         'id': '#!/bin/sh\necho 0\n'}.items():
        file = bin_dir / name
        file.write_text(script)
        file.chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
           'REPO_DIR': str(repo), 'SYSTEMD_DIR': str(systemd), 'SUDOERS_DIR': str(sudoers),
           'MOCK_STATE': str(state), 'BACKUP_ROOT': str(tmp_path / 'backups'),
           'STATUS_FILE': str(tmp_path / 'status.json'),
           'JOURNALD_POLICY_SRC': str(tmp_path / 'absent-journal'),
           'JOURNALD_POLICY_DEST': str(tmp_path / 'journal'),
           'TMPFILES_POLICY_SRC': str(tmp_path / 'absent-tmpfiles'),
           'TMPFILES_POLICY_DEST': str(tmp_path / 'tmpfiles'), 'TMPDIR': str(tmp_path)}
    return repo, systemd, sudoers, state, env


def run_install(env, *args):
    return subprocess.run(['bash', str(Path(env['REPO_DIR']) / 'updater/sistema/instalar.sh'),
                           '--from-updater', *args], env=env, text=True, capture_output=True, timeout=30)


def assert_no_service_stop(calls):
    for args in calls:
        if args[0] in {'stop', 'restart'} or args[0] == 'disable' and '--now' in args:
            assert 'tts-bot-updater.service' not in args
            assert 'bot-updater.service' not in args


def test_new_service_waits_for_shared_lock_without_a_path_restart_loop(tmp_path):
    source = (ROOT / 'updater/core/atualizar.sh').read_text()
    start = source.index('mkdir -p "$(dirname "$UPDATER_LOCK_FILE")"')
    # A Wave 48 move a aquisição para antes da fotografia dos módulos.
    end = source.index('UPDATE_RUNTIME_RUN_ID=', start)
    block = source[start:end]
    if 'UPDATER_RUNTIME_BASE=' in block:
        block = block[:block.index('  UPDATER_RUNTIME_BASE=')]
    lock_path = tmp_path / 'shared.lock'
    env = {**os.environ, 'UPDATER_LOCK_FILE': str(lock_path), 'TTS_BOT_UPDATER_WAIT_FOR_LOCK': '1'}
    with lock_path.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process = subprocess.Popen(['bash', '-euc', block + '\nprintf acquired'], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert stdout == 'acquired'
    service = (ROOT / 'updater/sistema/bot-updater.service').read_text()
    assert 'Environment=TTS_BOT_UPDATER_WAIT_FOR_LOCK=1' in service
    assert 'TimeoutStartSec=infinity' in service


@pytest.mark.parametrize('timer,path', [(True, True), (False, False), (False, True), (True, False)])
def test_migration_preserves_independent_maintenance_states(tmp_path, timer, path):
    repo, systemd, sudoers, state, env = installation(tmp_path, timer=timer, path=path, migrated=True)
    result = run_install(env)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(state.read_text())
    for suffix, enabled in [('timer', timer), ('path', path)]:
        assert data['units'][f'bot-updater.{suffix}']['enabled'] is enabled
        assert data['units'][f'bot-updater.{suffix}']['active'] is enabled
        assert data['units'].get(f'tts-bot-updater.{suffix}', {}).get('active', False) is False
    assert all((systemd / unit).is_file() for unit in NEW_UNITS)
    assert all(not (systemd / unit).exists() for unit in OLD_UNITS)
    assert not (sudoers / 'tts-bot-updater-start').exists()
    assert 'OnFailure=bot-updater-alert@%n.service' in (systemd / 'tts-bot.service').read_text()
    assert json.loads((repo / 'data/updater/systemd-migration.json').read_text())['ready'] is True
    assert_no_service_stop(data['calls'])


def test_removal_refuses_to_skip_wave47a(tmp_path):
    repo, systemd, _, state, env = installation(tmp_path)
    result = run_install(env)
    assert result.returncode != 0
    assert 'migração 47a concluída' in result.stdout
    assert all((systemd / unit).exists() for unit in OLD_UNITS)
    assert not (systemd / 'bot-updater.service').exists()
    assert json.loads(state.read_text())['calls'] == []


def test_failed_migration_restores_old_files_and_dispatch(tmp_path):
    repo, systemd, sudoers, state, env = installation(tmp_path, migrated=True)
    original = (systemd / 'tts-bot.service').read_bytes()
    env['MOCK_FAIL'] = 'disable tts-bot-alert@.service'
    result = run_install(env)
    assert result.returncode != 0
    assert (systemd / 'tts-bot.service').read_bytes() == original
    assert all((systemd / name).is_file() for name in (*OLD_UNITS, *NEW_UNITS))
    assert (sudoers / 'tts-bot-updater-start').is_file()
    assert (sudoers / 'bot-updater-start').is_file()
    data = json.loads(state.read_text())
    assert data['units']['bot-updater.timer']['active'] is True
    assert data['units']['bot-updater.path']['active'] is True
    assert_no_service_stop(data['calls'])


def test_invalid_sudoers_aborts_before_any_installation(tmp_path):
    repo, systemd, _, state, env = installation(tmp_path)
    (repo / 'updater/sudoers/bot-updater-start').write_text('invalid syntax @@@\n')
    result = run_install(env)
    assert result.returncode != 0
    assert not (systemd / 'bot-updater.service').exists()
    assert json.loads(state.read_text())['calls'] == []


@pytest.mark.parametrize('legacy_state', ['active', 'activating', 'reloading', 'deactivating'])
def test_removal_never_interrupts_running_legacy_service(tmp_path, legacy_state):
    _, systemd, _, state, env = installation(tmp_path, migrated=True)
    data = json.loads(state.read_text())
    data['units']['tts-bot-updater.service'] = {'state': legacy_state}
    state.write_text(json.dumps(data))
    result = run_install(env)
    assert result.returncode != 0
    assert 'serviço legado ainda está em execução' in result.stdout
    assert all((systemd / unit).exists() for unit in OLD_UNITS)
    assert all(call[0] in {'is-active', 'is-enabled', 'show', 'list-units'} for call in json.loads(state.read_text())['calls'])


def test_removal_detects_old_onfailure_in_live_dropin(tmp_path):
    _, systemd, _, _, env = installation(tmp_path, migrated=True)
    dropin = systemd / 'tts-bot.service.d/custom.conf'
    dropin.parent.mkdir()
    dropin.write_text('[Unit]\nOnFailure=tts-bot-alert@%n.service\n')
    result = run_install(env)
    assert result.returncode != 0
    assert 'referência legada ainda ativa' in result.stderr
    assert all((systemd / unit).exists() for unit in OLD_UNITS)


def test_removal_is_idempotent_after_legacy_files_are_gone(tmp_path):
    _, systemd, sudoers, state, env = installation(tmp_path, migrated=True)
    first = run_install(env)
    assert first.returncode == 0, first.stdout + first.stderr
    second = run_install(env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert all(not (systemd / unit).exists() for unit in OLD_UNITS)
    assert not (sudoers / 'tts-bot-updater-start').exists()
    assert_no_service_stop(json.loads(state.read_text())['calls'])


def test_dry_run_has_no_service_or_file_mutations(tmp_path):
    repo, systemd, _, state, env = installation(tmp_path, migrated=True)
    result = run_install(env, '--dry-run')
    assert result.returncode == 0, result.stdout + result.stderr
    assert (systemd / 'bot-updater.service').exists()
    assert all((systemd / unit).exists() for unit in OLD_UNITS)
    assert all(call[0] in {'is-active', 'is-enabled', 'show', 'list-units'} for call in json.loads(state.read_text())['calls'])
