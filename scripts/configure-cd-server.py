#!/usr/bin/env python3
"""Prepare private publication configuration and a reviewable systemd drop-in."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from urllib.parse import urlsplit


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


def configure(url, directory, data_root, client_path=None):
    url = url.strip().rstrip('/')
    if not url.endswith('/api/osaka/cd/publish'):
        url += '/api/osaka/cd/publish'
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != '/api/osaka/cd/publish'
            or any(char.isspace() for char in url)):
        raise ValueError('public_https_url_required')
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
        private_write(exported, json.dumps({'version': 1, 'publish_url': url, 'token': token}, indent=2) + '\n')
    return {'ok': True, 'publish_url': url, 'data_root': str(data_root),
            'environment_file': str(environment_file), 'systemd_dropin': str(dropin),
            'client_config': exported, 'token_printed': False,
            'service_restarted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='Endereço HTTPS público do bot')
    parser.add_argument('--config-dir', type=Path, default=Path.home() / '.config/osaka-cd')
    parser.add_argument('--data-root', type=Path, default=Path.home() / 'osaka-data/cd')
    parser.add_argument('--client-config', type=Path, default=Path.home() / 'osaka-cd-client.json')
    args = parser.parse_args()
    try:
        result = configure(args.url or input('Endereço HTTPS público do bot: '),
                           args.config_dir, args.data_root, args.client_config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == '__main__':
    sys.exit(main())
