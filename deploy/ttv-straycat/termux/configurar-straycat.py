#!/usr/bin/env python3
"""Reuse the phone's working native Teto engine for its installed TTV voices."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MINIMUM_WORKER = (1, 11, 49)
PURURU_PROFILE_MINIMUM_WORKER = (1, 11, 51)
PURURU_PROFILE = 'pururu-ptbr-v1'
PURURU_PROFILE_DEFAULTS = {'FLAGS': 'g5 B50', 'MODULATION': '0', 'BASE_PITCH': 'C4',
                           'SPEECH_RATE': '1.0', 'VELOCITY': '100', 'TEMPO': '125'}
PURURU_PROFILE_KEYS = frozenset({'PHONE_WORKER_PURURU_PROFILE', *(
    'PHONE_WORKER_PURURU_' + suffix for suffix in PURURU_PROFILE_DEFAULTS)})
MAX_ENV = 256 * 1024
VOICES = {
    'MIKU': ('hatsune-miku', 'hatsune-miku-english'),
    'ANNA': ('anna-nyui', 'anna-nyui-diva'),
    'PURURU': ('pururu-purpura', 'pururu-purpura'),
}
BASELINE = {'BASE_PITCH': 'C4', 'SPEECH_RATE': '1.0', 'VELOCITY': '100',
            'MODULATION': '15', 'TEMPO': '140', 'FLAGS': '', 'RENDER_THREADS': '2',
            'FRAGMENT_CACHE_MB': '256', 'PCM_CACHE_MB': '256',
            'NATIVE_ANALYSIS_CACHE_MB': '256'}
KEY = re.compile(r'^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=')


class MigrationError(ValueError):
    pass


def regular_bytes(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= maximum:
            raise MigrationError('arquivo ausente, inválido ou grande demais')
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        if len(data) != before.st_size or (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
            raise MigrationError('arquivo mudou durante a leitura')
    return data


def read_environment(path):
    raw = regular_bytes(path, MAX_ENV)
    values = {}
    for line in raw.decode('utf-8').splitlines():
        match = KEY.match(line)
        if not match:
            continue
        key = match[1]
        if not key.startswith('PHONE_WORKER_'):
            continue
        try:
            parts = shlex.split(line[match.end():], comments=True)
        except ValueError as error:
            raise MigrationError(f'valor inválido em {key}') from error
        value = parts[0] if parts else ''
        if any(character in value for character in '\x00\r\n'):
            raise MigrationError(f'valor inválido em {key}')
        values[key] = value
    return raw, values


def literal(value):
    return json.dumps(str(value), ensure_ascii=False).replace('$', '\\$').replace('`', '\\`')


def updated_environment(original, updates, *, remove_keys=()):
    lines, seen = [], set()
    for line in original.decode('utf-8').splitlines(keepends=True):
        match = KEY.match(line)
        key = match[1] if match else None
        if key in remove_keys:
            continue
        if key in updates:
            if key not in seen:
                lines.append(key + '=' + literal(updates[key]) + '\n')
                seen.add(key)
        else:
            lines.append(line)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    lines.extend(key + '=' + literal(value) + '\n' for key, value in updates.items() if key not in seen)
    result = ''.join(lines).encode('utf-8')
    if len(result) > MAX_ENV:
        raise MigrationError('configuração excede o limite permitido')
    return result


def baseline_engine(values):
    raw = values.get('PHONE_WORKER_TETO_RESAMPLER_COMMAND') or str(Path.home() / 'bin/straycat-rs')
    try:
        command = shlex.split(raw)
    except ValueError as error:
        raise MigrationError('comando da Teto inválido') from error
    if len(command) != 1:
        raise MigrationError('a migração exige o Straycat nativo usado diretamente pela Teto')
    name = command[0].replace('${HOME}', str(Path.home())).replace('$HOME', str(Path.home()))
    engine = Path(shutil.which(name) or name).expanduser().resolve(strict=True)
    data = regular_bytes(engine, 64 * 1024 * 1024)
    machines = {'aarch64': 183, 'arm64': 183, 'x86_64': 62, 'amd64': 62,
                'armv7l': 40, 'armv8l': 40, 'i386': 3, 'i686': 3}
    if (len(data) < 64 or data[:4] != b'\x7fELF' or data[4] not in (1, 2) or data[5] != 1
            or struct.unpack_from('<H', data, 18)[0] != machines.get(platform.machine().lower())
            or not os.access(engine, os.X_OK)):
        raise MigrationError('o motor da Teto precisa ser um ELF nativo executável deste aparelho')
    probe = subprocess.run([str(engine), '--version'], capture_output=True, timeout=5, check=False)
    version = (probe.stdout + probe.stderr).decode('utf-8', errors='replace').strip()
    if probe.returncode or 'straycat' not in version.lower() or len(version) > 1024:
        raise MigrationError('o executável da Teto não confirmou que é o Straycat')
    return engine, hashlib.sha256(data).hexdigest(), version


def installed_runtime(values, explicit=None, *, minimum=MINIMUM_WORKER):
    candidates = [explicit] if explicit else [
        phone_path(values.get('PHONE_WORKER_RUNTIME_ROOT') or Path.home() / '.core-worker-runtime') / 'current',
        phone_path(values.get('PHONE_WORKER_DIR') or Path.home() / 'phone-worker'),
    ]
    for directory in candidates:
        path = Path(directory) / 'phone_worker.py'
        if not path.is_file():
            continue
        source = regular_bytes(path, 2 * 1024 * 1024).decode('utf-8')
        version = re.search(r'PHONE_WORKER_VERSION\s*=\s*[\'\"](\d+)\.(\d+)\.(\d+)', source)
        if version and tuple(map(int, version.groups())) >= minimum:
            renderer = Path(directory) / 'teto_renderer/renderer.py'
            if renderer.is_file() and '_native_source_lease' in regular_bytes(renderer, 2 * 1024 * 1024).decode('utf-8'):
                return str(directory)
    wanted = '.'.join(map(str, minimum))
    raise MigrationError(f'aplique o patch do bot e aguarde o worker {wanted} antes desta migração')


def publish_environment(path, original, updated, *, backup_label='straycat'):
    if original == updated:
        return None
    if regular_bytes(path, MAX_ENV) != original:
        raise MigrationError('a configuração mudou durante a preparação; execute novamente')
    backup = path.with_name(path.name + '.bak-' + backup_label + '-' + str(time.time_ns()))
    with backup.open('xb') as target:
        os.chmod(backup, 0o600)
        target.write(original)
    descriptor, name = tempfile.mkstemp(prefix='.ttv-straycat-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(updated)
            target.flush()
            os.fsync(target.fileno())
        if regular_bytes(path, MAX_ENV) != original:
            raise MigrationError('a configuração mudou antes de salvar; arquivo atual preservado')
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
    return str(backup)


def migrate(path, *, dry_run=False, runtime_dir=None):
    path = Path(path).expanduser().absolute()
    lock_path = path.with_name(path.name + '.ttv-straycat.lock')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MigrationError('já existe uma configuração do Straycat em andamento') from error
        original, values = read_environment(path)
        profile = str(values.get('PHONE_WORKER_PURURU_PROFILE') or '').strip()
        if profile not in ('', 'legacy', PURURU_PROFILE):
            raise MigrationError('perfil da Pururu desconhecido; configuração preservada')
        runtime = installed_runtime(values, runtime_dir, minimum=(
            PURURU_PROFILE_MINIMUM_WORKER if profile == PURURU_PROFILE else MINIMUM_WORKER))
        engine, digest, version = baseline_engine(values)
        if values.get('PHONE_WORKER_TETO_BACKEND', 'utau') not in ('utau', 'voicepeak', ''):
            raise MigrationError('a Teto precisa estar usando o backend UTAU/Straycat')
        updates = {'PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256': digest}
        voices = []
        for prefix, (voice_id, default_bank) in VOICES.items():
            if values.get('PHONE_WORKER_' + prefix + '_ENABLED', '').lower() not in ('true', '1', 'yes', 'on', 'sim'):
                continue
            bank = values.get('PHONE_WORKER_' + prefix + '_VOICEBANK_DIR')
            if not bank and prefix != 'PURURU':
                bank = values.get('PHONE_WORKER_' + prefix + '_ENGLISH_VOICEBANK_DIR')
            bank = str(bank or Path.home() / 'voicebanks' / default_bank)
            bank = Path(bank.replace('${HOME}', str(Path.home())).replace('$HOME', str(Path.home()))).expanduser()
            if not bank.is_dir():
                raise MigrationError(f'o banco de {voice_id} não está disponível na pasta configurada')
            updates['PHONE_WORKER_' + prefix + '_RESAMPLER_COMMAND'] = shlex.join([str(engine)])
            updates['PHONE_WORKER_' + prefix + '_NATIVE_RESAMPLER_SHA256'] = digest
            updates['PHONE_WORKER_' + prefix + '_LENGTH_MODE'] = 'post-consonant'
            for suffix, default in BASELINE.items():
                key = 'PHONE_WORKER_' + prefix + '_' + suffix
                if prefix == 'PURURU' and profile == PURURU_PROFILE and suffix in PURURU_PROFILE_DEFAULTS:
                    # An already activated profile owns its acoustic settings.
                    # Future shared-engine migrations must not replace them.
                    if key not in values:
                        updates[key] = PURURU_PROFILE_DEFAULTS[suffix]
                else:
                    updates[key] = values.get('PHONE_WORKER_TETO_' + suffix) or default
            voices.append(voice_id)
        if not voices:
            raise MigrationError('nenhuma das novas vozes está habilitada; configuração preservada')
        updated = updated_environment(original, updates)
        backup = None if dry_run else publish_environment(path, original, updated)
        return {'ok': True, 'dry_run': dry_run, 'configuration_changed': updated != original,
                'configuration_backup': backup, 'engine': str(engine), 'engine_sha256': digest,
                'engine_version': version, 'runtime': runtime, 'voices': voices,
                'bank_downloaded': False, 'bank_files_changed': False,
                'teto_settings_preserved': True, 'pururu_language': 'pt-BR'}, values
    finally:
        os.close(descriptor)


def migrate_pururu_profile(path, *, dry_run=False, runtime_dir=None, restore_backup=None):
    """Activate/restore only Pururu's seven acoustic profile keys."""
    path = Path(path).expanduser().absolute()
    lock_path = path.with_name(path.name + '.ttv-straycat.lock')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MigrationError('já existe uma configuração do Straycat em andamento') from error
        original, values = read_environment(path)
        runtime = installed_runtime(values, runtime_dir, minimum=PURURU_PROFILE_MINIMUM_WORKER)
        if values.get('PHONE_WORKER_PURURU_ENABLED', '').lower() not in ('true', '1', 'yes', 'on', 'sim'):
            raise MigrationError('a Pururu precisa estar habilitada; configuração preservada')
        bank = phone_path(values.get('PHONE_WORKER_PURURU_VOICEBANK_DIR') or
                          Path.home() / 'voicebanks/pururu-purpura')
        if not bank.is_dir():
            raise MigrationError('o banco da Pururu não está disponível na pasta configurada')
        engine, digest, version = baseline_engine(values)
        try:
            command = shlex.split(values.get('PHONE_WORKER_PURURU_RESAMPLER_COMMAND') or
                                  values.get('PHONE_WORKER_TETO_RESAMPLER_COMMAND') or str(engine))
        except ValueError as error:
            raise MigrationError('comando da Pururu inválido; configuração preservada') from error
        if len(command) != 1:
            raise MigrationError('a Pururu precisa usar diretamente o Straycat nativo já configurado')
        candidate = phone_path(shutil.which(command[0]) or command[0]).resolve(strict=True)
        pin = (values.get('PHONE_WORKER_PURURU_NATIVE_RESAMPLER_SHA256') or
               values.get('PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256') or '').lower()
        if candidate != engine or pin != digest:
            raise MigrationError('o motor e o SHA-256 da Pururu precisam confirmar o Straycat já instalado')
        previous = str(values.get('PHONE_WORKER_PURURU_PROFILE') or '').strip() or 'legacy'
        if previous not in ('legacy', PURURU_PROFILE) and restore_backup is None:
            raise MigrationError('perfil da Pururu desconhecido; configuração preservada')
        remove = set()
        if restore_backup is None:
            updates = {'PHONE_WORKER_PURURU_PROFILE': PURURU_PROFILE}
            for suffix, default in PURURU_PROFILE_DEFAULTS.items():
                key = 'PHONE_WORKER_PURURU_' + suffix
                if previous != PURURU_PROFILE or key not in values:
                    updates[key] = default
            target_profile = PURURU_PROFILE
        else:
            backup_path = Path(restore_backup).expanduser().absolute()
            if backup_path == path:
                raise MigrationError('escolha o backup anterior, não a configuração atual')
            _, prior = read_environment(backup_path)
            if not any(key.startswith('PHONE_WORKER_PURURU_') for key in prior):
                raise MigrationError('o backup não contém uma configuração da Pururu')
            target_profile = str(prior.get('PHONE_WORKER_PURURU_PROFILE') or '').strip() or 'legacy'
            if target_profile not in ('legacy', PURURU_PROFILE):
                raise MigrationError('o backup contém um perfil da Pururu desconhecido')
            updates = {key: prior[key] for key in sorted(PURURU_PROFILE_KEYS) if key in prior}
            remove = PURURU_PROFILE_KEYS.difference(updates)
        updated = updated_environment(original, updates, remove_keys=remove)
        backup = None if dry_run else publish_environment(
            path, original, updated, backup_label='pururu-profile')
        report = {'ok': True, 'dry_run': dry_run, 'mode': 'pururu_profile',
                  'profile_action': 'restore' if restore_backup is not None else 'activate',
                  'voice_profile': target_profile, 'previous_voice_profile': previous,
                  'configuration_changed': updated != original, 'configuration_backup': backup,
                  'engine': str(engine), 'engine_sha256': digest, 'engine_version': version,
                  'runtime': runtime, 'voices': ['pururu-purpura'],
                  'bank_downloaded': False, 'bank_files_changed': False,
                  'teto_settings_preserved': True, 'other_voice_settings_preserved': True,
                  'personal_settings_preserved': True, 'pururu_language': 'pt-BR'}
        if restore_backup is not None:
            report['restored_from'] = str(backup_path)
        return report, values
    finally:
        os.close(descriptor)


