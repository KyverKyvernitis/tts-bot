#!/usr/bin/env bash
# Entrega de status, progresso e apresentação Discord do updater.
# Carregado por atualizar.sh; não execute este módulo isoladamente.

send_update_status_payload() {
  # `${1:-{}}` acrescenta uma chave `}` ao argumento em Bash, produzindo JSON
  # inválido e fazendo toda edição via /internal/update/zip-status ser descartada.
  local payload_json="${1:-}"
  [[ -n "${payload_json//[[:space:]]/}" ]] || payload_json='{}'
  local final_delivery="${2:-0}"
  prepare_update_delivery_dirs || true
  local rc=0
  if UPDATE_PAYLOAD_JSON="$payload_json" FINAL_DELIVERY="$final_delivery" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" UPDATE_STATUS_OUTBOX_DIR="$UPDATE_STATUS_OUTBOX_DIR" python3 - <<'PYSENDSTATUS'
import datetime, hashlib, json, os, pathlib, time, urllib.error, urllib.request, uuid

try:
    payload = json.loads(os.environ.get('UPDATE_PAYLOAD_JSON') or '{}')
except Exception:
    raise SystemExit(2)
if not isinstance(payload, dict) or not payload.get('channel_id') or not payload.get('message_id'):
    raise SystemExit(2)

payload.setdefault('event_at', datetime.datetime.now(datetime.timezone.utc).isoformat())
delivery_seed = '\0'.join(
    str(payload.get(key) or '')
    for key in ('channel_id', 'message_id', 'candidate_id', 'status', 'title', 'event_at')
)
delivery_id = str(payload.get('delivery_id') or '').strip() or hashlib.sha256(delivery_seed.encode('utf-8')).hexdigest()[:24]
payload['delivery_id'] = delivery_id

url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/zip-status')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass

body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
final_delivery = str(os.environ.get('FINAL_DELIVERY') or '0').lower() in {'1', 'true', 'yes'}
try:
    attempts = max(1, min(10, int(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_ATTEMPTS') or (5 if final_delivery else 1))))
except (TypeError, ValueError):
    attempts = 5 if final_delivery else 1
try:
    base_delay = max(0.0, min(10.0, float(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_RETRY_DELAY_SECONDS') or 1.5)))
except (TypeError, ValueError):
    base_delay = 1.5
try:
    request_timeout = max(1.0, min(30.0, float(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_TIMEOUT_SECONDS') or 10)))
except (TypeError, ValueError):
    request_timeout = 10.0
last_error = ''
for attempt in range(attempts):
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            parsed = json.loads(response.read().decode('utf-8', errors='ignore') or '{}')
        if parsed.get('ok') and (parsed.get('delivered') or parsed.get('ignored')):
            raise SystemExit(0)
        last_error = str(parsed.get('error') or parsed or 'resposta sem confirmação')[:500]
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode('utf-8', errors='ignore')
        except Exception:
            detail = ''
        last_error = f'HTTP {exc.code}: {detail[:400]}'
    except Exception as exc:
        last_error = f'{type(exc).__name__}: {exc}'[:500]
    if attempt + 1 < attempts:
        time.sleep(min(8.0, base_delay * (2 ** attempt)))

if not final_delivery:
    raise SystemExit(0)

outbox = pathlib.Path(os.environ.get('UPDATE_STATUS_OUTBOX_DIR') or '/tmp/update-status-outbox')
try:
    outbox.mkdir(parents=True, exist_ok=True)
    safe_id = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in delivery_id)[:120]
    path = outbox / f'{safe_id}.json'
    existing = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    job = {
        'schema_version': 2,
        'delivery_id': delivery_id,
        'payload': payload,
        'created_at': existing.get('created_at') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'attempts': int(existing.get('attempts') or 0),
        'last_error': last_error or 'entrega indisponível',
        'next_attempt_at': 0,
    }
    tmp = path.with_name('.' + path.name + f'.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(tmp, path)
    os.chmod(path, 0o664)
except Exception as exc:
    print(f'falha ao persistir status final: {type(exc).__name__}: {exc}', file=os.sys.stderr)
    raise SystemExit(2)
raise SystemExit(0)
PYSENDSTATUS
  then
    rc=0
  else
    rc=$?
  fi
  chown ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" 2>/dev/null || true
  if (( rc != 0 )); then
    logger -t "$LOG_TAG" "falha ao entregar ou persistir status final do update (rc=$rc)" 2>/dev/null || true
  fi
  return "$rc"
}

flush_update_status_outbox() {
  prepare_update_delivery_dirs || true
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" UPDATE_STATUS_OUTBOX_DIR="$UPDATE_STATUS_OUTBOX_DIR" python3 - <<'PYFLUSHSTATUS'
import datetime, json, os, pathlib, time, urllib.error, urllib.request

root = pathlib.Path(os.environ.get('UPDATE_STATUS_OUTBOX_DIR') or '/tmp/update-status-outbox')
if not root.is_dir():
    raise SystemExit(0)
url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/zip-status')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass

now = time.time()

# Um processo pode morrer depois de renomear o job para .sending.*. Nesse caso,
# o glob normal não o encontra mais e a entrega ficava presa para sempre.
# Recoloque claims antigos na fila antes de buscar novos trabalhos.
for stale_claim in root.glob('.sending.*.json'):
    try:
        if now - stale_claim.stat().st_mtime < 120:
            continue
        parts = stale_claim.name.split('.', 3)
        original_name = parts[3] if len(parts) == 4 and parts[3] else f'recovered-{int(now)}.json'
        target = root / original_name
        if target.exists():
            target = root / f'recovered-{int(now)}-{os.getpid()}-{original_name}'
        os.replace(stale_claim, target)
    except OSError:
        pass

for path in sorted(root.glob('*.json'), key=lambda item: item.stat().st_mtime)[:100]:
    claim = path.with_name(f'.sending.{os.getpid()}.{path.name}')
    try:
        os.replace(path, claim)
    except OSError:
        continue
    data = {}
    try:
        data = json.loads(claim.read_text(encoding='utf-8'))
        payload = data.get('payload') if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise ValueError('job de status sem payload válido')
        attempts = int(data.get('attempts') or 0)
        next_attempt_at = float(data.get('next_attempt_at') or 0)
        if next_attempt_at > now:
            os.replace(claim, path)
            continue
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8', errors='ignore') or '{}')
        if result.get('ok') and (result.get('delivered') or result.get('ignored')):
            claim.unlink(missing_ok=True)
            continue
        raise RuntimeError(str(result.get('error') or result or 'sem confirmação'))
    except Exception as exc:
        try:
            permanent = isinstance(exc, (json.JSONDecodeError, ValueError, TypeError, AttributeError))
            attempts = 20 if permanent else (int(data.get('attempts') or 0) + 1 if isinstance(data, dict) else 1)
            if attempts >= 20:
                dead = root / 'failed'
                dead.mkdir(parents=True, exist_ok=True)
                failed_path = dead / path.name
                if isinstance(data, dict):
                    data['attempts'] = attempts
                    data['last_error'] = f'{type(exc).__name__}: {exc}'[:600]
                    data['failed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
                os.replace(claim, failed_path)
                continue
            if not isinstance(data, dict):
                data = {}
            data['attempts'] = attempts
            data['last_error'] = f'{type(exc).__name__}: {exc}'[:600]
            data['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data['next_attempt_at'] = now + min(300, 5 * (2 ** min(attempts, 6)))
            claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
            os.replace(claim, path)
        except Exception:
            try:
                os.replace(claim, path)
            except Exception:
                pass
PYFLUSHSTATUS
  local rc=$?
  chown -R ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" 2>/dev/null || true
  return "$rc"
}

flush_update_alert_outbox() {
  # A fila de logs pertence ao bot. Quando ele está online, pedimos um flush
  # imediato; quando está reiniciando/offline, os jobs permanecem no disco e o
  # loop de reconciliação do bot os entrega assim que voltar.
  prepare_update_delivery_dirs || true
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" python3 - <<'PYFLUSHLOG' 2>/dev/null || true
import json, os, pathlib, urllib.request

url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/flush-logs')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass
try:
    request = urllib.request.Request(url, data=b'{}', headers=headers, method='POST')
    urllib.request.urlopen(request, timeout=3).read()
except Exception:
    pass
PYFLUSHLOG
  return 0
}

notify_zip_status_message() {
  local status="${1:-info}"
  local title="${2:-Atualização}"
  local description="${3:-}"
  local final_delivery=0 generated_fallback_ui=0
  [[ "$status" =~ ^(success|ok|warn|error|done|failed)$ ]] && final_delivery=1

  # Todo estado final usa o mesmo renderer compacto do bot, inclusive falhas,
  # rejeições e recuperações que terminam antes do bloco final principal. Isso
  # evita voltar ao card técnico antigo justamente nos caminhos de erro.
  if (( final_delivery == 1 )) && [[ -z "${ZIP_STATUS_UI_JSON:-}" ]]; then
    local fallback_display_id fallback_from fallback_to fallback_count fallback_duration fallback_github
    fallback_display_id="${LOCAL_CANDIDATE_DISPLAY_ID:-${UPDATE_DISPLAY_ID:-${ROLLBACK_REQUEST_ID:-}}}"
    fallback_from="$(short_commit "${PREVIOUS_COMMIT:-${CURRENT_COMMIT:-}}")"
    fallback_to="$(short_commit "${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}")"
    fallback_count="$(format_update_file_count "${CHANGED_FILES_COUNT:-0}")"
    fallback_duration="$(human_duration "${SECONDS:-0}")"
    fallback_github="false"
    [[ "$status" =~ ^(success|ok|done)$ ]] && fallback_github="true"
    ZIP_STATUS_UI_JSON="$(
      UI_STATUS="$status" UI_HEADLINE="$title" UI_SUMMARY="$description" \
      UI_DISPLAY_ID="$fallback_display_id" UI_BRANCH="${BRANCH:-main}" \
      UI_FROM="$fallback_from" UI_TO="$fallback_to" UI_FILE_COUNT="$fallback_count" \
      UI_DIFF="${DIFF_TOTAL_SUMMARY:-}" UI_IMPACT="${APPLY_MODE:-}" UI_DURATION="$fallback_duration" \
      UI_TOTAL_DURATION="${TOTAL_FROM_RECEIVE_DURATION:-}" UI_HEALTH="${BOT_HEALTHCHECK_STATUS:-}" UI_GITHUB="$fallback_github" \
      UI_CHECKS="${CHECKS_TEXT:-}" UI_TIMINGS="${TIMINGS_TEXT:-}" UI_CACHE="${CACHE_TEXT:-}" \
      UI_TESTS="${TEST_PLAN_TEXT:-}" UI_FILES="${CHANGED_FILES:-}" UI_PROCESSES="${CHANGED_PROCESSES:-}" \
      python3 - <<'PYFALLBACKUI'
import json, os, re
headline = os.environ.get("UI_HEADLINE") or ""
headline = re.sub(r"^[^\wÀ-ÿ]+\s*", "", headline, count=1).strip()
print(json.dumps({
    "kind": "final",
    "status": os.environ.get("UI_STATUS") or "error",
    "headline": headline,
    "summary": os.environ.get("UI_SUMMARY") or "",
    "display_id": os.environ.get("UI_DISPLAY_ID") or "",
    "branch": os.environ.get("UI_BRANCH") or "main",
    "from": os.environ.get("UI_FROM") or "",
    "to": os.environ.get("UI_TO") or "",
    "file_count_text": os.environ.get("UI_FILE_COUNT") or "0 arquivos",
    "diff_summary": os.environ.get("UI_DIFF") or "",
    "impact": os.environ.get("UI_IMPACT") or "",
    "duration": os.environ.get("UI_DURATION") or "",
    "total_duration": os.environ.get("UI_TOTAL_DURATION") or "",
    "bot_health": os.environ.get("UI_HEALTH") or "",
    "github_synced": (os.environ.get("UI_GITHUB") or "false").lower() == "true",
    "checks_text": os.environ.get("UI_CHECKS") or "",
    "timings_text": os.environ.get("UI_TIMINGS") or "",
    "cache_text": os.environ.get("UI_CACHE") or "",
    "tests_text": os.environ.get("UI_TESTS") or "",
    "files_text": os.environ.get("UI_FILES") or "",
    "processes": os.environ.get("UI_PROCESSES") or "",
}, ensure_ascii=False))
PYFALLBACKUI
    )"
    generated_fallback_ui=1
  fi

  if (( REMOTE_CANDIDATE_MODE == 1 )); then
    post_direct_update_message "$REMOTE_STATUS_CHANNEL_ID" "$REMOTE_STATUS_MESSAGE_ID" "$status" "$title" "$description" "${ZIP_STATUS_CONTROL_JSON:-}"
    (( generated_fallback_ui == 1 )) && ZIP_STATUS_UI_JSON=""
    return 0
  fi
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -f "$LOCAL_CANDIDATE_DIR/manifest.json" ]] || return 0
  local channel_id message_id payload
  channel_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.channel_id 2>/dev/null || true)"
  message_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.message_id 2>/dev/null || true)"
  [[ -n "$channel_id" && -n "$message_id" ]] || return 0
  payload="$(CHANNEL_ID_VALUE="$channel_id" MESSAGE_ID_VALUE="$message_id" STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" CONTROL_VALUE="${ZIP_STATUS_CONTROL_JSON:-}" UI_VALUE="${ZIP_STATUS_UI_JSON:-}" CANDIDATE_ID_VALUE="${LOCAL_CANDIDATE_ID:-}" DISPLAY_ID_VALUE="${LOCAL_CANDIDATE_DISPLAY_ID:-}" python3 - <<'PYBUILDPAYLOAD'
import datetime, json, os
payload = {
    'channel_id': os.environ.get('CHANNEL_ID_VALUE') or '',
    'message_id': os.environ.get('MESSAGE_ID_VALUE') or '',
    'status': os.environ.get('STATUS_VALUE') or 'info',
    'title': os.environ.get('TITLE_VALUE') or 'Atualização',
    'description': os.environ.get('DESCRIPTION_VALUE') or '',
    'candidate_id': os.environ.get('CANDIDATE_ID_VALUE') or '',
    'display_id': os.environ.get('DISPLAY_ID_VALUE') or '',
    'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
try:
    control = json.loads(os.environ.get('CONTROL_VALUE') or '')
    if isinstance(control, dict):
        payload['control'] = control
except Exception:
    pass
try:
    ui = json.loads(os.environ.get('UI_VALUE') or '')
    if isinstance(ui, dict):
        payload['ui'] = ui
except Exception:
    pass
print(json.dumps(payload, ensure_ascii=False))
PYBUILDPAYLOAD
)"
  send_update_status_payload "$payload" "$final_delivery"
  local delivery_rc=$?
  (( generated_fallback_ui == 1 )) && ZIP_STATUS_UI_JSON=""
  return "$delivery_rc"
}

post_direct_update_message() {
  local channel_id="${1:-}"
  local message_id="${2:-}"
  local status="${3:-info}"
  local title="${4:-Atualização}"
  local description="${5:-}"
  local control_json="${6:-}"
  local final_delivery=0 payload
  [[ -n "$channel_id" && -n "$message_id" ]] || return 0
  [[ "$status" =~ ^(success|ok|warn|error|done|failed)$ ]] && final_delivery=1
  payload="$(CHANNEL_ID_VALUE="$channel_id" MESSAGE_ID_VALUE="$message_id" STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" CONTROL_VALUE="$control_json" UI_VALUE="${ZIP_STATUS_UI_JSON:-}" python3 - <<'PYDIRECTPAYLOAD'
import datetime, json, os
payload = {
    'channel_id': os.environ.get('CHANNEL_ID_VALUE') or '',
    'message_id': os.environ.get('MESSAGE_ID_VALUE') or '',
    'status': os.environ.get('STATUS_VALUE') or 'info',
    'title': os.environ.get('TITLE_VALUE') or 'Atualização',
    'description': os.environ.get('DESCRIPTION_VALUE') or '',
    'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
try:
    control = json.loads(os.environ.get('CONTROL_VALUE') or '')
    if isinstance(control, dict):
        payload['control'] = control
except Exception:
    pass
try:
    ui = json.loads(os.environ.get('UI_VALUE') or '')
    if isinstance(ui, dict):
        payload['ui'] = ui
except Exception:
    pass
print(json.dumps(payload, ensure_ascii=False))
PYDIRECTPAYLOAD
)"
  send_update_status_payload "$payload" "$final_delivery"
}

create_direct_update_message() {
  local status="${1:-applying}"
  local title="${2:-$UPDATE_TITLE_EMOJI Aplicando atualização...}"
  local description="${3:-}"
  STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" python3 - <<'PYCREATE' 2>/dev/null || true
import json, os, shlex, urllib.request
from pathlib import Path
payload = {
    "status": os.environ.get("STATUS_VALUE") or "applying",
    "title": os.environ.get("TITLE_VALUE") or "Atualização",
    "description": os.environ.get("DESCRIPTION_VALUE") or "",
}
url = (os.environ.get("BOT_HEALTH_URL") or "http://127.0.0.1:10000/health").replace("/health", "/internal/update/create-zip-status")
headers = {"Content-Type": "application/json"}
try:
    env_path = Path(os.environ.get("REPO_DIR", "/home/ubuntu/bot")) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("BOT_INTERNAL_UPDATE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    headers["X-Update-Token"] = token
                break
except Exception:
    pass
try:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
    if data.get("ok"):
        print("REMOTE_STATUS_CHANNEL_ID=" + shlex.quote(str(data.get("channel_id") or "")))
        print("REMOTE_STATUS_MESSAGE_ID=" + shlex.quote(str(data.get("message_id") or "")))
except Exception:
    pass
PYCREATE
}

zip_progress_title() {
  local stage_label="${1:-Processando atualização}"
  local lowered="${stage_label,,}"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    if [[ "${ROLLBACK_REQUEST_ACTION:-rollback}" == "redo" ]]; then
      printf '↪️ Reaplicando atualização'
    else
      printf '↩️ Revertendo atualização'
    fi
    return 0
  fi

  # O título acompanha a fase sem expor o subsistema alterado. A etapa detalhada
  # continua logo abaixo, enquanto o cabeçalho deixa claro que o painel avançou.
  case "$lowered" in
    *fila*|*aguard*)
      printf '📦 Atualização na fila'
      ;;
    *finalizando*)
      printf '✅ Finalizando atualização'
      ;;
    *fazendo\ commit*|*commit\ criado*|*registrando*)
      printf '📝 Registrando atualização'
      ;;
    *publicando\ no\ github*|*github*)
      printf '☁️ Publicando atualização'
      ;;
    *publicando*|*enviando*)
      printf '🚀 Publicando atualização'
      ;;
    *compilando*|*build*)
      printf '🛠️ Compilando atualização'
      ;;
    *instalando*|*preparando\ servidor*|*dependências*)
      printf '📦 Preparando atualização'
      ;;
    *reiniciando*)
      printf '🔄 Reiniciando serviços'
      ;;
    *recarregando*|*reload*)
      printf '🔄 Aplicando atualização'
      ;;
    *conferindo*|*validando*|*verificando*|*analisando*|*recuperando*)
      printf '🔎 Verificando atualização'
      ;;
    *aplicando*|*preparando\ arquivos*)
      printf '⚙️ Aplicando atualização'
      ;;
    *)
      printf '⚙️ Atualizando'
      ;;
  esac
}

