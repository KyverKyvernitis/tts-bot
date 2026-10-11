"""CLI mínimo do updater: avisa via API local e registra término durável."""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.request


def send(phase: str, run_id: str) -> bool:
    root = pathlib.Path(os.environ.get('REPO_DIR') or '/home/ubuntu/bot')
    health_url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').rstrip('/')
    url = health_url.removesuffix('/health') + '/internal/update/voice-notices'
    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('BOT_INTERNAL_UPDATE_TOKEN', '').strip()
    if not token:
        try:
            for line in (root / '.env').read_text(encoding='utf-8').splitlines():
                if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                    token = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    if token:
        headers['X-Update-Token'] = token
    try:
        request = urllib.request.Request(url, method='POST', headers=headers,
            data=json.dumps({'phase': phase, 'run_id': run_id}).encode())
        with urllib.request.urlopen(request, timeout=3.0 if phase == 'start' else 1.5) as response:
            return bool(json.load(response).get('ok'))
    except (OSError, ValueError, TimeoutError):
        return False


def main() -> int:
    phase, run_id = sys.argv[1:3]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', run_id) or phase not in ('start', 'success', 'failure'):
        return 2
    if phase == 'start':
        sent = send('start', run_id)
        print('avisos_servidores_start=' + ('ok' if sent else 'indisponivel'))
        return 0
    root = pathlib.Path(os.environ.get('REPO_DIR') or '/home/ubuntu/bot') / 'data' / 'runtime' / 'update-voice-notices'
    state = root / (run_id + '.json')
    if not state.is_file():
        return 0
    destination = root / (run_id + '.result.json')
    tmp = root / ('.' + run_id + '.result.' + str(os.getpid()) + '.tmp')
    try:
        tmp.write_text(json.dumps({'result': phase, 'at': time.time()}), encoding='utf-8')
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)
    send('finish', run_id)  # O reconciliador recupera o resultado se o bot reiniciou.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