def phone_path(value):
    return Path(str(value).replace('${HOME}', str(Path.home())).replace('$HOME', str(Path.home()))).expanduser()


def renderer_configuration_digest(values, *, include_profiles=True):
    suffixes = {'ENABLED', 'VOICEBANK_DIR', 'ENGLISH_VOICEBANK_DIR', 'RESAMPLER_COMMAND',
                'NATIVE_RESAMPLER_SHA256', 'LENGTH_MODE', *BASELINE}
    if include_profiles:
        suffixes.add('PROFILE')
    selected = {f'PHONE_WORKER_{prefix}_{suffix}': str(values.get(f'PHONE_WORKER_{prefix}_{suffix}',
        os.getenv(f'PHONE_WORKER_{prefix}_{suffix}', '')))
        .replace('${HOME}', str(Path.home())).replace('$HOME', str(Path.home()))
        for prefix in ('TETO', *VOICES) for suffix in suffixes}
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode('utf-8')).hexdigest()


def process_snapshot(values, expected_runtime=None):
    """Read only the selected worker's identity and whitelisted renderer keys."""
    worker = phone_path(values.get('PHONE_WORKER_DIR') or Path.home() / 'phone-worker')
    state_root = phone_path(values.get('PHONE_WORKER_STATE_DIR') or Path.home() / '.local/state/core-worker-phone-worker')
    result = {'pid': None, 'identity_verified': False, 'configuration_verified': False,
              'configuration_mismatches': [], 'runtime_matches': False}
    try:
        state = json.loads(regular_bytes(state_root / 'runtime-status.json', 65536))
        pid_path = phone_path(values.get('PHONE_WORKER_PID_FILE') or worker / 'phone-worker.pid')
        pid = int(regular_bytes(pid_path, 32).strip())
        if pid <= 1 or pid != int(state.get('pid') or 0):
            return result
        result.update(pid=pid, started_at=state.get('started_at'), http_port=state.get('http_port'))
        os.kill(pid, 0)
        arguments = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\x00')
        names = [Path(value.decode('utf-8', errors='replace')).name for value in arguments if value]
        if not any(name.startswith('python') for name in names) or 'phone_worker.py' not in names:
            return result
        result['identity_verified'] = bool(state.get('runtime_kind') == 'termux' and
            state.get('source') == 'termux-phone-worker' and state.get('control_plane_alive') is True and
            1024 <= int(state.get('http_port') or 0) <= 65535 and
            0 <= time.time() - float(state.get('updated_at') or 0) <= 90)
        result['expected_runtime'] = str(Path(expected_runtime).resolve()) if expected_runtime else None
        try:
            cwd = Path(os.readlink(Path('/proc') / str(pid) / 'cwd')).resolve()
            result['runtime_matches'] = expected_runtime is None or cwd == Path(expected_runtime).resolve()
        except PermissionError:
            result['runtime_pending_health'] = True
        try:
            with (Path('/proc') / str(pid) / 'environ').open('rb') as source:
                raw = source.read(1024 * 1024 + 1)
        except PermissionError:
            result['configuration_pending_health'] = True
            return result
        if len(raw) > 1024 * 1024:
            return result
        actual = {}
        for item in raw.split(b'\x00'):
            key, separator, value = item.partition(b'=')
            if separator:
                actual[key.decode('utf-8', errors='replace')] = value.decode('utf-8', errors='replace')
        suffixes = {'ENABLED', 'VOICEBANK_DIR', 'ENGLISH_VOICEBANK_DIR', 'RESAMPLER_COMMAND',
                    'NATIVE_RESAMPLER_SHA256', 'LENGTH_MODE', 'PROFILE', *BASELINE}
        keys = {f'PHONE_WORKER_{prefix}_{suffix}' for prefix in ('TETO', *VOICES) for suffix in suffixes}
        mismatches = []
        for key in sorted(keys):
            expected = values.get(key, os.getenv(key))
            if expected is not None:
                expected = expected.replace('${HOME}', str(Path.home())).replace('$HOME', str(Path.home()))
            if actual.get(key) != expected:
                mismatches.append(key)
        result['configuration_mismatches'] = mismatches
        result['configuration_verified'] = not mismatches
    except (OSError, ValueError, TypeError) as error:
        result['inspection_error'] = type(error).__name__
    return result