zip_progress_macro_index() {
  local stage_label="${1:-Processando atualização}"
  local lowered="${stage_label,,}"
  case "$lowered" in
    *github*|*push*|*sincronizando*) printf '9' ;;
    *health*|*saúde*|*saude*|*comandos*|*estabilidade*|*finalizando*|*estado\ publicado*|*recuperando\ confirmação*|"validando"|*validando\ arquivos*) printf '8' ;;
    *publicando\ interface*|*publicando\ servidor*|*reiniciando*|*recarregando*|*reload*|*ativando*|*validando\ aplicação*|*validando\ aplicacao*) printf '7' ;;
    *promov*|*aplicando\ na\ vps*) printf '6' ;;
    *ready*|*release*|*preservando\ runtime*|*fazendo\ commit*|*commit\ criado*|*registrando*) printf '5' ;;
    *validando\ runtime*|*verificando\ comandos*|*test*|*typescript*|*compilando*|*build*|*dependências*|*dependencias*) printf '4' ;;
    *aplicando\ em\ área*|*worktree*|*staging*|*isolamento*) printf '3' ;;
    *validando\ estado*|*preparando\ arquivos*) printf '2' ;;
    *integridade*|*segurança*|*seguranca*|*validando\ permissões*|*analisando*) printf '1' ;;
    *conferindo\ zip*|*pacote*|*conferindo\ commit*) printf '0' ;;
    *validando*) printf '4' ;;
    *) printf '2' ;;
  esac
}

