from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from updater.testes.fonte_core import caminho_fonte_core
from updater.testes.test_migracao_systemd import installation, run_install

ROOT = Path(__file__).resolve().parents[2]


def bootstrap_fixture(tmp_path):
    source = tmp_path / 'checkout com espacos/core'
    source.mkdir(parents=True)
    shutil.copy(ROOT / 'updater/core/atualizar.sh', source)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    marker = tmp_path / 'marker'
    (source / 'finalizacao.sh').write_text('printf original > "$SNAPSHOT_MARKER"\n')
    (source / 'configuracao.sh').write_text('''
printf 'printf mixed > "$SNAPSHOT_MARKER"\\n' > "$ORIGINAL_CORE/finalizacao.sh"
. "$UPDATER_SOURCE_DIR/finalizacao.sh"
exit 0
''')
    env = {**os.environ, 'TTS_BOT_UPDATER_SOURCE_DIR': str(source),
           'TTS_BOT_UPDATER_RUNTIME_DIR': str(runtime),
           'DISCORD_AUTO_UPDATE_LOCK_FILE': str(tmp_path / 'shared.lock'),
           'ORIGINAL_CORE': str(source), 'SNAPSHOT_MARKER': str(marker)}
    env.pop('TTS_BOT_UPDATER_RUNNING_COPY', None)
    return source, runtime, marker, env


def test_runtime_finalization_uses_same_revision_even_after_checkout_changes(tmp_path):
    source, runtime, marker, env = bootstrap_fixture(tmp_path)
    result = subprocess.run(['bash', str(source / 'atualizar.sh')], env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == 'original'
    assert 'mixed' in (source / 'finalizacao.sh').read_text()
    assert list(runtime.iterdir()) == []


def test_running_legacy_lock_prevents_snapshot_and_second_execution(tmp_path):
    source, runtime, marker, env = bootstrap_fixture(tmp_path)
    with open(env['DISCORD_AUTO_UPDATE_LOCK_FILE'], 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(['bash', str(source / 'atualizar.sh')], env=env,
                                text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert list(runtime.iterdir()) == []


def test_incomplete_overlay_is_rejected_before_changing_live_units(tmp_path):
    repo, systemd, _, state, env = installation(tmp_path, migrated=True)
    overlay = tmp_path / 'overlay'
    overlay.mkdir()
    for name in ('bot-updater.service', 'bot-updater.timer', 'bot-updater-alert@.service'):
        shutil.copy(repo / 'updater/sistema' / name, overlay)
    before = (systemd / 'bot-updater.path').read_bytes()
    env['TEMPLATE_DIR'] = str(overlay)
    result = run_install(env)
    assert result.returncode != 0
    assert (systemd / 'bot-updater.path').read_bytes() == before
    assert 'bot-updater.path' in result.stderr


@pytest.mark.parametrize('path', [
    'updater/sistema/bot-updater.service', 'updater/sistema/bot-updater.timer',
    'updater/sistema/bot-updater.path', 'updater/sistema/bot-updater-alert@.service',
    'updater/sistema/instalar.sh', 'updater/sudoers/bot-updater-start',
])
def test_infrastructure_changes_do_not_request_worker_or_dashboard_builds(path):
    script = '''
source <(awk '/^classify_changed_files[(][)]/{f=1} /^fast_reload_modules_for_changed_files[(][)]/{f=0} f' "$CORE_SOURCE")
classify_changed_files
printf '%s %s %s %s %s %s %s\\n' "$VPS_SYSTEMD_UNITS_CHANGED" "$FRONT_CHANGED" "$BACK_CHANGED" "$PHONE_WORKER_SYNC_REQUIRED" "$CORE_WORKER_AUTOMATION_REQUIRED" "$AUDIO_SYSTEMD_CHANGED" "$REQUIREMENTS_CHANGED"
'''
    result = subprocess.run(['bash', '-euc', script], capture_output=True, text=True,
                            env={**os.environ, 'CORE_SOURCE': str(caminho_fonte_core()), 'CHANGED_FILES_RAW': path})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '1 0 0 0 0 0 0'


def test_music_agent_source_change_requests_termux_release_without_legacy_sync():
    script = '''
source <(awk '/^classify_changed_files[(][)]/{f=1} /^fast_reload_modules_for_changed_files[(][)]/{f=0} f' "$CORE_SOURCE")
classify_changed_files
printf '%s %s %s\\n' "$BOT_CHANGED" "$PHONE_WORKER_SYNC_REQUIRED" "$CORE_WORKER_AUTOMATION_REQUIRED"
'''
    result = subprocess.run(['bash', '-euc', script], capture_output=True, text=True,
                            env={**os.environ, 'CORE_SOURCE': str(caminho_fonte_core()),
                                 'CHANGED_FILES_RAW': 'cogs/musica/runtime_telefone/agente/servidor.py'})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '1 0 1'


def test_final_layout_has_only_canonical_infrastructure_and_helpers():
    assert {p.name for p in (ROOT / 'updater/sistema').iterdir() if p.is_file()} == {
        'README.md', 'instalar.sh', 'bot-updater.service', 'bot-updater.timer',
        'bot-updater.path', 'bot-updater-alert@.service',
    }
    assert {p.name for p in (ROOT / 'updater/sudoers').iterdir()} == {'bot-updater-start'}
    assert not list((ROOT / 'tests').glob('test_update_*.py'))
    for name in ('snapshot_git.py', 'smoke_runtime.py'):
        assert not (ROOT / 'updater/utilitarios' / name).exists()
    for directory in ('updater/core', 'updater/discord'):
        for path in (ROOT / directory).iterdir():
            if path.suffix not in ('.py', '.sh'):
                continue
            source = path.read_text()
            for legacy in ('tts-bot-updater.service', 'tts-bot-updater.timer', 'tts-bot-updater.path',
                           'tts-bot-alert@', '/snapshot_git.py', '/smoke_runtime.py',
                           'scripts/install-vps-systemd-units.sh'):
                assert legacy not in source, (path, legacy)


def test_production_python_base_prefers_system_interpreter():
    source = (ROOT / 'updater/core/candidato.sh').read_text()
    block = source[source.index('python_runtime_base_python() {'):source.index('\npython_uv_tool_root() {')]
    result = subprocess.run(['bash', '-euc', block + '\npython_runtime_base_python'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    expected = '/usr/bin/python3' if os.access('/usr/bin/python3', os.X_OK) else shutil.which('python3')
    assert result.stdout.strip() == expected