def startup_lock_state(values):
    worker = phone_path(values.get('PHONE_WORKER_DIR') or Path.home() / 'phone-worker')
    directory = phone_path(values.get('PHONE_WORKER_LOCK_DIR') or worker / '.phone-worker-start.lock')
    result = {'exists': directory.exists(), 'owner_alive': False, 'owner_pid': None}
    if not result['exists']:
        return result
    try:
        pid = int(regular_bytes(directory / 'owner.pid', 32).strip())
        if pid > 1:
            os.kill(pid, 0)
            arguments = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\x00')
            if any(Path(arg.decode('utf-8', errors='replace')).name == 'start-phone-worker.sh'
                   for arg in arguments if arg):
                result.update(owner_alive=True, owner_pid=pid)
    except (OSError, ValueError):
        pass
    return result


def restart_worker(values, runtime, diagnostics, *, budget=150):
    """Wait for a live launcher, then prove the new worker loaded this env."""
    deadline = time.monotonic() + budget
    before = process_snapshot(values, runtime)
    diagnostics.update(stage='waiting_startup_lock', previous_pid=before.get('pid'), restart_attempts=0)
    worker = phone_path(values.get('PHONE_WORKER_DIR') or Path.home() / 'phone-worker')
    start = worker / 'start-phone-worker.sh'
    if not start.is_file():
        raise MigrationError('configuração salva; launcher do worker não encontrado')
    environment = dict(os.environ)
    environment['PHONE_WORKER_ENV'] = values['_configuration_path']
    attempts = 0
    next_attempt = time.monotonic()
    announced_lock = False
    while time.monotonic() < deadline:
        current = process_snapshot(values, runtime)
        if current.get('identity_verified') and (current.get('runtime_pending_health') or
                                                  current.get('configuration_pending_health')):
            try:
                configuration = local_configuration(values, current)
                confirm_configuration(configuration, values, current)
                current.update(configuration_verified=True, runtime_matches=True,
                               configuration_verified_via='authenticated_local_endpoint')
            except (OSError, ValueError, urllib.error.URLError) as error:
                diagnostics['configuration_probe_error'] = type(error).__name__
        diagnostics['process'] = current
        runtime_ok = current.get('runtime_matches')
        config_ok = current.get('configuration_verified')
        if current.get('identity_verified') and runtime_ok and config_ok:
            diagnostics.update(stage='process_verified', active_pid=current['pid'],
                               process_restarted=(current.get('pid'), current.get('started_at')) !=
                                   (before.get('pid'), before.get('started_at')))
            return current
        lock = startup_lock_state(values)
        diagnostics['startup_lock'] = lock
        if lock['owner_alive']:
            diagnostics['stage'] = 'waiting_startup_lock'
            if not announced_lock:
                print('Aguardando a inicialização que já está em andamento...', file=sys.stderr, flush=True)
                announced_lock = True
            time.sleep(min(.25, max(0, deadline - time.monotonic())))
            continue
        if attempts < 3 and time.monotonic() >= next_attempt:
            diagnostics['stage'] = 'requesting_restart'
            attempts += 1
            diagnostics['restart_attempts'] = attempts
            try:
                completed = subprocess.run(['bash', str(start), '--restart'], env=environment,
                    timeout=max(1, min(90, deadline - time.monotonic())), check=False)
                diagnostics['launcher_exit_code'] = completed.returncode
            except subprocess.TimeoutExpired:
                # The process we launched may wait on a slow companion even
                # after the official worker started. Inspect that worker next.
                diagnostics['launcher_timed_out'] = True
            next_attempt = time.monotonic() + 45
        diagnostics['stage'] = 'verifying_live_process'
        time.sleep(min(.5, max(0, deadline - time.monotonic())))
    if diagnostics.get('startup_lock', {}).get('owner_alive'):
        raise MigrationError('a inicialização anterior continua em andamento; nenhum lock vivo foi apagado')
    raise MigrationError('o processo ativo ainda não confirmou a nova configuração; consulte o diagnóstico')