zip_progress_advance_macro_index() {
  local candidate="${1:-0}" current="${ZIP_PROGRESS_CURRENT_MACRO_INDEX:--1}"
  local max_index="${ZIP_PROGRESS_MACRO_MAX_INDEX:-9}"
  [[ "$candidate" =~ ^[0-9]+$ ]] || candidate=0
  [[ "$current" =~ ^-?[0-9]+$ ]] || current=-1
  (( candidate > max_index )) && candidate="$max_index"
  (( current > max_index )) && current="$max_index"
  if (( candidate > current )); then
    ZIP_PROGRESS_CURRENT_MACRO_INDEX="$candidate"
  else
    ZIP_PROGRESS_CURRENT_MACRO_INDEX="$current"
  fi
}

zip_progress_add_macro_duration() {
  local macro_index="${1:-}" elapsed_ms="${2:-0}" var current
  [[ "$macro_index" =~ ^[0-9]+$ ]] || return 0
  (( macro_index <= ${ZIP_PROGRESS_MACRO_MAX_INDEX:-9} )) || return 0
  [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
  var="ZIP_PROGRESS_MACRO_DURATION_MS_${macro_index}"
  current="${!var:-0}"
  [[ "$current" =~ ^[0-9]+$ ]] || current=0
  printf -v "$var" '%d' "$((current + elapsed_ms))"
}

zip_progress_macro_duration_pairs() {
  local index var elapsed_ms duration output=""
  for ((index=0; index<=${ZIP_PROGRESS_MACRO_MAX_INDEX:-9}; index++)); do
    var="ZIP_PROGRESS_MACRO_DURATION_MS_${index}"
    elapsed_ms="${!var:-0}"
    [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
    (( elapsed_ms > 0 )) || continue
    duration="$(format_update_duration_ms "$elapsed_ms")"
    [[ -n "$output" ]] && output+="|"
    output+="${index}=${duration}"
  done
  printf '%s' "$output"
}

zip_progress_status() {
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    printf 'progress'
  else
    printf 'applying'
  fi
}

zip_progress_identifier() {
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    printf '%s' "${LOCAL_CANDIDATE_DISPLAY_ID:-$LOCAL_CANDIDATE_ID}"
  elif (( ROLLBACK_CONTROL_MODE == 1 )); then
    printf '%s' "${ROLLBACK_REQUEST_ID:-controle}"
  elif [[ -n "${SHORT_TO:-}" ]]; then
    printf 'commit %s' "$SHORT_TO"
  else
    printf 'atualização'
  fi
}

zip_progress_hydrate_from_candidate() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  (( ZIP_PROGRESS_HANDOFF_LOADED == 0 )) || return 0
  [[ -f "${LOCAL_CANDIDATE_DIR:-}/manifest.json" ]] || return 0

  local record_kind elapsed_ms encoded_label label total_ms=0 summed_ms=0 started_at_ms=0 now_ms line index
  local -a handoff_labels=()
  local -a handoff_elapsed=()

  while IFS=$'\t' read -r record_kind elapsed_ms encoded_label; do
    case "$record_kind" in
      META)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] && total_ms="$elapsed_ms"
        ;;
      START)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] && started_at_ms="$elapsed_ms"
        ;;
      STEP)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
        label="$(printf '%s' "$encoded_label" | base64 --decode 2>/dev/null || true)"
        label="$(printf '%s' "$label" | tr '\r\n\t' '   ' | tr -s ' ' | sed 's/^ //;s/ $//' | cut -c1-120)"
        [[ -n "${label//[[:space:]]/}" ]] || continue
        if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]] \
          && printf '%s\n' "$ZIP_PROGRESS_DONE_LABELS" | grep -Fxq -- "$label"; then
          continue
        fi
        handoff_labels+=("$label")
        handoff_elapsed+=("$elapsed_ms")
        summed_ms=$((summed_ms + elapsed_ms))
        ;;
    esac
  done < <(python3 - "${LOCAL_CANDIDATE_DIR}/manifest.json" <<'PYHANDOFF' 2>/dev/null || true
import base64
import json
import pathlib
import sys

try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

handoff = data.get("progress_handoff")
if not isinstance(handoff, dict) or int(handoff.get("schema_version") or 0) != 1:
    raise SystemExit(0)

try:
    total_ms = max(0, min(int(handoff.get("preparation_total_ms") or 0), 15 * 60 * 1000))
except (TypeError, ValueError):
    total_ms = 0
print(f"META\t{total_ms}\t")
try:
    started_at_ms = max(0, int(handoff.get("started_at_epoch_ms") or 0))
except (TypeError, ValueError):
    started_at_ms = 0
print(f"START\t{started_at_ms}\t")

seen: set[str] = set()
steps = handoff.get("completed_steps")
if not isinstance(steps, list):
    steps = []
for raw in steps[:20]:
    if not isinstance(raw, dict):
        continue
    label = " ".join(str(raw.get("label") or "").split())[:120]
    if not label or label in seen:
        continue
    try:
        elapsed_ms = max(0, min(int(raw.get("elapsed_ms") or 0), 10 * 60 * 1000))
    except (TypeError, ValueError):
        elapsed_ms = 0
    seen.add(label)
    encoded = base64.b64encode(label.encode("utf-8")).decode("ascii")
    print(f"STEP\t{elapsed_ms}\t{encoded}")
PYHANDOFF
  )

  ((${#handoff_labels[@]} > 0)) || return 0
  (( total_ms < summed_ms )) && total_ms="$summed_ms"
  (( total_ms > 900000 )) && total_ms=900000

  for ((index=0; index<${#handoff_labels[@]}; index++)); do
    label="${handoff_labels[$index]}"
    elapsed_ms="${handoff_elapsed[$index]}"
    if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]]; then
      ZIP_PROGRESS_DONE_LABELS+=$'\n'
    fi
    ZIP_PROGRESS_DONE_LABELS="${ZIP_PROGRESS_DONE_LABELS:-}${label}"
    ZIP_PROGRESS_LAST_DONE_LABEL="$label"
    ZIP_PROGRESS_LAST_DONE_DURATION="$(format_update_duration_ms "$elapsed_ms")"
    ZIP_PROGRESS_LAST_DONE_MACRO_INDEX="$(zip_progress_macro_index "$label")"
    zip_progress_add_macro_duration "$ZIP_PROGRESS_LAST_DONE_MACRO_INDEX" "$elapsed_ms"
    line="-# $label"
    if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
      ZIP_PROGRESS_HISTORY+=$'\n'
    fi
    ZIP_PROGRESS_HISTORY+="$line"
    ZIP_PROGRESS_COMPLETED_COUNT=$((ZIP_PROGRESS_COMPLETED_COUNT + 1))
  done
  zip_progress_trim_history

  now_ms="$(update_now_ms)"
  if [[ "$now_ms" =~ ^[0-9]+$ ]] && (( ZIP_PROGRESS_STARTED_MS <= 0 )); then
    if (( started_at_ms > 0 && started_at_ms <= now_ms )); then
      ZIP_PROGRESS_STARTED_MS="$started_at_ms"
      ZIP_PROGRESS_RECEIVED_AT_MS="$started_at_ms"
      local updater_started_ms="$LOCAL_CANDIDATE_UPDATER_STARTED_MS"
      [[ "$updater_started_ms" =~ ^[0-9]+$ ]] || updater_started_ms="$UPDATER_PROCESS_STARTED_MS"
      if [[ "$updater_started_ms" =~ ^[0-9]+$ ]] && (( updater_started_ms >= started_at_ms )); then
        ZIP_PROGRESS_UPDATER_DELAY_MS=$((updater_started_ms - started_at_ms))
      fi
    else
      ZIP_PROGRESS_STARTED_MS=$((now_ms - total_ms))
      (( ZIP_PROGRESS_STARTED_MS < 0 )) && ZIP_PROGRESS_STARTED_MS=0
    fi
  fi
  ZIP_PROGRESS_STAGE_LABEL=""
  ZIP_PROGRESS_STAGE_STARTED_MS=0
  ZIP_PROGRESS_HANDOFF_LOADED=1
}

zip_progress_trim_history() {
  local line_count
  line_count="$(printf '%s\n' "$ZIP_PROGRESS_HISTORY" | awk 'NF {c++} END {print c+0}')"
  while (( line_count > ZIP_PROGRESS_MAX_VISIBLE_STEPS )); do
    ZIP_PROGRESS_HISTORY="$(printf '%s\n' "$ZIP_PROGRESS_HISTORY" | awk 'BEGIN{removed=0} {if (!removed && NF) {removed=1; next} print}')"
    ZIP_PROGRESS_HIDDEN_COUNT=$((ZIP_PROGRESS_HIDDEN_COUNT + 1))
    line_count=$((line_count - 1))
  done
}

zip_progress_close_active_macro_on_advance() {
  local next_label="${1:-}" now_ms="${2:-0}"
  local active_label="${ZIP_PROGRESS_STAGE_LABEL:-}" active_started="${ZIP_PROGRESS_STAGE_STARTED_MS:-0}"
  local active_macro next_macro elapsed_ms
  [[ -n "${active_label//[[:space:]]/}" ]] || return 0
  [[ "$active_started" =~ ^[0-9]+$ ]] || return 0
  (( active_started > 0 )) || return 0
  [[ "$now_ms" =~ ^[0-9]+$ ]] || return 0

  active_macro="$(zip_progress_macro_index "$active_label")"
  next_macro="$(zip_progress_macro_index "$next_label")"
  [[ "$active_macro" =~ ^[0-9]+$ && "$next_macro" =~ ^[0-9]+$ ]] || return 0
  # Evento atrasado/regressivo não pode fechar nem reatribuir a etapa atual.
  (( next_macro >= active_macro )) || return 0

  elapsed_ms=$((now_ms - active_started))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  zip_progress_add_macro_duration "$active_macro" "$elapsed_ms"
  if (( next_macro > active_macro )); then
    ZIP_PROGRESS_LAST_DONE_LABEL="$active_label"
    ZIP_PROGRESS_LAST_DONE_DURATION="$(format_update_duration_ms "$elapsed_ms")"
    ZIP_PROGRESS_LAST_DONE_MACRO_INDEX="$active_macro"
  fi
}

zip_progress_publish() {
  local stage_label="${1:-Processando atualização}"
  local detail="${2:-}"
  local title status description identifier now_ms elapsed_ms elapsed_text footer stage_changed=0
  now_ms="$(update_now_ms)"
  if (( ZIP_PROGRESS_STARTED_MS <= 0 )); then
    ZIP_PROGRESS_STARTED_MS="$now_ms"
  fi
  if [[ "$ZIP_PROGRESS_STAGE_LABEL" != "$stage_label" && "$ZIP_PROGRESS_STAGE_STARTED_MS" -gt 0 ]]; then
    zip_progress_close_active_macro_on_advance "$stage_label" "$now_ms"
  fi
  if [[ "$ZIP_PROGRESS_STAGE_LABEL" != "$stage_label" || "$ZIP_PROGRESS_STAGE_STARTED_MS" -le 0 ]]; then
    ZIP_PROGRESS_STAGE_LABEL="$stage_label"
    ZIP_PROGRESS_STAGE_STARTED_MS="$now_ms"
    stage_changed=1
  fi
  if declare -F write_update_runtime_state >/dev/null 2>&1; then
    write_update_runtime_state "$stage_label"
  fi
  title="$(zip_progress_title "$stage_label")"
  status="$(zip_progress_status)"
  description=""
  if (( ZIP_PROGRESS_HIDDEN_COUNT > 0 )); then
    if (( ZIP_PROGRESS_HIDDEN_COUNT == 1 )); then
      description+="-# … 1 etapa anterior concluída"$'\n'
    else
      description+="-# … $ZIP_PROGRESS_HIDDEN_COUNT etapas anteriores concluídas"$'\n'
    fi
  fi
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    description+="$ZIP_PROGRESS_HISTORY"$'\n'
  fi
  description+="$UPDATE_STAGE_EMOJI **$stage_label**"
  if [[ -n "${detail//[[:space:]]/}" ]]; then
    description+=$'\n'"-# $detail"
  fi
  identifier="$(zip_progress_identifier)"
  elapsed_ms=$((now_ms - ZIP_PROGRESS_STARTED_MS))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  if (( ZIP_PROGRESS_COMPLETED_COUNT == 1 )); then
    footer="$identifier · 1 etapa concluída · $elapsed_text"
  else
    footer="$identifier · $ZIP_PROGRESS_COMPLETED_COUNT etapas concluídas · $elapsed_text"
  fi
  description+=$'\n'"-# $footer"
  local macro_index action_name
  macro_index="$(zip_progress_macro_index "$stage_label")"
  zip_progress_advance_macro_index "$macro_index"
  macro_index="$ZIP_PROGRESS_CURRENT_MACRO_INDEX"
  action_name="update"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    if [[ "${ROLLBACK_REQUEST_ACTION:-rollback}" == "redo" ]]; then
      action_name="redo"
    else
      action_name="rollback"
    fi
  fi
  local macro_duration_pairs
  macro_duration_pairs="$(zip_progress_macro_duration_pairs)"
  ZIP_STATUS_UI_JSON="$(UI_KIND=progress UI_STAGE="$stage_label" UI_DETAIL="$detail" UI_IDENTIFIER="$identifier" UI_ELAPSED="$elapsed_text" UI_COMPLETED_STAGE="${ZIP_PROGRESS_LAST_DONE_LABEL:-}" UI_COMPLETED_DURATION="${ZIP_PROGRESS_LAST_DONE_DURATION:-}" UI_COMPLETED_MACRO_INDEX="${ZIP_PROGRESS_LAST_DONE_MACRO_INDEX:--1}" UI_MACRO_INDEX="$macro_index" UI_MACRO_DURATIONS="$macro_duration_pairs" UI_ACTION="$action_name" python3 - <<'PYPROGRESSUI'
import json, os
try:
    completed_macro_index = int(os.environ.get("UI_COMPLETED_MACRO_INDEX") or -1)
except (TypeError, ValueError):
    completed_macro_index = -1
macro_durations = {}
for item in (os.environ.get("UI_MACRO_DURATIONS") or "").split("|"):
    if "=" not in item:
        continue
    key, value = item.split("=", 1)
    key, value = key.strip(), value.strip()
    if key.isdigit() and value:
        macro_durations[key] = value
print(json.dumps({
    "kind": "progress",
    "stage": os.environ.get("UI_STAGE") or "Processando atualização",
    "detail": os.environ.get("UI_DETAIL") or "",
    "identifier": os.environ.get("UI_IDENTIFIER") or "",
    "elapsed": os.environ.get("UI_ELAPSED") or "",
    "completed_stage": os.environ.get("UI_COMPLETED_STAGE") or "",
    "completed_duration": os.environ.get("UI_COMPLETED_DURATION") or "",
    "completed_macro_index": completed_macro_index,
    "macro_durations": macro_durations,
    "macro_index": int(os.environ.get("UI_MACRO_INDEX") or 0),
    "action": os.environ.get("UI_ACTION") or "update",
}, ensure_ascii=False))
PYPROGRESSUI
)"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "$status" "$title" "$description" || true
  else
    notify_zip_status_message "$status" "$title" "$description" || true
  fi
  ZIP_STATUS_UI_JSON=""
  update_local_candidate_heartbeat "active" "" "$stage_label"
}

zip_recovery_publish() {
  local stage_label="${1:-Restaurando versão anterior}"
  local detail="${2:-}"
  local recovery_step="${3:-0}"
  local now_ms elapsed_ms elapsed_text identifier failure_code
  now_ms="$(update_now_ms)"
  if (( ZIP_RECOVERY_STARTED_MS <= 0 )); then
    ZIP_RECOVERY_STARTED_MS="$now_ms"
  fi
  elapsed_ms=$((now_ms - ZIP_RECOVERY_STARTED_MS))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  identifier="$(zip_progress_identifier)"
  failure_code="${LAST_ERROR_CODE:-UPDATE_STAGE_FAILED}"
  ZIP_STATUS_UI_JSON="$(UI_STAGE="$stage_label" UI_DETAIL="$detail" UI_IDENTIFIER="$identifier" UI_ELAPSED="$elapsed_text" UI_RECOVERY_STEP="$recovery_step" UI_FAILURE_CODE="$failure_code" python3 - <<'PYRECOVERYUI'
import json, os
print(json.dumps({
    "kind": "recovery",
    "stage": os.environ.get("UI_STAGE") or "Restaurando versão anterior",
    "detail": os.environ.get("UI_DETAIL") or "",
    "identifier": os.environ.get("UI_IDENTIFIER") or "",
    "elapsed": os.environ.get("UI_ELAPSED") or "",
    "recovery_step": int(os.environ.get("UI_RECOVERY_STEP") or 0),
    "failure_code": os.environ.get("UI_FAILURE_CODE") or "UPDATE_STAGE_FAILED",
}, ensure_ascii=False))
PYRECOVERYUI
)"
  notify_zip_status_message "recovering" "Atualização falhou" "$detail" || true
  ZIP_STATUS_UI_JSON=""
}

zip_progress_done() {
  local done_label="${1:-}"
  [[ -n "${done_label//[[:space:]]/}" ]] || return 0
  # Uma retomada ou uma transição repetida não pode recolocar a mesma microetapa
  # no histórico. O painel sempre avança de forma monotônica.
  if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]] \
    && printf '%s\n' "${ZIP_PROGRESS_DONE_LABELS:-}" | grep -Fxq -- "$done_label"; then
    return 0
  fi
  if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]]; then
    ZIP_PROGRESS_DONE_LABELS+=$'\n'
  fi
  ZIP_PROGRESS_DONE_LABELS="${ZIP_PROGRESS_DONE_LABELS:-}${done_label}"
  local now_ms elapsed_ms elapsed_text line
  now_ms="$(update_now_ms)"
  if (( ZIP_PROGRESS_STAGE_STARTED_MS > 0 )); then
    elapsed_ms=$((now_ms - ZIP_PROGRESS_STAGE_STARTED_MS))
  else
    elapsed_ms=0
  fi
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  local completed_macro_source completed_macro_index
  completed_macro_source="${ZIP_PROGRESS_STAGE_LABEL:-$done_label}"
  completed_macro_index="$(zip_progress_macro_index "$completed_macro_source")"
  ZIP_PROGRESS_LAST_DONE_LABEL="$done_label"
  ZIP_PROGRESS_LAST_DONE_DURATION="$elapsed_text"
  ZIP_PROGRESS_LAST_DONE_MACRO_INDEX="$completed_macro_index"
  zip_progress_add_macro_duration "$completed_macro_index" "$elapsed_ms"
  ZIP_PROGRESS_COMPLETED_COUNT=$((ZIP_PROGRESS_COMPLETED_COUNT + 1))
  line="-# $done_label"
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    ZIP_PROGRESS_HISTORY+=$'\n'
  fi
  ZIP_PROGRESS_HISTORY+="$line"
  zip_progress_trim_history
  ZIP_PROGRESS_STAGE_LABEL=""
  ZIP_PROGRESS_STAGE_STARTED_MS=0
}

