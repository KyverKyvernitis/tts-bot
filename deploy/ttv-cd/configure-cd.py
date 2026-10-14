#!/usr/bin/env python3
"""One-time private configuration for automatic CD publication."""
from __future__ import annotations
import argparse
import getpass
import json
from pathlib import Path
from cd_publish import (CONFIG_PATH, PublishError, load_configuration,
                        save_configuration, publication_status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='HTTPS público da VPS; aceita domínio ou rota /api/osaka/cd/publish')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    parser.add_argument('--show', action='store_true', help='Mostrar somente configuração sem token')
    parser.add_argument('--check', action='store_true', help='Verificar autenticação e catálogo da VPS')
    args = parser.parse_args()
    try:
        if args.show or args.check:
            configuration = load_configuration(args.config)
            if not configuration:
                raise PublishError('publish_not_configured')
            result = {'ok': True, 'configured': True, 'publish_url': configuration['publish_url'],
                      'config_path': str(args.config)}
            if args.check:
                status = publication_status(configuration['publish_url'], configuration['token'])
                result.update(server_available=True, generation=status.get('generation'),
                              songs=len(status.get('versions', status.get('song_versions', {}))))
        else:
            url = args.url or input('Endereço HTTPS da VPS: ').strip()
            if not url.rstrip('/').endswith('/api/osaka/cd/publish'):
                url = url.rstrip('/') + '/api/osaka/cd/publish'
            token = getpass.getpass('Token de publicação (entrada oculta): ')
            result = {'ok': True, **save_configuration(url, token, args.config)}
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
