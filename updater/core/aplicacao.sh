# Funções de aplicação, publicação e releases do updater.
# Extraídas do orquestrador sem alteração de comportamento.

sanitize_vps_lavalink_units() {
  # Remove dependências locais do Lavalink. O node de áudio válido fica no phone worker/Music Agent.
  local file changed_any=0
  for file in /etc/systemd/system/tts-bot.service /etc/systemd/system/tts-bot.service.d/*.conf; do
    [[ -f "$file" ]] || continue
    if grep -q 'lavalink.service' "$file" 2>/dev/null; then
      cp -a "$file" "$file.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      python3 - "$file" <<'PY_SANITIZE_LAVALINK'
import sys
from pathlib import Path
p=Path(sys.argv[1])
text=p.read_text(encoding='utf-8', errors='replace')
out=[]
for line in text.splitlines():
    stripped=line.strip()
    if stripped.startswith(('Wants=', 'Requires=', 'After=', 'Before=')) and 'lavalink.service' in stripped:
        key, value = line.split('=', 1)
        parts=[part for part in value.split() if part != 'lavalink.service']
        if parts:
            out.append(key + '=' + ' '.join(parts))
        else:
            out.append('# ' + key + '=lavalink.service removido: Lavalink roda no phone worker/Music Agent')
        continue
    if stripped.startswith('ExecStartPre=') and 'wait-audio-node-ready.py' in stripped:
        out.append('# ExecStartPre wait-audio-node-ready.py removido: Lavalink local da VPS não é usado')
        continue
    out.append(line)
p.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_SANITIZE_LAVALINK
      changed_any=1
    fi
  done
  if ! env_truthy VPS_LAVALINK_ENABLED; then
    systemctl stop lavalink.service >/dev/null 2>&1 || true
    systemctl disable lavalink.service >/dev/null 2>&1 || true
    systemctl reset-failed lavalink.service >/dev/null 2>&1 || true
    # systemctl mask falha quando /etc/systemd/system/lavalink.service é um arquivo
    # real. Fazemos a máscara idempotente manualmente para impedir restart-loop local.
    if [[ -e /etc/systemd/system/lavalink.service && ! -L /etc/systemd/system/lavalink.service ]]; then
      cp -a /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      mv /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.disabled.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    fi
    ln -sfn /dev/null /etc/systemd/system/lavalink.service 2>/dev/null || true
    changed_any=1
  fi
  if (( changed_any == 1 )); then
    systemctl daemon-reload || true
  fi
}


normalize_healthcheck_crontab() {
  # Corrige linhas temporárias quebradas criadas durante diagnóstico manual.
  # Não reativa healthcheck/resource-check automaticamente: eles continuam
  # pausados até a correção passar pelo patch e pelo operador.
  STAGE="normalização do crontab de emergência"
  local tmp current
  tmp="${TMPDIR:-/tmp}/tts-bot-cron.$$"
  current="${TMPDIR:-/tmp}/tts-bot-cron-current.$$"
  if ! sudo -u ubuntu -H crontab -l > "$current" 2>/dev/null; then
    CRONTAB_HEALTH_STATUS="sem crontab do usuário ubuntu"
    rm -f "$tmp" "$current" 2>/dev/null || true
    return 0
  fi
  python3 - "$current" "$tmp" <<'PY_CRON'
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
text = src.read_text(encoding='utf-8', errors='replace')

HEALTH_DISABLED = '# TEMP_DISABLED_HEALTHCHECK_UNTIL_PATCH_20260524 * * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_DISABLED = '# TEMP_DISABLED_EMERGENCY_20260524 */5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'
HEALTH_ACTIVE = '* * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_ACTIVE = '*/5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'

out = []
has_disabled_health = False
has_active_health = False
has_disabled_resource = False
has_active_resource = False

def is_redirect_only(line):
    return line.strip() in {'>/dev/null 2>&1', '>>/dev/null 2>&1', '2>&1', '&>/dev/null'}

def kind_for(line):
    if 'healthcheck.sh' in line:
        return 'health'
    if 'resource-check.sh' in line:
        return 'resource'
    return None

def disabled(line):
    return line.lstrip().startswith('#') or 'TEMP_DISABLED' in line

for raw in text.splitlines():
    line = raw.rstrip('\r')
    if is_redirect_only(line):
        continue
    kind = kind_for(line)
    if kind == 'health':
        if disabled(line):
            if not has_disabled_health:
                out.append(HEALTH_DISABLED)
                has_disabled_health = True
        else:
            if not has_active_health:
                out.append(HEALTH_ACTIVE)
                has_active_health = True
        continue
    if kind == 'resource':
        if disabled(line):
            if not has_disabled_resource:
                out.append(RESOURCE_DISABLED)
                has_disabled_resource = True
        else:
            if not has_active_resource:
                out.append(RESOURCE_ACTIVE)
                has_active_resource = True
        continue
    out.append(line)

dst.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_CRON
  if ! cmp -s "$current" "$tmp"; then
    cp -a /var/spool/cron/crontabs/ubuntu "$REPO_DIR/crontab.backup.auto-clean.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    sudo -u ubuntu -H crontab "$tmp" || true
    CRONTAB_HEALTH_STATUS="normalizado; healthcheck/resource-check seguem pausados se estavam pausados"
  else
    CRONTAB_HEALTH_STATUS="limpo"
  fi
  rm -f "$tmp" "$current" 2>/dev/null || true
}

managed_systemd_template_source() {
  local rel="${1:-}"
  local canonical_src="$REPO_DIR/updater/sistema/$rel"
  local root_src="$REPO_DIR/deploy/systemd/$rel"
  local vps_src="$REPO_DIR/deploy/systemd/vps/$rel"

  # As units próprias do updater têm uma única fonte de verdade.
  case "$rel" in
    bot-updater.service|bot-updater.timer|bot-updater.path|bot-updater-alert@.service)
      [[ -f "$canonical_src" ]] && printf '%s' "$canonical_src"
      return 0
      ;;
  esac

  # Os demais templates continuam pertencendo à infraestrutura geral da VPS.
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Fxq "deploy/systemd/vps/$rel"; then
    [[ -f "$vps_src" ]] && printf '%s' "$vps_src"
    return 0
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Fxq "deploy/systemd/$rel"; then
    [[ -f "$root_src" ]] && printf '%s' "$root_src"
    return 0
  fi
  if [[ -f "$vps_src" ]]; then
    printf '%s' "$vps_src"
  elif [[ -f "$root_src" ]]; then
    printf '%s' "$root_src"
  fi
}