zip_progress_done_and_publish() {
  local done_label="${1:-}"
  local next_label="${2:-Processando atualização}"
  local detail="${3:-}"
  zip_progress_done "$done_label"
  zip_progress_publish "$next_label" "$detail"
}

zip_progress_heartbeat_seconds() {
  local interval="${DISCORD_AUTO_UPDATE_PROGRESS_HEARTBEAT_SECONDS:-12}"
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=12
  (( interval < 5 )) && interval=5
  (( interval > 60 )) && interval=60
  printf '%s' "$interval"
}

zip_progress_run_as_ubuntu() {
  local stage_label="${1:?}"
  local detail="${2:-Em andamento}"
  local command="${3:?}"
  local pid rc started_ms now_ms elapsed_ms interval next_publish_ms stage_log

  zip_progress_publish "$stage_label" "$detail"
  interval="$(zip_progress_heartbeat_seconds)"
  started_ms="$(update_now_ms)"
  next_publish_ms=$((started_ms + interval * 1000))
  stage_log="$(stage_log_file_for "$STAGE" 2>/dev/null || true)"
  CURRENT_STAGE_LOG_FILE="$stage_log"
  CURRENT_STAGE_LOG_STAGE="$STAGE"
  CURRENT_STAGE_COMMAND="$command"
  if [[ -n "$stage_log" ]]; then
    : > "$stage_log" 2>/dev/null || true
    chmod 0644 "$stage_log" 2>/dev/null || true
  fi

  # O comando roda em um shell filho sem herdar o trap ERR transacional. Assim,
  # o pai pode acompanhar o PID, publicar heartbeats e tratar o status uma vez.
  # Quando possível, a saída também é preservada num log persistente exclusivo
  # da etapa; o tee global continua recebendo a mesma saída normalmente.
  if [[ -n "$stage_log" ]]; then
    ( set -o pipefail; sudo -u ubuntu -H bash -lc "$command" 2>&1 | tee -a "$stage_log" ) &
  else
    sudo -u ubuntu -H bash -lc "$command" &
  fi
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
    now_ms="$(update_now_ms)"
    if (( now_ms >= next_publish_ms )); then
      elapsed_ms=$((now_ms - started_ms))
      (( elapsed_ms < 0 )) && elapsed_ms=0
      zip_progress_publish "$stage_label" "$detail"
      next_publish_ms=$((now_ms + interval * 1000))
    fi
  done

  local restore_errexit=0
  [[ $- == *e* ]] && restore_errexit=1
  set +e
  wait "$pid"
  rc=$?
  if (( restore_errexit == 1 )); then
    set -e
  else
    set +e
  fi
  return "$rc"
}

