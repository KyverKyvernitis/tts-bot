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

MINIMUM_WORKER = (1, 11, 48)
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


def updated_environment(original, updates):
    lines, seen = [], set()
    for line in original.decode('utf-8').splitlines(keepends=True):
        match = KEY.match(line)
        key = match[1] if match else None
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


def installed_runtime(values, explicit=None):
    candidates = [explicit] if explicit else [
        Path.home() / '.core-worker-runtime/current',
        Path(values.get('PHONE_WORKER_DIR') or Path.home() / 'phone-worker'),
    ]
    for directory in candidates:
        path = Path(directory) / 'phone_worker.py'
        if not path.is_file():
            continue
        source = regular_bytes(path, 2 * 1024 * 1024).decode('utf-8')
        version = re.search(r'PHONE_WORKER_VERSION\s*=\s*[\'\"](\d+)\.(\d+)\.(\d+)', source)
        if version and tuple(map(int, version.groups())) >= MINIMUM_WORKER:
            renderer = Path(directory) / 'teto_renderer/renderer.py'
            if renderer.is_file() and '_native_source_lease' in regular_bytes(renderer, 2 * 1024 * 1024).decode('utf-8'):
                return str(directory)
    raise MigrationError('aplique o patch do bot e aguarde o worker 1.11.48 antes desta migração')


def publish_environment(path, original, updated):
    if original == updated:
        return None
    if regular_bytes(path, MAX_ENV) != original:
        raise MigrationError('a configuração mudou durante a preparação; execute novamente')
    backup = path.with_name(path.name + '.bak-straycat-' + str(time.time_ns()))
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
        runtime = installed_runtime(values, runtime_dir)
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
                updates['PHONE_WORKER_' + prefix + '_' + suffix] = values.get('PHONE_WORKER_TETO_' + suffix) or default
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise MigrationError('a consulta local tentou redirecionar para outro endereço')


def warmup(values, voices, *, timeout=124):
    port = int(values.get('PHONE_WORKER_PORT') or 8766)
    state_root = Path(values.get('PHONE_WORKER_STATE_DIR') or Path.home() / '.local/state/core-worker-phone-worker')
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
    if version < MINIMUM_WORKER:
        raise MigrationError('o processo ativo ainda não é o worker 1.11.48; aguarde a atualização')
    rows = {row['id']: row for row in ((health.get('tts_agent') or {}).get('teto') or {}).get('ttv_voices', [])}
    results = []
    for voice in voices:
        row = rows.get(voice, {})
        if not row.get('ready') or not row.get('native_resampler_verified') or row.get('length_mode') != 'post-consonant':
            raise MigrationError(f'{voice} ainda não confirmou o Straycat nativo pronto no worker')
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
                    response.headers.get('X-Core-Worker-TTV-Language') != 'pt-BR'):
                raise MigrationError(f'o worker não confirmou a amostra de {voice}')
        results.append({'voice_id': voice, 'ok': True, 'seconds': round(time.monotonic() - started, 3)})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=Path(os.getenv('PHONE_WORKER_ENV') or Path.home() / '.phone-worker.env'))
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-restart', action='store_true')
    parser.add_argument('--no-warmup', action='store_true')
    options = parser.parse_args(argv)
    report = {'ok': False, 'configuration_changed': False}
    values = {}
    try:
        report, values = migrate(options.env, dry_run=options.dry_run, runtime_dir=options.runtime_dir)
        if not options.dry_run and not options.no_restart:
            worker = Path(values.get('PHONE_WORKER_DIR') or Path.home() / 'phone-worker')
            start = worker / 'start-phone-worker.sh'
            if not start.is_file():
                raise MigrationError('configuração salva; o launcher do worker não foi encontrado para reiniciar')
            print('Reiniciando o worker com o Straycat...', file=sys.stderr, flush=True)
            environment = dict(os.environ)
            environment['PHONE_WORKER_ENV'] = str(options.env.expanduser().absolute())
            restarted = subprocess.run(['bash', str(start), '--restart'], env=environment, timeout=90, check=False)
            if restarted.returncode:
                raise MigrationError('configuração salva; o worker não confirmou o reinício')
        if not options.dry_run and not options.no_warmup and not options.no_restart:
            report['warmup'] = warmup(values, ['kasane-teto', *report['voices']])
        report['phone_synthesis_verified'] = bool(report.get('warmup'))
    except (OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as error:
        report['ok'] = False
        detail = str(error)
        for key in ('PHONE_WORKER_TOKEN', 'CORE_WORKER_TOKEN'):
            secret = values.get(key) or os.getenv(key)
            if secret:
                detail = detail.replace(secret, '[oculto]')
        report['error'] = type(error).__name__ + ': ' + detail[:400]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