def report_path():
    downloads = Path('/storage/emulated/0/Download')
    return (downloads if downloads.is_dir() else Path.home()) / 'ttv-straycat-recuperacao.json'


def save_report(report, path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.ttv-diagnostico-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write('\n')
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def redact_report(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[oculto]')
        return value
    if isinstance(value, list):
        return [redact_report(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact_report(item, secrets) for key, item in value.items()}
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise MigrationError('a consulta local tentou redirecionar para outro endereço')


def local_configuration(values, process):
    port = int(process.get('http_port') or values.get('PHONE_WORKER_PORT') or 8766)
    if not 1024 <= port <= 65535:
        raise MigrationError('porta local do worker inválida')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(f'http://127.0.0.1:{port}/tts-agent/configuration',
        headers={'Authorization': 'Bearer ' + values.get('PHONE_WORKER_TOKEN', '')})
    with opener.open(request, timeout=2) as response:
        return json.loads(response.read(65536))


def confirm_configuration(health, values, process):
    if health.get('pid') != process.get('pid') or health.get('process_started_at') != process.get('started_at'):
        raise MigrationError('o processo mudou durante a verificação; execute novamente')
    version = tuple(int(number) for number in str(health.get('version') or '1.11.51').split('.')[:3])
    if health.get('renderer_configuration_sha256') != renderer_configuration_digest(
            values, include_profiles=version >= PURURU_PROFILE_MINIMUM_WORKER):
        raise MigrationError('o processo ativo não carregou a configuração salva')
    if process.get('expected_runtime') and health.get('runtime_directory') != process['expected_runtime']:
        raise MigrationError('o processo ativo não está usando o runtime atualizado')


def warmup(values, voices, *, timeout=124, expected_process=None, engine_sha=None, diagnostics=None,
           minimum_worker=MINIMUM_WORKER, expected_voice_profiles=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics['stage'] = 'checking_voices'
    port = int(values.get('PHONE_WORKER_PORT') or 8766)
    state_root = phone_path(values.get('PHONE_WORKER_STATE_DIR') or Path.home() / '.local/state/core-worker-phone-worker')
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(regular_bytes(state_root / 'runtime-status.json', 65536))
        if state.get('control_plane_alive') and state.get('http_port'):
            port = int(state['http_port'])
    if not 1024 <= port <= 65535:
        raise MigrationError('porta local do worker inválida')
    base = f'http://127.0.0.1:{port}'
    token = values.get('PHONE_WORKER_TOKEN', '')
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(base + '/tts-agent/health', headers=headers), timeout=60) as response:
        health = json.loads(response.read(2 * 1024 * 1024))
    version = tuple(int(number) for number in str(health.get('version', '')).split('.')[:3])
    if version < minimum_worker:
        wanted = '.'.join(map(str, minimum_worker))
        raise MigrationError(f'o processo ativo ainda não é o worker {wanted}; aguarde a atualização')
    if expected_process:
        confirm_configuration(health, values, expected_process)
    rows = {row['id']: row for row in ((health.get('tts_agent') or {}).get('teto') or {}).get('ttv_voices', [])}
    diagnostics['voices'] = [{key: rows.get(voice, {}).get(key) for key in (
        'id', 'ready', 'available', 'enabled', 'native_resampler_verified', 'native_resampler_sha256',
        'resampler', 'resampler_backend', 'length_mode', 'voice_profile', 'last_error')} for voice in voices]
    for voice, row in zip(voices, diagnostics['voices']):
        row['id'] = voice
    diagnostics['resources'] = ((health.get('tts_agent') or {}).get('teto') or {}).get('resources', {})
    profiles = dict(expected_voice_profiles or {})
    if values.get('PHONE_WORKER_PURURU_PROFILE') == PURURU_PROFILE:
        profiles.setdefault('pururu-purpura', PURURU_PROFILE)
    pending = []
    for voice in voices:
        row = rows.get(voice, {})
        length = values.get('PHONE_WORKER_TETO_LENGTH_MODE') if voice == 'kasane-teto' else 'post-consonant'
        if length not in ('post-consonant', 'total'):
            length = 'post-consonant'
        if (not row.get('ready') or not row.get('native_resampler_verified') or row.get('length_mode') != length
                or (engine_sha and row.get('native_resampler_sha256') and row['native_resampler_sha256'] != engine_sha)
                or (voice in profiles and row.get('voice_profile') != profiles[voice])):
            pending.append(voice)
    if pending:
        diagnostics['stage'] = 'voices_not_ready'
        raise MigrationError('vozes ainda não confirmaram o Straycat nativo pronto: ' + ', '.join(pending))
    results = []
    for voice in voices:
        diagnostics.update(stage='warming_voice', warming_voice=voice, completed_warmup=list(results))
        body = {'engine': 'teto', 'preferred_engine': 'teto', 'text': 'Olá. Como você está?',
                'ttv_voice_id': voice, 'ttv_language': 'pt-BR', 'ttv_pitch_semitones': 0.0,
                'ttv_speech_rate': 1.0, 'ttv_preview': True, 'timeout_seconds': 120,
                'max_audio_bytes': 8 * 1024 * 1024, 'cache_mode': 'prefer'}
        started = time.monotonic()
        print(f'Preparando o cache de {voice}...', file=sys.stderr, flush=True)
        request = urllib.request.Request(base + '/tts-agent/synthesize.raw',
                                         data=json.dumps(body).encode(), headers=headers, method='POST')
        with opener.open(request, timeout=timeout) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if (not 44 < len(data) <= 8 * 1024 * 1024 or
                    response.headers.get('X-Core-Worker-Selected-Engine') != 'teto' or
                    response.headers.get('X-Core-Worker-TTV-Voice-Id') != voice or
                    response.headers.get('X-Core-Worker-TTV-Language') != 'pt-BR' or
                    (voice in profiles and response.headers.get('X-Core-Worker-TTV-Voice-Profile') != profiles[voice])):
                raise MigrationError(f'o worker não confirmou a amostra de {voice}')
        result = {'voice_id': voice, 'ok': True, 'seconds': round(time.monotonic() - started, 3)}
        if voice in profiles:
            result['voice_profile'] = profiles[voice]
        results.append(result)
    diagnostics.update(stage='verified', completed_warmup=list(results))
    diagnostics.pop('warming_voice', None)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=Path(os.getenv('PHONE_WORKER_ENV') or Path.home() / '.phone-worker.env'))
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-restart', action='store_true')
    parser.add_argument('--no-warmup', action='store_true')
    parser.add_argument('--report', type=Path, default=report_path())
    profile_options = parser.add_mutually_exclusive_group()
    profile_options.add_argument('--pururu-profile', choices=[PURURU_PROFILE])
    profile_options.add_argument('--restore-pururu-profile', type=Path, metavar='BACKUP')
    options = parser.parse_args(argv)
    report = {'ok': False, 'configuration_changed': False, 'phone_synthesis_verified': False}
    values = {}
    try:
        profile_only = bool(options.pururu_profile or options.restore_pururu_profile is not None)
        if profile_only:
            report, values = migrate_pururu_profile(options.env, dry_run=options.dry_run,
                runtime_dir=options.runtime_dir, restore_backup=options.restore_pururu_profile)
        else:
            report, values = migrate(options.env, dry_run=options.dry_run, runtime_dir=options.runtime_dir)
        report['phone_synthesis_verified'] = False
        diagnostics = report.setdefault('diagnostics', {})
        if not options.dry_run and not options.no_restart:
            _, values = read_environment(options.env)
            values['_configuration_path'] = str(options.env.expanduser().absolute())
            print('Reiniciando o worker com o Straycat...', file=sys.stderr, flush=True)
            process = restart_worker(values, report['runtime'], diagnostics)
        if not options.dry_run and not options.no_warmup and not options.no_restart:
            if profile_only:
                print('Conferindo o perfil da Pururu no processo ativo...', file=sys.stderr, flush=True)
                report['warmup'] = warmup(values, report['voices'], expected_process=process,
                    engine_sha=report['engine_sha256'], diagnostics=diagnostics,
                    minimum_worker=PURURU_PROFILE_MINIMUM_WORKER,
                    expected_voice_profiles={'pururu-purpura': report['voice_profile']})
            else:
                print('Conferindo as quatro vozes no processo ativo...', file=sys.stderr, flush=True)
                report['warmup'] = warmup(values, ['kasane-teto', *report['voices']],
                    expected_process=process, engine_sha=report['engine_sha256'], diagnostics=diagnostics)
        report['phone_synthesis_verified'] = bool(report.get('warmup'))
    except (OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as error:
        report['ok'] = False
        detail = str(error)
        for key in ('PHONE_WORKER_TOKEN', 'CORE_WORKER_TOKEN'):
            secret = values.get(key) or os.getenv(key)
            if secret:
                detail = detail.replace(secret, '[oculto]')
        report['error'] = type(error).__name__ + ': ' + detail[:400]
    report['diagnostic_report'] = str(options.report)
    report = redact_report(report, [values.get(key) or os.getenv(key) for key in
        ('PHONE_WORKER_TOKEN', 'CORE_WORKER_TOKEN')])
    try:
        save_report(report, options.report)
    except OSError:
        report['diagnostic_report_saved'] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