zip_progress_only_site_changed() {
  (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )) || return 1
  (( BOT_CHANGED == 0 )) || return 1
  (( REQUIREMENTS_CHANGED == 0 )) || return 1
  (( AUDIO_SYSTEMD_CHANGED == 0 )) || return 1
  (( CLEANUP_CHANGED == 0 )) || return 1
  (( PHONE_LAVALINK_WATCH_CHANGED == 0 )) || return 1
  (( PHONE_WORKER_WATCH_CHANGED == 0 )) || return 1
  (( VPS_SYSTEMD_UNITS_CHANGED == 0 )) || return 1
  (( ALERT_CHANGED == 0 )) || return 1
  (( PHONE_WORKER_SYNC_REQUIRED == 0 )) || return 1
  (( CORE_WORKER_APK_CHANGED == 0 )) || return 1
  (( CORE_WORKER_AUTOMATION_REQUIRED == 0 )) || return 1
  return 0
}

zip_progress_process_detail() {
  local processes
  processes="$(format_changed_processes 2>/dev/null || true)"
  if [[ -n "${processes//[[:space:]]/}" && "$processes" != "nenhum processo alterado" ]]; then
    printf '%s' "$processes"
  fi
}

format_cog_module_names() {
  local modules_text="${1:-}"
  MODULES_TEXT="$modules_text" python3 - <<'PYCOGS'
import os
mods = []
for raw in (os.environ.get("MODULES_TEXT") or "").splitlines():
    raw = raw.strip()
    if not raw:
        continue
    name = raw
    if name.startswith("cogs."):
        name = name[5:]
    mods.append(name.replace("_", "-"))
print(", ".join(dict.fromkeys(mods)))
PYCOGS
}

