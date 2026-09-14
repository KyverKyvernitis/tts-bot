#!/usr/bin/env bash
# Módulo carregado por atualizar.sh; não executar diretamente.
# Estado runtime persistido da execução.

write_update_runtime_state() {
  local phase="${1:-Atualizando}"
  if (( LOCAL_CANDIDATE_MODE == 0 && ROLLBACK_CONTROL_MODE == 0 && REMOTE_CANDIDATE_MODE == 0 )); then
    return 0
  fi
  mkdir -p "$(dirname "$UPDATE_RUNTIME_STATE_FILE")" 2>/dev/null || return 0
  UPDATE_PHASE="$phase" \
  UPDATE_RUNTIME_RUN_ID_VALUE="$UPDATE_RUNTIME_RUN_ID" \
  UPDATE_RUNTIME_STATE_FILE_VALUE="$UPDATE_RUNTIME_STATE_FILE" \
  UPDATE_ID_VALUE="${LOCAL_CANDIDATE_DISPLAY_ID:-${ROLLBACK_REQUEST_ID:-${SHORT_TO:-atualização}}}" \
  UPDATE_RESTART_EXPECTED="$BOT_CHANGED" \
  python3 - <<'PYUPDATESTATE' 2>/dev/null || true
import datetime, json, os, pathlib, time
path = pathlib.Path(os.environ['UPDATE_RUNTIME_STATE_FILE_VALUE'])
payload = {
    'active': True,
    'run_id': os.environ.get('UPDATE_RUNTIME_RUN_ID_VALUE') or '',
    'update_id': os.environ.get('UPDATE_ID_VALUE') or 'atualização',
    'phase': (os.environ.get('UPDATE_PHASE') or 'Atualizando')[:200],
    'restart_expected': os.environ.get('UPDATE_RESTART_EXPECTED') == '1',
    'heartbeat_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'heartbeat_epoch': time.time(),
    'pid': os.getppid(),
}
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYUPDATESTATE
  chown ubuntu:ubuntu "$UPDATE_RUNTIME_STATE_FILE" 2>/dev/null || true
  chmod 0644 "$UPDATE_RUNTIME_STATE_FILE" 2>/dev/null || true
}

