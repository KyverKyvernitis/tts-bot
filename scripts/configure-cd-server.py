#!/usr/bin/env python3
"""Prepare private publication configuration and a reviewable systemd drop-in."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osaka_cd.transport import normalize_publish_url


_DISCOVERY_KEYS = frozenset({
    'PORT', 'CORE_WORKER_PUBLIC_BASE_URL', 'CORE_WORKER_VPS_URL',
    'VPS_PUBLIC_BASE_URL',
})


def _readonly_command(arguments):
    """Keep discovery bounded and do not expose command output on errors."""
    return subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, timeout=2, check=False)


def _selected_environment(raw):
    selected = {}
    for entry in raw.split(b'\x00'):
        key, separator, value = entry.partition(b'=')
        name = key.decode('ascii', errors='ignore')
        if separator and name in _DISCOVERY_KEYS:
            selected[name] = value.decode('utf-8', errors='replace')
    return selected


def _service_environment(service):
    """Read only routing keys from the running bot, never its credentials."""
    try:
        result = _readonly_command(['systemctl', 'show', '--property=MainPID', '--value', service])
        pid = (result.stdout or '').strip()
        if result.returncode or not pid.isascii() or not pid.isdigit() or int(pid) <= 1:
            return {}
        with (Path('/proc') / pid / 'environ').open('rb') as environment:
            return _selected_environment(environment.read(1024 * 1024))
    except (OSError, subprocess.SubprocessError):
        return {}


def _tailnet_addresses():
    try:
        result = _readonly_command(['tailscale', 'status', '--json'])
        if result.returncode:
            raise ValueError('tailscale_status_unavailable')
        status = json.loads(result.stdout or '{}')
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError('tailscale_status_unavailable') from None
    if not isinstance(status, dict) or status.get('BackendState') != 'Running':
        raise ValueError('tailscale_not_running')
    own = status.get('Self')
    if not isinstance(own, dict) or own.get('Online') is False:
        raise ValueError('tailscale_not_online')
    addresses = []
    candidates = own.get('TailscaleIPs', [])
    if not isinstance(candidates, list):
        raise ValueError('tailscale_address_unavailable')
    for address in candidates:
        if not isinstance(address, str):
            continue
        host = '[' + address + ']' if ':' in address else address
        try:
            url = normalize_publish_url('http://' + host + ':10000', transport='tailscale')
        except ValueError:
            continue
        addresses.append(urlsplit(url).hostname)
    if not addresses:
        raise ValueError('tailscale_address_unavailable')
    return addresses


def discover_endpoint(service='tts-bot.service', environment=None, port=None):
    """Discover this VPS's active VPN address and the existing bot's listener."""
    own_addresses = _tailnet_addresses()
    process_environment = os.environ if environment is None else environment
    shell = {key: process_environment[key] for key in _DISCOVERY_KEYS if key in process_environment}
    running = _service_environment(service)
    configured = None
    address = next((item for item in own_addresses if ':' not in item), own_addresses[0])
    for source in (running, shell):
        for key in ('CORE_WORKER_VPS_URL', 'CORE_WORKER_PUBLIC_BASE_URL', 'VPS_PUBLIC_BASE_URL'):
            value = source.get(key, '')
            if not value:
                continue
            try:
                candidate = urlsplit(normalize_publish_url(value, transport='tailscale'))
            except ValueError:
                continue
            if candidate.scheme == 'http' and candidate.hostname in own_addresses:
                configured = candidate
                address = candidate.hostname
                break
        if configured is not None:
            break
    if port is not None:
        selected_port, port_source = port, 'argument'
    elif running.get('PORT'):
        selected_port, port_source = running['PORT'], 'running_service_PORT'
    elif shell.get('PORT'):
        selected_port, port_source = shell['PORT'], 'environment_PORT'
    elif configured is not None and configured.port is not None:
        selected_port, port_source = configured.port, 'existing_worker_url'
    else:
        selected_port, port_source = 10000, 'repository_default'
    value = str(selected_port)
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('invalid_bot_port')
    host = '[' + address + ']' if ':' in address else address
    url = normalize_publish_url(f'http://{host}:{int(value)}', transport='tailscale')
    return {'publish_url': url, 'transport': 'tailscale',
            'address_source': 'running_tailscale_self', 'port_source': port_source,
            'reachability_checked': False}


def private_write(path, data, mode=0o600):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('configuration_symlink_refused')
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure(url, directory, data_root, client_path=None, *, transport='https'):
    url = normalize_publish_url(url, transport=transport)
    directory = Path(directory).expanduser().absolute()
    data_root = Path(data_root).expanduser().absolute()
    for path in (directory, data_root):
        if any(char in str(path) for char in ('\n', '\r', '\x00', '"', '\\', '%')):
            raise ValueError('invalid_configuration_path')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment_file = directory / 'server.env'
    token = ''
    if environment_file.exists():
        if environment_file.is_symlink() or environment_file.stat().st_mode & 0o077:
            raise ValueError('server_environment_requires_private_regular_file')
        for line in environment_file.read_text().splitlines():
            if line.startswith('OSAKA_CD_PUBLISH_TOKEN='):
                token = line.split('=', 1)[1]
    if not token:
        token = secrets.token_urlsafe(48)
    if not 32 <= len(token) <= 4096 or any(not (c.isalnum() or c in '-_') for c in token):
        raise ValueError('existing_token_invalid')
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_write(environment_file,
                  'OSAKA_CD_PUBLISH_ENABLED=1\n'
                  f'OSAKA_CD_DATA_ROOT="{data_root}"\n'
                  f'OSAKA_CD_PUBLISH_TOKEN={token}\n')
    dropin = directory / '60-cd-publication.conf'
    private_write(dropin, '[Service]\n' + f'EnvironmentFile="{environment_file}"\n')
    exported = None
    if client_path:
        exported = str(Path(client_path).expanduser().absolute())
        private_write(exported, json.dumps({'version': 1, 'publish_url': url, 'transport': transport,
                                           'token': token}, indent=2) + '\n')
    return {'ok': True, 'publish_url': url, 'transport': transport, 'data_root': str(data_root),
            'environment_file': str(environment_file), 'systemd_dropin': str(dropin),
            'client_config': exported, 'token_printed': False,
            'service_restarted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='Endereço do bot; sem esta opção, usa a VPN Tailscale existente')
    parser.add_argument('--transport', choices=('https', 'tailscale'),
                        help='HTTP privado exige tailscale; HTTPS continua disponível')
    parser.add_argument('--port', type=int, help='Porta do bot na descoberta automática')
    parser.add_argument('--service', default='tts-bot.service',
                        help='Serviço usado para descobrir o PORT do bot em execução')
    parser.add_argument('--config-dir', type=Path, default=Path.home() / '.config/osaka-cd')
    parser.add_argument('--data-root', type=Path, default=Path.home() / 'osaka-data/cd')
    parser.add_argument('--client-config', type=Path, default=Path.home() / 'osaka-cd-client.json')
    args = parser.parse_args()
    try:
        discovery = None
        if args.url:
            if args.port is not None:
                raise ValueError('port_only_available_for_auto_discovery')
            url, transport = args.url, args.transport or 'https'
        else:
            if args.transport == 'https':
                raise ValueError('url_required_for_https')
            discovery = discover_endpoint(args.service, port=args.port)
            url, transport = discovery['publish_url'], discovery['transport']
        result = configure(url, args.config_dir, args.data_root, args.client_config,
                           transport=transport)
        if discovery is not None:
            result['discovery'] = {key: value for key, value in discovery.items()
                                   if key not in ('publish_url', 'transport')}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == '__main__':
    sys.exit(main())