fast_reload_stage_label() {
  local modules_text="${1:-}"
  [[ -n "${modules_text//[[:space:]]/}" ]] || return 1
  local names count
  names="$(format_cog_module_names "$modules_text")"
  count="$(printf '%s\n' "$modules_text" | awk 'NF {c++} END {print c+0}')"
  if [[ "$count" =~ ^[0-9]+$ && "$count" -gt 1 ]]; then
    printf 'Recarregando cogs: %s' "$names"
  else
    printf 'Recarregando cog: %s' "$names"
  fi
}

zip_progress_next_apply_stage() {
  local fast_modules process_detail
  fast_modules="$(fast_reload_modules_for_changed_files 2>/dev/null || true)"
  FAST_RELOAD_MODULES="$fast_modules"
  if [[ -n "${fast_modules//[[:space:]]/}" ]]; then
    fast_reload_stage_label "$fast_modules"
    return 0
  fi
  # Mostre a primeira operação real, não um reinício genérico. Em patches do
  # site, npm ci/build é normalmente a parte mais longa e precisa ficar visível.
  if (( BOT_CHANGED == 0 && FRONT_CHANGED == 1 )); then
    printf 'Instalando dependências'
    return 0
  fi
  if (( BOT_CHANGED == 0 && FRONT_CHANGED == 0 && BACK_CHANGED == 1 )); then
    printf 'Preparando servidor'
    return 0
  fi
  process_detail="$(zip_progress_process_detail)"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    if [[ "$process_detail" == *,* ]]; then
      printf 'Reiniciando processos: %s' "$process_detail"
    else
      printf 'Reiniciando processo: %s' "$process_detail"
    fi
    return 0
  fi
  printf 'Validando aplicação'
}

zip_progress_done_apply_stage() {
  # Frontend/backend já publicam suas próprias fases e health check. Não acrescente
  # depois uma etapa genérica de “processo reiniciado”, que fazia o painel parecer
  # voltar ao início justamente quando o build terminava.
  if zip_progress_only_site_changed; then
    return 0
  fi
  local process_detail names count
  if [[ "${FAST_RELOAD_STATUS:-}" == "OK"* && -n "${FAST_RELOAD_MODULES//[[:space:]]/}" ]]; then
    names="$(format_cog_module_names "$FAST_RELOAD_MODULES")"
    count="$(printf '%s\n' "$FAST_RELOAD_MODULES" | awk 'NF {c++} END {print c+0}')"
    if [[ "$count" =~ ^[0-9]+$ && "$count" -gt 1 ]]; then
      zip_progress_done "Cogs recarregadas: **$names**"
    else
      zip_progress_done "Cog recarregada: **$names**"
    fi
    return 0
  fi
  process_detail="$(zip_progress_process_detail)"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    zip_progress_done "Processos reiniciados: **$process_detail**"
  else
    zip_progress_done "Aplicação validada"
  fi
}
