#!/usr/bin/env python3
"""One-time private configuration for automatic CD publication."""
from __future__ import annotations
import argparse
import getpass
import json
import os
from pathlib import Path
import shlex
from urllib.parse import urlsplit
from cd_publish import (CONFIG_PATH, PublishError, load_configuration,
                        save_configuration, publication_status)
from osaka_cd.transport import TransportError, normalize_publish_url


def discover_worker_endpoint(env_file=None):
    """Read only a literal URL from the existing pairing; never source a shell."""
    values = {key: os.getenv(key) for key in ('CORE_WORKER_VPS_URL', 'CORE_WORKER_BASE_URL')}
    path = Path(env_file or os.getenv('PHONE_WORKER_ENV') or Path.home() / '.phone-worker.env').expanduser()
    if not any(values.values()) and path.is_file() and path.stat().st_size <= 1024 * 1024:
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                words = shlex.split(line, comments=True)
            except ValueError:
                continue
            if words[:1] == ['export']:
                words = words[1:]
            if len(words) != 1 or '=' not in words[0]:
                continue
            key, value = words[0].split('=', 1)
            if key in values and not values[key] and not any(c in value for c in ('$','`')):
                values[key] = value
    for value in values.values():
        if not value:
            continue
        try:
            transport = 'tailscale' if urlsplit(value).scheme == 'http' else 'https'
            url = normalize_publish_url(value, transport)
            return {'publish_url': url, 'transport': transport, 'vps_host': urlsplit(url).hostname,
                    'source': 'existing_worker_pairing'}
        except (TransportError, ValueError):
            continue
    raise PublishError('worker_vps_endpoint_not_discovered')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='URL da VPS; aceita base ou rota /api/osaka/cd/publish')
    parser.add_argument('--transport', choices=('https', 'tailscale'), help='HTTP privado exige tailscale explícito')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    parser.add_argument('--discover', action='store_true', help='Mostrar endereço já pareado em ~/.phone-worker.env, sem credenciais')
    parser.add_argument('--show', action='store_true', help='Mostrar somente configuração sem token')
    parser.add_argument('--check', action='store_true', help='Verificar autenticação e catálogo da VPS')
    args = parser.parse_args()
    try:
        if args.discover:
            result = {'ok': True, **discover_worker_endpoint()}
        elif args.show or args.check:
            configuration = load_configuration(args.config)
            if not configuration:
                raise PublishError('publish_not_configured')
            result = {'ok': True, 'configured': True, 'publish_url': configuration['publish_url'],
                      'transport': configuration.get('transport', 'https'),
                      'config_path': str(args.config)}
            if args.check:
                status = publication_status(configuration['publish_url'], configuration['token'],
                                            transport=configuration.get('transport', 'https'))
                result.update(server_available=True, generation=status.get('generation'),
                              songs=len(status.get('versions', status.get('song_versions', {}))))
        else:
            if args.url:
                url, transport = args.url, args.transport or 'https'
            else:
                try:
                    discovered = discover_worker_endpoint()
                except PublishError:
                    discovered = None
                if discovered:
                    url = discovered['publish_url']
                    transport = args.transport or discovered['transport']
                    print(json.dumps({'detected_url': url, 'transport': transport}, ensure_ascii=False))
                else:
                    url = input('Endereço da VPS (VPN Tailscale ou HTTPS): ').strip()
                    transport = args.transport or 'https'
            try:
                url = normalize_publish_url(url, transport)
            except TransportError as exc:
                raise PublishError(str(exc)) from None
            token = getpass.getpass('Token de publicação (entrada oculta): ')
            result = {'ok': True, **save_configuration(url, token, args.config, transport=transport)}
            result['next_command'] = 'python deploy/ttv-cd/import-midi.py --watch'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PublishError, OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, PublishError) else type(exc).__name__}, ensure_ascii=False))
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