build_vps_systemd_template_overlay() {
  local overlay="${1:?overlay obrigatório}"
  local rel src changed_path
  local -a managed_units=(
    tts-bot.service
    bot-updater.service
    bot-updater.timer
    bot-updater.path
    bot-updater-alert@.service
    cleanup-audio-temp.service
    cleanup-audio-temp.timer
    sinuca-activity-server.service
    phone-worker-watch.service
    phone-worker-watch.timer
  )

  mkdir -p "$overlay"
  for rel in "${managed_units[@]}"; do
    src="$(managed_systemd_template_source "$rel")"
    [[ -n "$src" && -f "$src" ]] || continue
    mkdir -p "$overlay/$(dirname "$rel")"
    cp -a "$src" "$overlay/$rel"
  done

  # Os drop-ins vivem normalmente em vps/. Mantenha a árvore completa e
  # sobreponha apenas arquivos da raiz explicitamente alterados no patch.
  if [[ -d "$REPO_DIR/deploy/systemd/vps/tts-bot.service.d" ]]; then
    mkdir -p "$overlay/tts-bot.service.d"
    cp -a "$REPO_DIR/deploy/systemd/vps/tts-bot.service.d/." "$overlay/tts-bot.service.d/"
  fi
  while IFS= read -r changed_path; do
    [[ "$changed_path" == deploy/systemd/tts-bot.service.d/* ]] || continue
    rel="${changed_path#deploy/systemd/}"
    src="$REPO_DIR/$changed_path"
    [[ -f "$src" ]] || continue
    mkdir -p "$overlay/$(dirname "$rel")"
    cp -a "$src" "$overlay/$rel"
  done <<< "$CHANGED_FILES_RAW"
}


deploy_vps_systemd_units() {
  if (( VPS_SYSTEMD_UNITS_CHANGED == 0 )); then
    VPS_SYSTEMD_UNITS_STATUS="não alterado"
    return 0
  fi

  STAGE="sincronização dos units systemd da VPS"
  local instalador="$REPO_DIR/updater/sistema/instalar.sh"
  if [[ ! -f "$instalador" ]]; then
    VPS_SYSTEMD_UNITS_STATUS="script ausente"
    LAST_ERROR_STDERR="updater/sistema/instalar.sh não foi encontrado"
    return 1
  fi

  local rc=0 template_overlay
  template_overlay="$(mktemp -d "${TMPDIR:-/tmp}/tts-bot-systemd-overlay.XXXXXX")"
  if ! build_vps_systemd_template_overlay "$template_overlay"; then
    rm -rf "$template_overlay"
    VPS_SYSTEMD_UNITS_STATUS="falha ao preparar templates"
    LAST_ERROR_STDERR="não foi possível preparar os templates systemd do patch"
    return 1
  fi

  if REPO_DIR="$REPO_DIR" TEMPLATE_DIR="$template_overlay" \
      bash "$instalador" --from-updater; then
    VPS_SYSTEMD_UNITS_STATUS="sincronizados"
    rm -rf "$template_overlay"
    return 0
  else
    rc=$?
  fi
  rm -rf "$template_overlay"

  VPS_SYSTEMD_UNITS_STATUS="falha ao sincronizar"
  LAST_ERROR_STDERR="o instalador das units systemd falhou; consulte o log do instalador e a restauração da migração"
  return "$rc"
}

deploy_alert_unit() {
  STAGE="configuração do alerta systemd"
  local src=""
  src="$(managed_systemd_template_source "bot-updater-alert@.service")"
  if [[ -n "$src" ]]; then
    cp "$src" /etc/systemd/system/bot-updater-alert@.service
    systemctl daemon-reload || true
    ALERT_UNIT_STATUS="unit instalada"
  else
    ALERT_UNIT_STATUS="unit ausente no deploy"
  fi
}

deploy_audio_services() {
  sanitize_vps_lavalink_units
  if (( AUDIO_SYSTEMD_CHANGED == 0 )); then
    AUDIO_SERVICES_STATUS="não alterado; Lavalink VPS sanitizado"
    return 0
  fi

  STAGE="configuração dos serviços de áudio"
  local installed=0 lavalink_unit_changed=0

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/lavalink\.service$'; then
    lavalink_unit_changed=1
    if env_truthy VPS_LAVALINK_ENABLED && [[ -f "$REPO_DIR/deploy/systemd/lavalink.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/lavalink.service" /etc/systemd/system/lavalink.service
      installed=1
    fi
  fi

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/tts-bot\.service$'; then
    if [[ -f "$REPO_DIR/deploy/systemd/tts-bot.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/tts-bot.service" /etc/systemd/system/tts-bot.service
      installed=1
    fi
  fi

  if (( installed == 1 )); then
    systemctl daemon-reload
  fi

  if (( lavalink_unit_changed == 1 )); then
    if env_truthy VPS_LAVALINK_ENABLED; then
      systemctl enable "$LAVALINK_SERVICE" >/dev/null 2>&1 || true
      systemctl restart "$LAVALINK_SERVICE" || true
      if systemctl is-active --quiet "$LAVALINK_SERVICE"; then
        AUDIO_SERVICES_STATUS="Lavalink ativo"
      else
        AUDIO_SERVICES_STATUS="Lavalink configurado, mas não ficou ativo"
      fi
    else
      AUDIO_SERVICES_STATUS="Lavalink VPS não iniciado; node de áudio roda no phone worker/Music Agent"
    fi
  else
    AUDIO_SERVICES_STATUS="units atualizadas; Lavalink não alterado"
  fi
}


deploy_cleanup_timer() {
  if (( CLEANUP_CHANGED == 0 )); then
    CLEANUP_STATUS="não alterada"
    return 0
  fi

  STAGE="configuração da limpeza de temporários"
  local installed=0

  local cleanup_service_src="" cleanup_timer_src=""
  cleanup_service_src="$(managed_systemd_template_source "cleanup-audio-temp.service")"
  cleanup_timer_src="$(managed_systemd_template_source "cleanup-audio-temp.timer")"

  if [[ -f "$cleanup_service_src" ]]; then
    cp "$cleanup_service_src" /etc/systemd/system/cleanup-audio-temp.service
    installed=1
  fi
  if [[ -f "$cleanup_timer_src" ]]; then
    cp "$cleanup_timer_src" /etc/systemd/system/cleanup-audio-temp.timer
    installed=1
  fi

  if (( installed == 0 )); then
    CLEANUP_STATUS="units de limpeza não encontrados"
    return 0
  fi

  systemctl daemon-reload
  systemctl enable --now cleanup-audio-temp.timer >/dev/null 2>&1 || true
  systemctl start cleanup-audio-temp.service >/dev/null 2>&1 || true

  if systemctl is-active --quiet cleanup-audio-temp.timer; then
    CLEANUP_STATUS="timer ativo"
  else
    CLEANUP_STATUS="timer instalado, mas não ativo"
  fi
}


deploy_phone_lavalink_watch() {
  # Lavalink/NodeLink foi removido do worker. Esta etapa não instala mais unit
  # nova; ela só desativa qualquer timer/service antigo que ainda exista na VPS.
  if (( PHONE_LAVALINK_WATCH_CHANGED == 0 )); then
    PHONE_LAVALINK_WATCH_STATUS="removido do fluxo"
    return 0
  fi

  STAGE="desativando watcher legado do Lavalink"
  systemctl disable --now phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
  systemctl reset-failed phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
  systemctl daemon-reload >/dev/null 2>&1 || true
  PHONE_LAVALINK_WATCH_STATUS="desativado/removido do fluxo"
}


deploy_phone_worker_watch() {
  if (( PHONE_WORKER_WATCH_CHANGED == 0 )); then
    PHONE_WORKER_WATCH_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do watcher do phone worker"
  local installed=0

  # Não chmod em script rastreado: systemd usa /usr/bin/env bash e isso evita repo sujo.
  local phone_worker_service_src="" phone_worker_timer_src=""
  phone_worker_service_src="$(managed_systemd_template_source "phone-worker-watch.service")"
  phone_worker_timer_src="$(managed_systemd_template_source "phone-worker-watch.timer")"

  if [[ -f "$phone_worker_service_src" ]]; then
    cp "$phone_worker_service_src" /etc/systemd/system/phone-worker-watch.service
    installed=1
  fi
  if [[ -f "$phone_worker_timer_src" ]]; then
    cp "$phone_worker_timer_src" /etc/systemd/system/phone-worker-watch.timer
    installed=1
  fi

  if (( installed == 0 )); then
    PHONE_WORKER_WATCH_STATUS="units não encontradas"
    return 0
  fi

  systemctl daemon-reload

  local worker_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    worker_value="$(grep -E '^PHONE_WORKER_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    worker_value="${worker_value,,}"
  fi

  local watch_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    watch_value="$(grep -E '^PHONE_WORKER_WATCH_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    watch_value="${watch_value,,}"
  fi

  if [[ "$watch_value" == "1" || "$watch_value" == "true" || "$watch_value" == "yes" || "$watch_value" == "on" || "$watch_value" == "sim" ]]; then
    systemctl enable --now phone-worker-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-worker-watch.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet phone-worker-watch.timer; then
      PHONE_WORKER_WATCH_STATUS="timer ativo"
    else
      PHONE_WORKER_WATCH_STATUS="timer instalado, mas não ativo"
    fi
  else
    systemctl disable --now phone-worker-watch.timer phone-worker-watch.service >/dev/null 2>&1 || true
    PHONE_WORKER_WATCH_STATUS="instalado; inativo até PHONE_WORKER_WATCH_ENABLED=true"
  fi
}


deploy_phone_worker_sync() {
  if (( PHONE_WORKER_SYNC_REQUIRED == 0 )); then
    PHONE_WORKER_SYNC_STATUS="sem mudanças"
    return 0
  fi

  if ! env_truthy PHONE_WORKER_LEGACY_SSH_SYNC_ENABLED; then
    PHONE_WORKER_SYNC_STATUS="agendado para automação por jobs após restart"
    return 0
  fi

  STAGE="sincronização legada do phone-worker por SSH"

  if [[ ! -x "$REPO_DIR/scripts/sync-phone-worker.sh" ]]; then
    PHONE_WORKER_SYNC_STATUS="não executado: scripts/sync-phone-worker.sh ausente"
    return 0
  fi

  local output status_line
  output="$(sudo -u ubuntu -H bash "$REPO_DIR/scripts/sync-phone-worker.sh" 2>&1 || true)"
  status_line="$(printf '%s\n' "$output" | grep -E '\[phone-worker-sync\]' | tail -n 1 | sed -E 's/^\[phone-worker-sync\][[:space:]]*//' || true)"

  if [[ -n "${status_line//[[:space:]]/}" ]]; then
    PHONE_WORKER_SYNC_STATUS="$status_line"
  else
    PHONE_WORKER_SYNC_STATUS="executado; sem status legível"
  fi

  logger -t "$LOG_TAG" "Phone-worker sync legado: $PHONE_WORKER_SYNC_STATUS"
  return 0
}

run_core_worker_post_update_automation() {
  if (( CORE_WORKER_AUTOMATION_REQUIRED == 0 )); then
    CORE_WORKER_AGENT_UPDATE_STATUS="sem mudanças"
    CORE_WORKER_APK_BUILD_STATUS="sem mudanças"
    CORE_WORKER_NOTIFY_STATUS="sem mudanças"
    return 0
  fi

  STAGE="automação pós-update dos Core Workers"
  local py
  py="$(current_bot_python_bin)"
  if [[ -z "$py" || ! -f "$REPO_DIR/scripts/core-worker-automation.py" ]]; then
    CORE_WORKER_AGENT_UPDATE_STATUS="não executado: core-worker-automation ausente"
    CORE_WORKER_APK_BUILD_STATUS="não executado"
    CORE_WORKER_NOTIFY_STATUS="não executado"
    return 0
  fi

  local output automation_rc=0
  output="$(sudo -u ubuntu -H env CORE_WORKER_CHANGED_FILES="$CHANGED_FILES_RAW" "$py" "$REPO_DIR/scripts/core-worker-automation.py" after-update 2>&1)" || automation_rc=$?
  logger -t "$LOG_TAG" "Core Worker automation: $output"

  local parsed
  parsed="$(CORE_WORKER_AUTOMATION_RAW="$output" python3 - <<'PYJSON' 2>/dev/null || true
import json, os
raw = os.environ.get('CORE_WORKER_AUTOMATION_RAW') or '{}'
line = next((ln for ln in reversed(raw.splitlines()) if ln.strip().startswith('{')), '{}')
try:
    data = json.loads(line)
except Exception:
    data = {}
agent = data.get('agent_update') or {}
apk = data.get('apk_build') or {}
def brief_agent(obj):
    if not obj:
        return 'sem mudanças'
    queued = len(obj.get('queued') or [])
    skipped = len(obj.get('skipped') or [])
    errors = len(obj.get('errors') or [])
    version = obj.get('target_version') or '?'
    return f'agent {version}: {queued} job(s), {skipped} skip, {errors} erro(s)'
def brief_apk(obj):
    if not obj:
        return 'sem mudanças'
    if obj.get('ok'):
        job = obj.get('job') or {}
        if job.get('job_id'):
            return f"APK {obj.get('versionName') or '?'}: build job {job.get('job_id')}"
        if obj.get('already_published'):
            return f"APK {obj.get('versionName') or '?'}: já publicado para esta fonte"
        return f"APK {obj.get('versionName') or '?'}: {obj.get('message') or obj.get('phase') or 'pendente'}"
    return f"APK {obj.get('versionName') or '?'}: pendente ({obj.get('message') or obj.get('error') or 'sem builder'})"
print(brief_agent(agent))
print(brief_apk(apk))
print('apps verão banner/notify quando latest.json novo for publicado' if apk else 'sem notificação nova')
PYJSON
)"
  CORE_WORKER_AGENT_UPDATE_STATUS="$(printf '%s\n' "$parsed" | sed -n '1p')"
  CORE_WORKER_APK_BUILD_STATUS="$(printf '%s\n' "$parsed" | sed -n '2p')"
  CORE_WORKER_NOTIFY_STATUS="$(printf '%s\n' "$parsed" | sed -n '3p')"
  [[ -n "${CORE_WORKER_AGENT_UPDATE_STATUS//[[:space:]]/}" ]] || CORE_WORKER_AGENT_UPDATE_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_APK_BUILD_STATUS//[[:space:]]/}" ]] || CORE_WORKER_APK_BUILD_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_NOTIFY_STATUS//[[:space:]]/}" ]] || CORE_WORKER_NOTIFY_STATUS="executado"
  if (( automation_rc != 0 )); then
    # Telefone offline/pending é um estado normal e o orquestrador retorna rc=0.
    # rc != 0 significa falha interna real: preserve o deploy do bot, mas não
    # pinte a automação como sucesso/degradação genérica.
    CORE_WORKER_AGENT_UPDATE_STATUS="FALHA INTERNA na automação (rc=$automation_rc) · ${CORE_WORKER_AGENT_UPDATE_STATUS}"
    CORE_WORKER_APK_BUILD_STATUS="FALHA INTERNA na automação (rc=$automation_rc) · ${CORE_WORKER_APK_BUILD_STATUS}"
    CORE_WORKER_NOTIFY_STATUS="falha interna persistida; deploy principal concluído sem rollback por worker offline"
    logger -t "$LOG_TAG" "Core Worker automation FALHOU internamente (rc=$automation_rc); deploy principal preservado"
  fi
  return 0
}


restart_bot_service_once() {
  local phase="deploy"
  local count=0
  if (( ROLLBACK_IN_PROGRESS == 1 )); then
    phase="rollback"
    count="$BOT_RESTARTS_ROLLBACK"
  else
    count="$BOT_RESTARTS_DEPLOY"
  fi

  if (( count >= 1 )); then
    LAST_ERROR_STDERR="restart do bot bloqueado: limite de 1 reinício na fase $phase já foi consumido"
    logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
    return 75
  fi

  # Consuma o orçamento antes da chamada: mesmo uma tentativa que pare o
  # processo e falhe ao subir não pode ser repetida indefinidamente.
  if [[ "$phase" == "rollback" ]]; then
    BOT_RESTARTS_ROLLBACK=$((BOT_RESTARTS_ROLLBACK + 1))
  else
    BOT_RESTARTS_DEPLOY=$((BOT_RESTARTS_DEPLOY + 1))
  fi

  # Um start-limit-hit anterior não deve impedir um único reinício legítimo.
  # O limite por execução acima evita transformar reset-failed em loop.
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE" || return $?
  logger -t "$LOG_TAG" "restart do bot executado: fase=$phase deploy=$BOT_RESTARTS_DEPLOY rollback=$BOT_RESTARTS_ROLLBACK" 2>/dev/null || true
  return 0
}

mark_deployment_committed() {
  DEPLOYMENT_COMMITTED=1
  STAGE="finalização pós-deploy"
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    write_local_candidate_state "deployment_completed" "${REMOTE_COMMIT:-}"
  fi
  logger -t "$LOG_TAG" "limite transacional concluído em $(short_commit "${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}")" 2>/dev/null || true
}

build_final_status_description() {
  local summary="${1:-}"
  local display_id="${2:-}"
  local short_from="${3:-}"
  local short_to="${4:-}"
  local changed_count="${5:-0}"
  local diff_summary="${6:-diff indisponível}"
  local apply_mode="${7:-aplicação concluída}"
  local duration="${8:-tempo indisponível}"
  local health_status="${9:-health não informado}"
  local file_count_text=""

  if [[ "$changed_count" =~ ^[0-9]+$ ]] && (( changed_count == 1 )); then
    file_count_text="1 arquivo alterado"
  elif [[ "$changed_count" =~ ^[0-9]+$ ]]; then
    file_count_text="$changed_count arquivos alterados"
  else
    file_count_text="arquivos alterados"
  fi

  # A string de formato é literal; IDs e commits são somente argumentos. Isso
  # impede que crases de Markdown virem substituição de comando do Bash.
  printf -v ZIP_STATUS_DESCRIPTION '%s\n\nAtualização `%s`\n`%s` → `%s`\n%s · %s\n%s · duração total: %s' \
    "$summary" "$display_id" "$short_from" "$short_to" "$file_count_text" \
    "$diff_summary" "$apply_mode" "$duration"
  # “OK” isolado não comunica nada e aparecia como uma linha solta no cartão.
  # Só acrescente saúde quando houver informação diferente do sucesso padrão.
  if [[ -n "${health_status//[[:space:]]/}" && "$health_status" != "OK" ]]; then
    ZIP_STATUS_DESCRIPTION+=$'\n\n'"Saúde: $health_status"
  fi
}

deploy_bot() {
  # Caminho rápido: não reinstale systemd/watchers/áudio em todo update.
  # Cada rotina só roda quando os arquivos dela mudaram; isso reduz bastante
  # o custo dos patches comuns.
  if (( VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    normalize_healthcheck_crontab
    deploy_vps_systemd_units
  fi
  if (( ALERT_CHANGED == 1 || VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    deploy_alert_unit
  fi
  if (( AUDIO_SYSTEMD_CHANGED == 1 )); then
    deploy_audio_services
  fi
  if (( CLEANUP_CHANGED == 1 )); then
    deploy_cleanup_timer
  fi
  if (( PHONE_LAVALINK_WATCH_CHANGED == 1 )); then
    deploy_phone_lavalink_watch
  fi
  if (( PHONE_WORKER_WATCH_CHANGED == 1 )); then
    deploy_phone_worker_watch
  fi
  if (( PHONE_WORKER_SYNC_REQUIRED == 1 )); then
    deploy_phone_worker_sync
  fi

  if (( REQUIREMENTS_CHANGED == 1 && ROLLBACK_IN_PROGRESS == 0 )); then
    STAGE="ativação do runtime Python"
    if (( LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
      local runtime_commit="${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-}}"
      if (( LOCAL_CANDIDATE_RUNTIME_READY == 0 )); then
        hydrate_local_candidate_runtime_artifacts "$runtime_commit" || {
          LAST_ERROR_STDERR="runtime Python candidato READY ausente após promoção"
          LAST_ERROR_CODE="PYTHON_RUNTIME_READY_MISSING"
          return 1
        }
      fi
      if (( LOCAL_CANDIDATE_PYTHON_READY == 0 )) || ! verify_local_candidate_artifact_integrity python; then
        LAST_ERROR_STDERR="runtime Python candidato ausente ou divergiu da validação READY"
        LAST_ERROR_CODE="PYTHON_RUNTIME_READY_INVALID"
        return 1
      fi
      if ! activate_python_runtime_release "$runtime_commit"; then
        return 1
      fi
    else
      # Compatibilidade de operações administrativas legadas fora dos caminhos
      # transacionais local/remoto. Updates normais nunca instalam na .venv live.
      local legacy_py
      legacy_py="$(current_bot_python_bin)"
      local legacy_requirements
      legacy_requirements="$(python_runtime_install_requirements_file "$REPO_DIR" || true)"
      if [[ -x "$legacy_py" && -n "$legacy_requirements" && -f "$legacy_requirements" ]]; then
        sudo -u ubuntu -H "$legacy_py" -m pip install --disable-pip-version-check --prefer-binary --no-input --progress-bar off -r "$legacy_requirements"
      fi
    fi
  fi

  if (( BOT_CHANGED == 1 )); then
    local fast_modules restart_epoch restarts_before
    fast_modules="$(fast_reload_modules_for_changed_files || true)"
    FAST_RELOAD_MODULES="$fast_modules"
    if [[ -n "${fast_modules//[[:space:]]/}" ]]; then
      STAGE="reload rápido de cogs"
      if try_fast_cog_reload "$fast_modules" "$APP_COMMANDS_MAY_HAVE_CHANGED"; then
        return 0
      fi
      logger -t "$LOG_TAG" "Fast reload indisponível; usando restart completo seguro do bot principal. Status: $FAST_RELOAD_STATUS"
      if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
        zip_progress_done "Reload da cog falhou"
        zip_progress_publish "Reiniciando processo: bot"
      fi
      FAST_RELOAD_MODULES=""
    fi

    restarts_before="$(service_restart_count "$SERVICE")"

    STAGE="reinício do bot"
    restart_epoch="$(date +%s)"
    local bot_phase_started_ms bot_phase_finished_ms
    bot_phase_started_ms="$(update_now_ms)"
    restart_bot_service_once
    bot_phase_finished_ms="$(update_now_ms)"
    append_update_timing_ms "bot.restart_command" "$((bot_phase_finished_ms - bot_phase_started_ms))"

    if env_truthy LAVALINK_ENABLED; then
      STAGE="espera curta do Lavalink"
      bot_phase_started_ms="$(update_now_ms)"
      wait_for_lavalink_ready || true
      bot_phase_finished_ms="$(update_now_ms)"
      append_update_timing_ms "bot.lavalink_wait" "$((bot_phase_finished_ms - bot_phase_started_ms))"
    fi

    STAGE="validação fatal do bot"
    local health_profile
    health_profile="$(bot_health_profile_for_changed_files)"
    verify_bot_after_restart "$restart_epoch" "$restarts_before" 1 "$health_profile"
    return $?
  fi

  STAGE="healthcheck do bot"
  if refresh_bot_health_status; then
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK com avisos"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="OK"
    fi
  else
    if [[ "$BOT_HEALTH_DETAIL_STATUS" == "HTTP sem resposta" ]]; then
      BOT_HEALTHCHECK_STATUS="não alterado; health HTTP sem resposta"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="falhou: health não saudável ($BOT_HEALTH_DETAIL_STATUS)"
      return 1
    fi
  fi
  return 0
}


frontend_release_root_for_key() {
  local key
  key="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$key" ]] || return 1
  local root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}"
  printf '%s/%s\n' "$root" "$key"
}

write_frontend_release_manifest() {
  local root="${1:?}" key="${2:?}"
  FRONT_RELEASE_DIR="$root" FRONT_RELEASE_KEY="$key" python3 - <<'PYFRONTRELEASEWRITE'
import datetime, hashlib, json, os, pathlib
root = pathlib.Path(os.environ['FRONT_RELEASE_DIR'])

def tree_hash(base: pathlib.Path) -> str:
    digest = hashlib.sha256()
    resolved_base = base.resolve()
    for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
        if item.name == '.tts-release.json':
            continue
        rel = item.relative_to(base).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(1)
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(resolved_base)
            except ValueError:
                raise SystemExit(1)
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

if not (root / 'index.html').is_file():
    raise SystemExit(1)
payload = {
    'state': 'ready',
    'release_key': os.environ['FRONT_RELEASE_KEY'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'sha256': tree_hash(root),
}
tmp = root / '.tts-release.json.tmp'
final = root / '.tts-release.json'
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, final)
PYFRONTRELEASEWRITE
}

verify_frontend_release() {
  local root="${1:?}" expected_key="${2:-}" manifest
  manifest="$root/.tts-release.json"
  [[ -d "$root" && ! -L "$root" && -s "$root/index.html" && -s "$manifest" && ! -L "$manifest" ]] || return 1
  FRONT_RELEASE_DIR="$root" FRONT_RELEASE_MANIFEST="$manifest" FRONT_RELEASE_EXPECTED_KEY="$expected_key" python3 - <<'PYFRONTRELEASEVERIFY' >/dev/null 2>&1
import hashlib, json, os, pathlib
root = pathlib.Path(os.environ['FRONT_RELEASE_DIR'])
manifest = pathlib.Path(os.environ['FRONT_RELEASE_MANIFEST'])
data = json.loads(manifest.read_text(encoding='utf-8'))
if data.get('state') != 'ready':
    raise SystemExit(1)
expected = os.environ.get('FRONT_RELEASE_EXPECTED_KEY') or ''
if expected and str(data.get('release_key') or '') != expected:
    raise SystemExit(1)

def tree_hash(base: pathlib.Path) -> str:
    digest = hashlib.sha256()
    resolved_base = base.resolve()
    for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
        if item.name == '.tts-release.json':
            continue
        rel = item.relative_to(base).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(1)
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(resolved_base)
            except ValueError:
                raise SystemExit(1)
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

if tree_hash(root) != str(data.get('sha256') or ''):
    raise SystemExit(1)
PYFRONTRELEASEVERIFY
}

frontend_active_release_key() {
  [[ -L "$FRONT_PUBLISH_DIR" ]] || return 1
  local target root resolved
  target="$(readlink -f -- "$FRONT_PUBLISH_DIR" 2>/dev/null || true)"
  root="$(readlink -f -- "${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}" 2>/dev/null || true)"
  [[ -n "$target" && -n "$root" && "$target" == "$root"/* ]] || return 1
  resolved="$(basename "$target")"
  [[ -n "$resolved" ]] || return 1
  verify_frontend_release "$target" "$resolved" || return 1
  printf '%s\n' "$resolved"
}

frontend_publication_is_healthy() {
  [[ -e "$FRONT_PUBLISH_DIR" || -L "$FRONT_PUBLISH_DIR" ]] || return 1
  [[ -s "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  [[ -r "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  if [[ -L "$FRONT_PUBLISH_DIR" ]]; then
    frontend_active_release_key >/dev/null 2>&1 || return 1
  fi
  return 0
}

adopt_frontend_publication_as_release() {
  local baseline_key="${1:-}" release_root release_dir parent tmp_link
  if frontend_active_release_key >/dev/null 2>&1; then
    frontend_active_release_key
    return 0
  fi
  [[ -d "$FRONT_PUBLISH_DIR" && ! -L "$FRONT_PUBLISH_DIR" && -s "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  baseline_key="$(sanitize_commit_ref "$baseline_key")"
  [[ -n "$baseline_key" ]] || return 1
  release_root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}"
  release_dir="$(frontend_release_root_for_key "$baseline_key")" || return 1
  parent="$(dirname "$FRONT_PUBLISH_DIR")"
  install -d -m 0755 "$release_root" || return 1
  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    if ! verify_frontend_release "$release_dir" "$baseline_key"; then
      return 1
    fi
    # Só descarta a árvore física se ela for byte-a-byte equivalente ao release já existente.
    local live_hash release_hash
    live_hash="$(FRONT_RELEASE_DIR="$FRONT_PUBLISH_DIR" python3 - <<'PYFRONTLIVEHASH'
import hashlib, os, pathlib
base = pathlib.Path(os.environ['FRONT_RELEASE_DIR'])
resolved_base = base.resolve()
d = hashlib.sha256()
for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
    if item.name == '.tts-release.json':
        continue
    rel = item.relative_to(base).as_posix()
    if item.is_symlink():
        target = os.readlink(item)
        if os.path.isabs(target):
            raise SystemExit(1)
        resolved = (item.parent / target).resolve(strict=False)
        try:
            resolved.relative_to(resolved_base)
        except ValueError:
            raise SystemExit(1)
        d.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
        continue
    if not item.is_file():
        continue
    d.update(b'F\0' + rel.encode() + b'\0')
    with item.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            d.update(chunk)
print(d.hexdigest())
PYFRONTLIVEHASH
)"
    release_hash="$(FRONT_RELEASE_MANIFEST="$release_dir/.tts-release.json" python3 - <<'PYFRONTHASHREAD'
import json, os
print(json.load(open(os.environ['FRONT_RELEASE_MANIFEST'], encoding='utf-8')).get('sha256',''))
PYFRONTHASHREAD
)"
    [[ -n "$live_hash" && "$live_hash" == "$release_hash" ]] || return 1
    rm -rf -- "$FRONT_PUBLISH_DIR" || return 1
  else
    # Registra o hash ainda no path live; o mv seguinte é rename no mesmo filesystem.
    write_frontend_release_manifest "$FRONT_PUBLISH_DIR" "$baseline_key" || return 1
    if ! mv -- "$FRONT_PUBLISH_DIR" "$release_dir"; then
      rm -f -- "$FRONT_PUBLISH_DIR/.tts-release.json" 2>/dev/null || true
      return 1
    fi
  fi
  tmp_link="$parent/.sinuca-current.$$.${RANDOM}"
  ln -s -- "$release_dir" "$tmp_link" || return 1
  if ! mv -Tf -- "$tmp_link" "$FRONT_PUBLISH_DIR"; then
    rm -f -- "$tmp_link" 2>/dev/null || true
    if [[ ! -e "$FRONT_PUBLISH_DIR" && -d "$release_dir" ]]; then
      mv -- "$release_dir" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    return 1
  fi
  verify_frontend_release "$release_dir" "$baseline_key" || return 1
  printf '%s\n' "$baseline_key"
}

prepare_frontend_release() {
  local source_dir="${1:?}" release_key="${2:?}" release_root release_dir tmp copy_mode="copy"
  release_key="$(sanitize_commit_ref "$release_key")"
  [[ -n "$release_key" && -s "$source_dir/index.html" ]] || return 1
  release_root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}"
  release_dir="$(frontend_release_root_for_key "$release_key")" || return 1
  if verify_frontend_release "$release_dir" "$release_key"; then
    printf '%s\n' "$release_dir"
    return 0
  fi
  install -d -m 0755 "$release_root" || return 1
  tmp="$(mktemp -d "$release_root/.${release_key}.XXXXXX")" || return 1
  if cp -al -- "$source_dir/." "$tmp/" 2>/dev/null; then
    copy_mode="hardlink"
  else
    rm -rf -- "$tmp"/* "$tmp"/.[!.]* "$tmp"/..?* 2>/dev/null || true
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete "$source_dir/" "$tmp/" || { rm -rf -- "$tmp"; return 1; }
    else
      cp -a -- "$source_dir/." "$tmp/" || { rm -rf -- "$tmp"; return 1; }
    fi
  fi
  [[ -s "$tmp/index.html" ]] || { rm -rf -- "$tmp"; return 1; }
  find "$tmp" -type d -exec chmod 0755 {} + 2>/dev/null || true
  find "$tmp" -type f -exec chmod 0644 {} + 2>/dev/null || true
  write_frontend_release_manifest "$tmp" "$release_key" || { rm -rf -- "$tmp"; return 1; }
  verify_frontend_release "$tmp" "$release_key" || { rm -rf -- "$tmp"; return 1; }
  rm -rf -- "$release_dir" 2>/dev/null || true
  mv -- "$tmp" "$release_dir" || { rm -rf -- "$tmp"; return 1; }
  logger -t "$LOG_TAG" "frontend release $(short_commit "$release_key") preparada via $copy_mode" 2>/dev/null || true
  printf '%s\n' "$release_dir"
}

activate_frontend_release() {
  local release_key="${1:?}" release_dir parent tmp_link backup="" had_physical=0
  release_key="$(sanitize_commit_ref "$release_key")"
  release_dir="$(frontend_release_root_for_key "$release_key")" || return 1
  verify_frontend_release "$release_dir" "$release_key" || return 1
  parent="$(dirname "$FRONT_PUBLISH_DIR")"
  install -d -m 0755 "$parent" || return 1
  tmp_link="$parent/.sinuca-current.$$.${RANDOM}"
  ln -s -- "$release_dir" "$tmp_link" || return 1
  if [[ -d "$FRONT_PUBLISH_DIR" && ! -L "$FRONT_PUBLISH_DIR" ]]; then
    backup="$parent/.sinuca-legacy.$$.${RANDOM}"
    mv -- "$FRONT_PUBLISH_DIR" "$backup" || { rm -f -- "$tmp_link"; return 1; }
    had_physical=1
  fi
  if ! mv -Tf -- "$tmp_link" "$FRONT_PUBLISH_DIR"; then
    rm -f -- "$tmp_link" 2>/dev/null || true
    if (( had_physical == 1 )); then
      mv -- "$backup" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    return 1
  fi
  if ! frontend_publication_is_healthy; then
    rm -f -- "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    if (( had_physical == 1 )); then
      mv -- "$backup" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    return 1
  fi
  (( had_physical == 0 )) || rm -rf -- "$backup" 2>/dev/null || true
  return 0
}

publish_frontend_atomically() {
  local source_dir="${1:?}" release_key="${2:-}"
  if [[ -z "$release_key" ]]; then
    release_key="${LOCAL_CANDIDATE_ARTIFACT_COMMIT:-${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-}}}"
  fi
  if [[ -z "$release_key" ]]; then
    release_key="$(repo_git rev-parse HEAD 2>/dev/null || true)"
  fi
  release_key="$(sanitize_commit_ref "$release_key")"
  [[ -n "$release_key" ]] || {
    FRONT_STATUS="não foi possível determinar a chave da release frontend"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  }
  prepare_frontend_release "$source_dir" "$release_key" >/dev/null || {
    FRONT_STATUS="falha ao preparar release imutável do frontend"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  }
  if ! activate_frontend_release "$release_key"; then
    FRONT_STATUS="não foi possível ativar release frontend $(short_commit "$release_key")"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  fi
  return 0
}

collect_protected_frontend_release_keys() {
  local active=""
  active="$(frontend_active_release_key 2>/dev/null || true)"
  [[ -n "$active" ]] && printf '%s\n' "$active"
  [[ -d "${RUNTIME_RELEASE_ROOT:-}" ]] || return 0
  find "$RUNTIME_RELEASE_ROOT" -mindepth 2 -maxdepth 2 -type f -name release.json -print0 2>/dev/null | while IFS= read -r -d '' manifest; do
    FRONT_RUNTIME_MANIFEST="$manifest" python3 - <<'PYFRONTREFS' 2>/dev/null || true
import json, os
try:
    data = json.load(open(os.environ['FRONT_RUNTIME_MANIFEST'], encoding='utf-8'))
except Exception:
    raise SystemExit(0)
record = data.get('frontend') if isinstance(data, dict) else None
if isinstance(record, dict):
    key = str(record.get('release_key') or '')
    if key:
        print(key)
PYFRONTREFS
  done
}

prune_frontend_releases() {
  local root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}" keep="${FRONT_RELEASE_RETENTION:-4}" count=0 entry key
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=4
  (( keep >= 1 )) || keep=1
  [[ -d "$root" ]] || return 0
  declare -A protected=()
  while IFS= read -r key; do
    [[ -n "$key" ]] && protected["$key"]=1
  done < <(collect_protected_frontend_release_keys | sort -u)
  while IFS= read -r entry; do
    [[ -d "$entry" ]] || continue
    key="$(basename "$entry")"
    if [[ -n "${protected[$key]:-}" ]]; then
      continue
    fi
    count=$((count + 1))
    if (( count > keep )); then
      rm -rf -- "$entry" 2>/dev/null || true
    fi
  done < <(find "$root" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | cut -d' ' -f2-)
}

deploy_frontend() {
  local repair_mode=0

  if (( FRONT_CHANGED == 0 )); then
    if frontend_publication_is_healthy; then
      if (( ${FRONT_TESTS_CHANGED:-0} == 1 )); then
        FRONT_STATUS="testes do frontend aprovados no worktree; publicação não necessária"
      else
        FRONT_STATUS="não alterado"
      fi
      return 0
    fi
    # Mesmo sem arquivos do frontend no patch, restaura automaticamente uma
    # publicação ausente/corrompida. Candidatos transacionais normalmente já
    # terão preparado esse reparo no worktree antes da promoção.
    FRONT_CHANGED=1
    repair_mode=1
    logger -t "$LOG_TAG" "publicação do frontend inválida; reconstrução automática solicitada"
  fi

  if (( (LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1) && FRONT_CHANGED == 1 && ROLLBACK_IN_PROGRESS == 0 )); then
    if (( LOCAL_CANDIDATE_RUNTIME_READY == 0 )); then
      hydrate_local_candidate_runtime_artifacts "${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-$(repo_git rev-parse HEAD)}}" || true
    fi
    if [[ -z "${LOCAL_CANDIDATE_FRONTEND_ARTIFACT:-}" || ! -s "$LOCAL_CANDIDATE_FRONTEND_ARTIFACT/index.html" ]]; then
      FRONT_STATUS="artefato frontend READY ausente; recusando rebuild no checkout live"
      LAST_ERROR_STDERR="$FRONT_STATUS"
      LAST_ERROR_CODE="FRONTEND_PREBUILT_ARTIFACT_MISSING"
      return 1
    fi
    if ! verify_local_candidate_artifact_integrity frontend; then
      FRONT_STATUS="artefato frontend READY divergiu do hash validado"
      LAST_ERROR_STDERR="$FRONT_STATUS"
      LAST_ERROR_CODE="FRONTEND_PREBUILT_ARTIFACT_INVALID"
      return 1
    fi
    STAGE="publicação do frontend"
    zip_progress_publish "Publicando interface" "Ativando artefato já validado"
    if ! publish_frontend_atomically "$LOCAL_CANDIDATE_FRONTEND_ARTIFACT"; then
      return 1
    fi
    FRONT_RUNTIME_MUTATED=1
    FRONT_STATUS="frontend publicado a partir do artefato READY do worktree"
    zip_progress_done "Interface publicada"
    return 0
  fi

  if [[ ! -d "$FRONT_DIR" ]]; then
    FRONT_STATUS="frontend não encontrado em $FRONT_DIR"
    return 1
  fi

  STAGE="dependências do frontend"
  zip_progress_run_as_ubuntu \
    "Instalando dependências" \
    "Verificando pacotes" \
    "cd \"$FRONT_DIR\" && if [ -f package-lock.json ]; then npm ci $NPM_INSTALL_FLAGS; else npm install $NPM_INSTALL_FLAGS; fi"
  zip_progress_done_and_publish \
    "Dependências instaladas" \
    "Compilando interface" \
    "Gerando os arquivos de produção"

  STAGE="testes do frontend"
  zip_progress_run_as_ubuntu \
    "Validando interface" \
    "Executando testes do site" \
    "cd \"$FRONT_DIR\" && npm test"
  zip_progress_done_and_publish \
    "Testes da interface aprovados" \
    "Compilando interface" \
    "Gerando os arquivos de produção"

  STAGE="build do frontend"
  zip_progress_run_as_ubuntu \
    "Compilando interface" \
    "Gerando os arquivos de produção" \
    "cd \"$FRONT_DIR\" && npm run build"
  zip_progress_done_and_publish \
    "Interface compilada" \
    "Publicando interface" \
    "Atualizando arquivos"

  STAGE="publicação do frontend"
  if [[ ! -s "$FRONT_DIR/dist/index.html" ]]; then
    FRONT_STATUS="build do frontend não produziu dist/index.html"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  fi
  if ! publish_frontend_atomically "$FRONT_DIR/dist"; then
    return 1
  fi
  FRONT_RUNTIME_MUTATED=1

  STAGE="limpeza do frontend"
  zip_progress_run_as_ubuntu \
    "Publicando interface" \
    "Limpando temporários" \
    "cd \"$FRONT_DIR\" && rm -rf node_modules"
  zip_progress_done "Interface publicada"

  if (( repair_mode == 1 )); then
    FRONT_STATUS="frontend reconstruído e republicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  else
    FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  fi
  return 0
}

install_backend_prebuilt_artifact() {
  local source_dir="${1:?}" token temp_dist temp_modules backup_dist backup_modules had_dist=0 had_modules=0
  local deps_key deps_layer
  [[ -s "$source_dir/dist/index.js" && -s "$source_dir/deps.json" ]] || {
    LAST_ERROR_STDERR="artefato backend incompleto: $source_dir"
    return 1
  }
  [[ -d "$BACK_DIR" ]] || {
    LAST_ERROR_STDERR="backend live não encontrado em $BACK_DIR"
    return 1
  }

  deps_key="$(BACK_DEPS_FILE="$source_dir/deps.json" python3 - <<'PYBACKINSTALLDEPS' 2>/dev/null
import json, os
with open(os.environ['BACK_DEPS_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
key = str(data.get('deps_key') or '')
if len(key) != 64:
    raise SystemExit(1)
print(key)
PYBACKINSTALLDEPS
)" || {
    LAST_ERROR_STDERR="artefato backend possui deps_key inválido"
    return 1
  }
  deps_layer="$(node_dependency_layer_root backend prod "$deps_key")" || return 1
  if ! verify_node_dependency_layer "$deps_layer" "$deps_key" prod; then
    LAST_ERROR_STDERR="dependency layer backend ausente ou inválido: ${deps_key:0:12}"
    return 1
  fi

  token="${UPDATE_RUNTIME_RUN_ID//[^[:alnum:]._-]/_}"
  temp_dist="$BACK_DIR/.update-dist-$token"
  temp_modules="$BACK_DIR/.update-node_modules-$token"
  backup_dist="$BACK_DIR/.previous-dist-$token"
  backup_modules="$BACK_DIR/.previous-node_modules-$token"

  sudo -u ubuntu -H rm -rf -- "$temp_dist" "$temp_modules" "$backup_dist" "$backup_modules"
  sudo -u ubuntu -H cp -a -- "$source_dir/dist" "$temp_dist" || return 1
  sudo -u ubuntu -H ln -s -- "$deps_layer/node_modules" "$temp_modules" || {
    sudo -u ubuntu -H rm -rf -- "$temp_dist" "$temp_modules"
    return 1
  }

  if [[ -e "$BACK_DIR/dist" || -L "$BACK_DIR/dist" ]]; then
    sudo -u ubuntu -H mv -- "$BACK_DIR/dist" "$backup_dist" || return 1
    had_dist=1
  fi
  if [[ -e "$BACK_DIR/node_modules" || -L "$BACK_DIR/node_modules" ]]; then
    if ! sudo -u ubuntu -H mv -- "$BACK_DIR/node_modules" "$backup_modules"; then
      (( had_dist == 1 )) && sudo -u ubuntu -H mv -- "$backup_dist" "$BACK_DIR/dist" 2>/dev/null || true
      return 1
    fi
    had_modules=1
  fi

  if ! sudo -u ubuntu -H mv -- "$temp_dist" "$BACK_DIR/dist"; then
    (( had_modules == 1 )) && sudo -u ubuntu -H mv -- "$backup_modules" "$BACK_DIR/node_modules" 2>/dev/null || true
    (( had_dist == 1 )) && sudo -u ubuntu -H mv -- "$backup_dist" "$BACK_DIR/dist" 2>/dev/null || true
    sudo -u ubuntu -H rm -rf -- "$temp_dist" "$temp_modules"
    return 1
  fi
  if ! sudo -u ubuntu -H mv -- "$temp_modules" "$BACK_DIR/node_modules"; then
    sudo -u ubuntu -H rm -rf -- "$BACK_DIR/dist" "$temp_modules" 2>/dev/null || true
    (( had_modules == 1 )) && sudo -u ubuntu -H mv -- "$backup_modules" "$BACK_DIR/node_modules" 2>/dev/null || true
    (( had_dist == 1 )) && sudo -u ubuntu -H mv -- "$backup_dist" "$BACK_DIR/dist" 2>/dev/null || true
    return 1
  fi

  sudo -u ubuntu -H rm -rf -- "$backup_dist" "$backup_modules" 2>/dev/null || true
  return 0
}

deploy_backend() {
  if (( BACK_CHANGED == 0 && FRONT_CHANGED == 0 )); then
    if (( ${BACK_TESTS_CHANGED:-0} == 1 )); then
      BACK_STATUS="testes do backend aprovados no worktree; publicação não necessária"
    else
      BACK_STATUS="não alterado"
    fi
    ACTIVITY_HEALTHCHECK_STATUS="não alterada"
    return 0
  fi

  if (( BACK_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    STAGE="healthcheck informativo do painel web"
    zip_progress_publish "Validando" "Confirmando disponibilidade"
    if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 8 1; else wait_for_health "$BACK_HEALTH_URL" 3 2; fi; }; then
      ACTIVITY_HEALTHCHECK_STATUS="OK"
      zip_progress_done "Validação concluída"
    else
      ACTIVITY_HEALTHCHECK_STATUS="indisponível; backend não foi alterado"
      zip_progress_done "Publicação concluída; validação indisponível"
    fi
    return 0
  fi

  if (( (LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1) && BACK_CHANGED == 1 && ROLLBACK_IN_PROGRESS == 0 )); then
    if (( LOCAL_CANDIDATE_RUNTIME_READY == 0 )); then
      hydrate_local_candidate_runtime_artifacts "${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-$(repo_git rev-parse HEAD)}}" || true
    fi
    if [[ -z "${LOCAL_CANDIDATE_BACKEND_ARTIFACT:-}" || ! -s "$LOCAL_CANDIDATE_BACKEND_ARTIFACT/dist/index.js" || ! -s "$LOCAL_CANDIDATE_BACKEND_ARTIFACT/deps.json" ]]; then
      BACK_STATUS="artefato backend READY ausente; recusando rebuild no checkout live"
      LAST_ERROR_STDERR="$BACK_STATUS"
      LAST_ERROR_CODE="BACKEND_PREBUILT_ARTIFACT_MISSING"
      return 1
    fi
    if ! verify_local_candidate_artifact_integrity backend; then
      BACK_STATUS="artefato backend READY divergiu do hash validado"
      LAST_ERROR_STDERR="$BACK_STATUS"
      LAST_ERROR_CODE="BACKEND_PREBUILT_ARTIFACT_INVALID"
      return 1
    fi
    STAGE="publicação do backend"
    zip_progress_publish "Publicando servidor" "Ativando artefato já validado"
    if ! install_backend_prebuilt_artifact "$LOCAL_CANDIDATE_BACKEND_ARTIFACT"; then
      BACK_STATUS="falha ao ativar artefato backend pré-compilado"
      LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-$BACK_STATUS}"
      LAST_ERROR_CODE="BACKEND_PUBLISH_FAILED"
      return 1
    fi
    BACK_RUNTIME_MUTATED=1

    STAGE="reinício do backend"
    if ! systemctl cat "$BACK_SERVICE" >/dev/null 2>&1; then
      BACK_STATUS="serviço systemd ausente: $BACK_SERVICE"
      LAST_ERROR_STDERR="$BACK_STATUS"
      return 1
    fi
    systemctl reset-failed "$BACK_SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$BACK_SERVICE"
    if ! wait_for_service_active "$BACK_SERVICE" 20 0.25; then
      BACK_STATUS="serviço não ficou ativo: $BACK_SERVICE"
      LAST_ERROR_STDERR="$(journalctl -u "$BACK_SERVICE" -n 30 --no-pager 2>/dev/null | trim_alert_text 1800)"
      return 1
    fi
    zip_progress_done_and_publish "Servidor reiniciado" "Validando" "Aguardando resposta"

    STAGE="healthcheck do painel web"
    if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2; else wait_for_health "$BACK_HEALTH_URL" 8 3; fi; }; then
      ACTIVITY_HEALTHCHECK_STATUS="OK"
      BACK_STATUS="backend publicado a partir do artefato READY e validado em $BACK_HEALTH_URL"
      zip_progress_done "Validação concluída"
      return 0
    fi
    ACTIVITY_HEALTHCHECK_STATUS="falhou"
    BACK_STATUS="backend pré-compilado ativado, mas healthcheck falhou em $BACK_HEALTH_URL"
    return 1
  fi

  if [[ ! -d "$BACK_DIR" ]]; then
    BACK_STATUS="backend não encontrado em $BACK_DIR"
    return 1
  fi

  # npm ci altera node_modules do runtime live antes mesmo do restart. Se
  # qualquer etapa falhar a partir daqui, o rollback precisa restaurar a
  # release preservada em vez de recompilar a versão anterior.
  BACK_RUNTIME_MUTATED=1
  STAGE="dependências do backend"
  zip_progress_run_as_ubuntu \
    "Preparando servidor" \
    "Verificando pacotes" \
    "cd \"$BACK_DIR\" && if [ -f package-lock.json ]; then npm ci $NPM_INSTALL_FLAGS; else npm install $NPM_INSTALL_FLAGS; fi"
  zip_progress_done_and_publish \
    "Servidor preparado" \
    "Compilando servidor" \
    "Gerando arquivos"

  STAGE="testes do backend"
  zip_progress_run_as_ubuntu \
    "Validando servidor" \
    "Executando testes do dashboard" \
    "cd \"$BACK_DIR\" && npm test"
  zip_progress_done_and_publish \
    "Testes do servidor aprovados" \
    "Compilando servidor" \
    "Gerando arquivos"

  STAGE="build do backend"
  zip_progress_run_as_ubuntu \
    "Compilando servidor" \
    "Gerando arquivos" \
    "cd \"$BACK_DIR\" && npm run build && npm prune --omit=dev --no-audit --no-fund --progress=false"
  zip_progress_done_and_publish \
    "Servidor compilado" \
    "Reiniciando servidor" \
    "Aplicando nova versão"

  STAGE="reinício do backend"
  if ! systemctl cat "$BACK_SERVICE" >/dev/null 2>&1; then
    BACK_STATUS="serviço systemd ausente: $BACK_SERVICE"
    LAST_ERROR_STDERR="$BACK_STATUS"
    return 1
  fi
  systemctl reset-failed "$BACK_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$BACK_SERVICE"
  if ! wait_for_service_active "$BACK_SERVICE" 20 0.25; then
    BACK_STATUS="serviço não ficou ativo: $BACK_SERVICE"
    LAST_ERROR_STDERR="$(journalctl -u "$BACK_SERVICE" -n 30 --no-pager 2>/dev/null | trim_alert_text 1800)"
    return 1
  fi
  BACK_STATUS="backend reiniciado pelo systemd na porta $BACK_PORT"
  zip_progress_done_and_publish \
    "Servidor reiniciado" \
    "Validando" \
    "Aguardando resposta"

  STAGE="healthcheck do painel web"
  if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2; else wait_for_health "$BACK_HEALTH_URL" 8 3; fi; }; then
    ACTIVITY_HEALTHCHECK_STATUS="OK"
    BACK_STATUS="backend publicado e validado em $BACK_HEALTH_URL"
    zip_progress_done "Validação concluída"
    return 0
  fi

  ACTIVITY_HEALTHCHECK_STATUS="falhou"
  BACK_STATUS="backend reiniciado, mas healthcheck falhou em $BACK_HEALTH_URL"
  return 1
}


runtime_release_root_for_commit() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$commit" ]] || return 1
  printf '%s/%s\n' "$RUNTIME_RELEASE_ROOT" "$commit"
}

write_runtime_release_manifest() {
  local root="${1:?}" commit="${2:?}" front_ready="${3:-0}" back_ready="${4:-0}"
  RELEASE_ROOT="$root" RELEASE_COMMIT="$commit" RELEASE_FRONT_READY="$front_ready" RELEASE_BACK_READY="$back_ready" \
    python3 - <<'PYRUNTIMERELEASE'
import datetime, hashlib, json, os, pathlib
root = pathlib.Path(os.environ['RELEASE_ROOT'])

def tree_hash(path: pathlib.Path) -> str:
    if not path.exists():
        return ''
    digest = hashlib.sha256()
    base = path.resolve()
    for item in sorted(path.rglob('*'), key=lambda p: p.as_posix()):
        rel = item.relative_to(path).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(f'release contains absolute symlink: {rel}')
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(base)
            except ValueError:
                raise SystemExit(f'release symlink escapes root: {rel}')
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

front_ready = os.environ.get('RELEASE_FRONT_READY') == '1'
front_release_key = ''
if front_ready:
    ref = root / 'frontend.json'
    if ref.is_file() and not ref.is_symlink():
        data = json.loads(ref.read_text(encoding='utf-8'))
        front_release_key = str(data.get('release_key') or '')
        if not front_release_key:
            raise SystemExit('frontend runtime release missing release_key')
    elif (root / 'frontend').is_dir():
        # Compatibilidade para releases criados antes do frontend por referência.
        front_release_key = ''
    else:
        raise SystemExit('frontend runtime release missing reference')

back_ready = os.environ.get('RELEASE_BACK_READY') == '1'
back_deps_key = ''
if back_ready:
    deps_file = root / 'backend' / 'deps.json'
    if not deps_file.is_file() or deps_file.is_symlink():
        raise SystemExit('backend release missing deps.json')
    deps = json.loads(deps_file.read_text(encoding='utf-8'))
    back_deps_key = str(deps.get('deps_key') or '')
    if len(back_deps_key) != 64:
        raise SystemExit('backend release has invalid deps_key')

payload = {
    'state': 'ready',
    'commit': os.environ['RELEASE_COMMIT'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'frontend': {
        'ready': front_ready,
        'release_key': front_release_key,
        'sha256': tree_hash(root / 'frontend') if front_ready and not front_release_key else '',
    },
    'backend': {
        'ready': back_ready,
        'sha256': tree_hash(root / 'backend' / 'dist') if back_ready else '',
        'deps_key': back_deps_key,
    },
}
path = root / 'release.json'
tmp = root / '.release.json.tmp'
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYRUNTIMERELEASE
}

verify_runtime_release_component() {
  local root="${1:?}" kind="${2:?}" ready
  ready="$root/release.json"
  [[ -s "$ready" && ! -L "$ready" ]] || return 1

  if [[ "$kind" == "frontend" ]]; then
    local release_key release_dir
    release_key="$(RELEASE_READY_FILE="$ready" python3 - <<'PYFRONTRELEASEKEY' 2>/dev/null
import json, os
with open(os.environ['RELEASE_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('frontend') if isinstance(data, dict) else None
if data.get('state') != 'ready' or not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
print(str(record.get('release_key') or ''))
PYFRONTRELEASEKEY
)" || return 1
    if [[ -n "$release_key" ]]; then
      release_dir="$(frontend_release_root_for_key "$release_key")" || return 1
      verify_frontend_release "$release_dir" "$release_key"
      return $?
    fi
    # Compatibilidade com release Wave 6–11 que continha snapshot físico.
    RELEASE_READY_FILE="$ready" RELEASE_ROOT="$root" python3 - <<'PYLEGACYFRONTVERIFY' >/dev/null 2>&1
import hashlib, json, os, pathlib
ready = pathlib.Path(os.environ['RELEASE_READY_FILE'])
root = pathlib.Path(os.environ['RELEASE_ROOT'])
data = json.loads(ready.read_text(encoding='utf-8'))
record = data.get('frontend') if isinstance(data, dict) else None
if not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
base = root / 'frontend'
if not base.is_dir():
    raise SystemExit(1)
digest = hashlib.sha256()
resolved_base = base.resolve()
for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
    rel = item.relative_to(base).as_posix()
    if item.is_symlink():
        target = os.readlink(item)
        if os.path.isabs(target):
            raise SystemExit(1)
        resolved = (item.parent / target).resolve(strict=False)
        try:
            resolved.relative_to(resolved_base)
        except ValueError:
            raise SystemExit(1)
        digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
        continue
    if not item.is_file():
        continue
    digest.update(b'F\0' + rel.encode() + b'\0')
    with item.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
if digest.hexdigest() != str(record.get('sha256') or ''):
    raise SystemExit(1)
PYLEGACYFRONTVERIFY
    return $?
  fi

  if [[ "$kind" == "backend" ]]; then
    local deps_key deps_layer
    deps_key="$(RELEASE_READY_FILE="$ready" python3 - <<'PYBACKRELEASEDEPS' 2>/dev/null
import json, os
with open(os.environ['RELEASE_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('backend') if isinstance(data, dict) else None
if data.get('state') != 'ready' or not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
key = str(record.get('deps_key') or '')
if len(key) != 64:
    raise SystemExit(1)
print(key)
PYBACKRELEASEDEPS
)" || return 1
    deps_layer="$(node_dependency_layer_root backend prod "$deps_key")" || return 1
    verify_node_dependency_layer "$deps_layer" "$deps_key" prod || return 1
  fi

  RELEASE_READY_FILE="$ready" RELEASE_KIND="$kind" RELEASE_ROOT="$root" python3 - <<'PYVERIFYRUNTIME' >/dev/null 2>&1
import hashlib, json, os, pathlib
ready = pathlib.Path(os.environ['RELEASE_READY_FILE'])
kind = os.environ['RELEASE_KIND']
root = pathlib.Path(os.environ['RELEASE_ROOT'])
data = json.loads(ready.read_text(encoding='utf-8'))
record = data.get(kind) if isinstance(data, dict) else None
if data.get('state') != 'ready' or not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
path = root / 'backend/dist'

def tree_hash(base: pathlib.Path) -> str:
    digest = hashlib.sha256()
    resolved_base = base.resolve()
    for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
        rel = item.relative_to(base).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(1)
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(resolved_base)
            except ValueError:
                raise SystemExit(1)
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

if not path.exists() or tree_hash(path) != str(record.get('sha256') or ''):
    raise SystemExit(1)
PYVERIFYRUNTIME
}

capture_runtime_release_snapshot() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")"
  [[ -n "$commit" ]] || return 1
  if (( ${REQUIREMENTS_CHANGED:-0} == 1 )); then
    if ! capture_python_runtime_release_snapshot "$commit"; then
      return 1
    fi
  fi
  if (( FRONT_CHANGED == 0 && BACK_CHANGED == 0 )); then
    RUNTIME_RELEASE_SNAPSHOT_READY=1
    RUNTIME_RELEASE_SNAPSHOT_COMMIT="$commit"
    return 0
  fi

  local root tmp front_ready=0 back_ready=0
  root="$(runtime_release_root_for_commit "$commit")" || return 1
  if [[ -s "$root/release.json" ]]; then
    local reusable=1
    (( FRONT_CHANGED == 0 )) || verify_runtime_release_component "$root" frontend || reusable=0
    (( BACK_CHANGED == 0 )) || verify_runtime_release_component "$root" backend || reusable=0
    if (( reusable == 1 )); then
      RUNTIME_RELEASE_SNAPSHOT_ROOT="$root"
      RUNTIME_RELEASE_SNAPSHOT_COMMIT="$commit"
      RUNTIME_RELEASE_SNAPSHOT_READY=1
      logger -t "$LOG_TAG" "release runtime anterior reutilizado para rollback: $(short_commit "$commit")" 2>/dev/null || true
      return 0
    fi
  fi

  install -d -o ubuntu -g ubuntu -m 0775 "$RUNTIME_RELEASE_ROOT" || return 1
  tmp="$(mktemp -d "$RUNTIME_RELEASE_ROOT/.${commit}.XXXXXX")" || return 1

  if (( FRONT_CHANGED == 1 )); then
    if ! frontend_publication_is_healthy; then
      LAST_ERROR_STDERR="não há publicação frontend saudável para preservar antes da promoção"
      LAST_ERROR_CODE="RUNTIME_RELEASE_FRONTEND_BASELINE_MISSING"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    local baseline_front_release_key=""
    baseline_front_release_key="$(frontend_active_release_key 2>/dev/null || true)"
    if [[ -z "$baseline_front_release_key" ]]; then
      baseline_front_release_key="$(adopt_frontend_publication_as_release "$commit" 2>/dev/null || true)"
    fi
    if [[ -z "$baseline_front_release_key" ]]; then
      LAST_ERROR_STDERR="não foi possível adotar/reutilizar release frontend anterior"
      LAST_ERROR_CODE="RUNTIME_RELEASE_FRONTEND_ADOPTION_FAILED"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    RELEASE_FRONT_REF_FILE="$tmp/frontend.json" RELEASE_FRONT_REF_KEY="$baseline_front_release_key" python3 - <<'PYRELEASEFRONTREF'
import json, os, pathlib
path = pathlib.Path(os.environ['RELEASE_FRONT_REF_FILE'])
payload = {'release_key': os.environ['RELEASE_FRONT_REF_KEY']}
path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
PYRELEASEFRONTREF
    front_ready=1
  fi

  if (( BACK_CHANGED == 1 )); then
    if [[ ! -s "$BACK_DIR/dist/index.js" || ! -e "$BACK_DIR/node_modules" && ! -L "$BACK_DIR/node_modules" ]]; then
      LAST_ERROR_STDERR="backend live não possui dist/index.js e node_modules preserváveis antes da promoção"
      LAST_ERROR_CODE="RUNTIME_RELEASE_BACKEND_BASELINE_MISSING"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    if ! backend_live_dependency_layer "$BACK_DIR"; then
      LAST_ERROR_STDERR="não foi possível adotar/reutilizar dependency layer do backend live antes da promoção"
      LAST_ERROR_CODE="RUNTIME_RELEASE_BACKEND_DEP_LAYER_FAILED"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    local baseline_dep_key="$LAST_NODE_DEP_LAYER_KEY"
    [[ "$baseline_dep_key" =~ ^[a-f0-9]{64}$ ]] || {
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    }
    install -d -m 0755 "$tmp/backend"
    cp -a -- "$BACK_DIR/dist" "$tmp/backend/dist" || { rm -rf -- "$tmp" 2>/dev/null || true; return 1; }
    RELEASE_BACK_DEPS_FILE="$tmp/backend/deps.json" RELEASE_BACK_DEPS_KEY="$baseline_dep_key" python3 - <<'PYRELEASEBACKDEPS'
import json, os, pathlib
path = pathlib.Path(os.environ['RELEASE_BACK_DEPS_FILE'])
payload = {'mode': 'prod', 'deps_key': os.environ['RELEASE_BACK_DEPS_KEY']}
path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
PYRELEASEBACKDEPS
    back_ready=1
  fi

  if ! write_runtime_release_manifest "$tmp" "$commit" "$front_ready" "$back_ready"; then
    LAST_ERROR_STDERR="não foi possível registrar release runtime anterior"
    LAST_ERROR_CODE="RUNTIME_RELEASE_MANIFEST_FAILED"
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if (( front_ready == 1 )) && ! verify_runtime_release_component "$tmp" frontend; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if (( back_ready == 1 )) && ! verify_runtime_release_component "$tmp" backend; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi

  rm -rf -- "$root" 2>/dev/null || true
  if ! mv -- "$tmp" "$root"; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  chmod 0644 "$root/release.json" 2>/dev/null || true
  RUNTIME_RELEASE_SNAPSHOT_ROOT="$root"
  RUNTIME_RELEASE_SNAPSHOT_COMMIT="$commit"
  RUNTIME_RELEASE_SNAPSHOT_READY=1
  logger -t "$LOG_TAG" "release runtime anterior preservado para rollback: $(short_commit "$commit") em $root" 2>/dev/null || true
  return 0
}

restore_frontend_runtime_release() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")" root release_key=""
  root="$(runtime_release_root_for_commit "$commit")" || return 1
  if ! verify_runtime_release_component "$root" frontend; then
    FRONT_STATUS="release frontend anterior ausente ou corrompido para $(short_commit "$commit")"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    LAST_ERROR_CODE="ROLLBACK_FRONTEND_RELEASE_INVALID"
    return 1
  fi
  release_key="$(RELEASE_READY_FILE="$root/release.json" python3 - <<'PYROLLBACKFRONTKEY' 2>/dev/null
import json, os
with open(os.environ['RELEASE_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('frontend') if isinstance(data, dict) else None
print(str(record.get('release_key') or '') if isinstance(record, dict) else '')
PYROLLBACKFRONTKEY
)" || true
  if [[ -z "$release_key" ]]; then
    # Migração de releases Wave 6–11: transforma o snapshot físico antigo em
    # release dedicada uma única vez; hardlink é usado quando o filesystem permite.
    [[ -s "$root/frontend/index.html" ]] || return 1
    prepare_frontend_release "$root/frontend" "$commit" >/dev/null || {
      LAST_ERROR_CODE="ROLLBACK_FRONTEND_RELEASE_MIGRATION_FAILED"
      return 1
    }
    release_key="$commit"
  fi
  if ! activate_frontend_release "$release_key"; then
    LAST_ERROR_CODE="ROLLBACK_FRONTEND_RELEASE_PUBLISH_FAILED"
    return 1
  fi
  FRONT_STATUS="frontend restaurado sem rebuild por release $(short_commit "$release_key")"
  return 0
}

restore_backend_runtime_release() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")" root
  root="$(runtime_release_root_for_commit "$commit")" || return 1
  if ! verify_runtime_release_component "$root" backend; then
    BACK_STATUS="release backend anterior ausente ou corrompido para $(short_commit "$commit")"
    LAST_ERROR_STDERR="$BACK_STATUS"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_RELEASE_INVALID"
    return 1
  fi
  if ! install_backend_prebuilt_artifact "$root/backend"; then
    BACK_STATUS="falha ao restaurar release backend anterior"
    LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-$BACK_STATUS}"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_RELEASE_INSTALL_FAILED"
    return 1
  fi
  if ! systemctl cat "$BACK_SERVICE" >/dev/null 2>&1; then
    BACK_STATUS="serviço systemd ausente durante rollback: $BACK_SERVICE"
    LAST_ERROR_STDERR="$BACK_STATUS"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_SERVICE_MISSING"
    return 1
  fi
  systemctl reset-failed "$BACK_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$BACK_SERVICE" || return 1
  if ! wait_for_service_active "$BACK_SERVICE" 20 0.25; then
    BACK_STATUS="backend anterior restaurado, mas serviço não ficou ativo"
    LAST_ERROR_STDERR="$(journalctl -u "$BACK_SERVICE" -n 30 --no-pager 2>/dev/null | trim_alert_text 1800)"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_SERVICE_FAILED"
    return 1
  fi
  if ! { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2; else wait_for_health "$BACK_HEALTH_URL" 8 3; fi; }; then
    ACTIVITY_HEALTHCHECK_STATUS="falhou"
    BACK_STATUS="backend anterior restaurado sem rebuild, mas healthcheck falhou"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_HEALTH_FAILED"
    return 1
  fi
  ACTIVITY_HEALTHCHECK_STATUS="OK"
  BACK_STATUS="backend restaurado sem rebuild a partir do release $(short_commit "$commit")"
  return 0
}

prune_runtime_releases() {
  prune_python_runtime_releases || true
  prune_frontend_releases || true
  local keep="${RUNTIME_RELEASE_RETENTION:-3}" current count=0 entry
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=3
  (( keep >= 1 )) || keep=1
  [[ -d "$RUNTIME_RELEASE_ROOT" ]] || return 0
  current="$(repo_git rev-parse HEAD 2>/dev/null || true)"
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    if [[ "$(basename "$entry")" == "$current" ]]; then
      continue
    fi
    count=$((count + 1))
    if (( count > keep )); then
      rm -rf -- "$entry" 2>/dev/null || true
    fi
  done < <(find "$RUNTIME_RELEASE_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | cut -d' ' -f2-)
}
