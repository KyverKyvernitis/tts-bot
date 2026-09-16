prune_archive_root() {
  local root="${1:?}" retention_days="${2:?}" keep_count="${3:?}"
  local -a entries=()
  local index path
  [[ -d "$root" && ! -L "$root" ]] || return 0
  [[ "$retention_days" =~ ^[0-9]+$ ]] || return 0
  [[ "$keep_count" =~ ^[0-9]+$ ]] || return 0
  (( keep_count < 1 )) && keep_count=1
  (( keep_count > 100 )) && keep_count=100

  find "$root" -mindepth 1 -maxdepth 1 -type d -mtime "+$retention_days" -exec rm -rf -- {} + 2>/dev/null || true
  mapfile -t entries < <(
    find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%T@\t%p\n' 2>/dev/null \
      | sort -t $'\t' -k1,1nr \
      | cut -f2-
  )
  for ((index=keep_count; index<${#entries[@]}; index++)); do
    path="${entries[$index]}"
    [[ -n "$path" && ! -L "$path" && "$(dirname -- "$path")" == "$root" ]] || continue
    rm -rf -- "$path" 2>/dev/null || true
  done
}

prune_updater_runtime_orphans() {
  local legacy_tmp="${TMPDIR:-/tmp}"
  [[ "$legacy_tmp" == /* && -d "$legacy_tmp" && ! -L "$legacy_tmp" ]] || return 0

  # Apenas prefixos privados do updater, sem recursão fora das raízes exatas.
  find "$legacy_tmp" -maxdepth 1 -type f -name 'tts-bot-update.*.run' -mmin +60 -delete 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type f -name 'tts-bot-updater.*.log' -mmin +1440 -delete 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type f -name 'bot-updater.*.log' -mmin +1440 -delete 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type d -name 'bot-updater.*.core' \
    ! -path "${UPDATER_RUNTIME_BUNDLE:-}" -mmin +1440 -exec rm -rf -- {} + 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type f \( -name 'tts-bot-git-add.*' -o -name 'tts-bot-git-add-retry.*' \) -mmin +360 -delete 2>/dev/null || true
  find "$legacy_tmp" -mindepth 1 -maxdepth 1 -type d \( -name 'tts-bot-remote-candidate.*' -o -name 'tts-bot-systemd-overlay.*' \) -mmin +360 -exec rm -rf -- {} + 2>/dev/null || true
  repo_git worktree prune --expire=now >/dev/null 2>&1 || true
}

prune_rejected_remote_commits() {
  local retention_days="${DISCORD_AUTO_UPDATE_REJECTED_RETENTION_DAYS:-30}"
  local keep_count="${DISCORD_AUTO_UPDATE_REJECTED_KEEP_COUNT:-100}"
  [[ -f "$REMOTE_REJECTED_FILE" && ! -L "$REMOTE_REJECTED_FILE" ]] || return 0
  [[ "$retention_days" =~ ^[0-9]+$ ]] || retention_days=30
  [[ "$keep_count" =~ ^[0-9]+$ ]] || keep_count=100
  REJECTED_FILE_VALUE="$REMOTE_REJECTED_FILE" REJECTED_RETENTION_DAYS="$retention_days" REJECTED_KEEP_COUNT="$keep_count" \
    python3 - <<'PYPRUNEREJECTED' 2>/dev/null || true
import datetime, json, os, pathlib, tempfile

path = pathlib.Path(os.environ['REJECTED_FILE_VALUE'])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    raise SystemExit
items = data.get('commits') if isinstance(data, dict) else None
if not isinstance(items, dict):
    raise SystemExit

now = datetime.datetime.now(datetime.timezone.utc)
cutoff = now - datetime.timedelta(days=max(0, int(os.environ['REJECTED_RETENTION_DAYS'])))
keep_count = max(1, min(1000, int(os.environ['REJECTED_KEEP_COUNT'])))
retained = []
for commit, record in items.items():
    if not isinstance(record, dict):
        retained.append((now, commit, record))
        continue
    raw = str(record.get('rejected_at') or '')
    try:
        stamp = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        stamp = now
    if stamp >= cutoff:
        retained.append((stamp, commit, record))
retained.sort(key=lambda item: (item[0], item[1]), reverse=True)
new_items = {commit: record for _stamp, commit, record in retained[:keep_count]}
if new_items == items:
    raise SystemExit
data['commits'] = new_items
fd, tmp_name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
tmp = pathlib.Path(tmp_name)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
PYPRUNEREJECTED
  chown ubuntu:ubuntu "$REMOTE_REJECTED_FILE" 2>/dev/null || true
}

human_storage_bytes() {
  local value="${1:-0}"
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$value" 2>/dev/null || printf '%s bytes' "$value"
  else
    printf '%s bytes' "$value"
  fi
}

guard_updater_disk_space() {
  local metrics total available hard_min hard_percent soft_percent hard_floor percent_floor event_id
  metrics="$(df -P -B1 "$REPO_DIR" 2>/dev/null | awk 'NR == 2 {print $2, $4}')"
  read -r total available <<< "$metrics"
  [[ "$total" =~ ^[0-9]+$ && "$available" =~ ^[0-9]+$ ]] || {
    logger -t "$LOG_TAG" "proteção de espaço: não foi possível ler o filesystem" 2>/dev/null || true
    return 0
  }

  hard_min="${TTS_BOT_UPDATER_DISK_HARD_MIN_BYTES:-2147483648}"
  hard_percent="${TTS_BOT_UPDATER_DISK_HARD_MIN_PERCENT:-5}"
  soft_percent="${TTS_BOT_UPDATER_DISK_SOFT_MIN_PERCENT:-15}"
  [[ "$hard_min" =~ ^[0-9]+$ ]] || hard_min=2147483648
  [[ "$hard_percent" =~ ^[0-9]+$ && "$hard_percent" -le 50 ]] || hard_percent=5
  [[ "$soft_percent" =~ ^[0-9]+$ && "$soft_percent" -le 80 ]] || soft_percent=15
  percent_floor=$((total * hard_percent / 100))
  hard_floor="$hard_min"
  (( percent_floor > hard_floor )) && hard_floor="$percent_floor"

  if (( available < hard_floor )); then
    event_id="updater-disk-critical-$(date +%Y%m%d)"
    logger -t "$LOG_TAG" "update pausado: espaço crítico, livre=$(human_storage_bytes "$available") mínimo=$(human_storage_bytes "$hard_floor")" 2>/dev/null || true
    send_error "Update pausado por pouco espaço" "Resumo: A atualização não foi iniciada para evitar uma aplicação parcial.
Espaço livre: $(human_storage_bytes "$available")
Mínimo seguro: $(human_storage_bytes "$hard_floor")
Bot: preservado e sem reinício
Ação: revise os artefatos de armazenamento antes de tentar novamente.
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "$event_id"
    return 1
  fi

  if (( available * 100 < total * soft_percent )); then
    event_id="updater-disk-warning-$(date +%Y%m%d)"
    logger -t "$LOG_TAG" "proteção de espaço: nível preventivo, livre=$(human_storage_bytes "$available")" 2>/dev/null || true
    send_warn "Espaço da VPS abaixo do nível preventivo" "Resumo: O updater ainda pode funcionar, mas o espaço livre caiu abaixo de ${soft_percent}%.
Espaço livre: $(human_storage_bytes "$available")
Limite crítico atual: $(human_storage_bytes "$hard_floor")
Bot: preservado
Ação: revise os artefatos de armazenamento antes de atingir o bloqueio de segurança.
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "$event_id"
  fi
  return 0
}

updater_heavy_maintenance_due() {
  local interval="${UPDATE_HEAVY_MAINTENANCE_INTERVAL_SECONDS:-3600}"
  local stamp="${UPDATE_HEAVY_MAINTENANCE_STAMP:-$CANDIDATE_ROOT/.heavy-maintenance.stamp}"
  local now last=0
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=3600
  (( interval >= 60 )) || interval=60
  now="$(date +%s)"
  if [[ -f "$stamp" && ! -L "$stamp" ]]; then
    last="$(stat -c %Y "$stamp" 2>/dev/null || printf '0')"
  fi
  [[ "$last" =~ ^[0-9]+$ ]] || last=0
  (( now - last >= interval ))
}

mark_updater_heavy_maintenance() {
  local stamp="${UPDATE_HEAVY_MAINTENANCE_STAMP:-$CANDIDATE_ROOT/.heavy-maintenance.stamp}"
  local tmp="${stamp}.tmp.$$"
  install -d -o ubuntu -g ubuntu -m 0775 "$(dirname "$stamp")" 2>/dev/null || true
  printf '%s\n' "$(date +%s)" > "$tmp" 2>/dev/null || return 0
  chown ubuntu:ubuntu "$tmp" 2>/dev/null || true
  chmod 0644 "$tmp" 2>/dev/null || true
  mv -f -- "$tmp" "$stamp" 2>/dev/null || rm -f -- "$tmp" 2>/dev/null || true
}

prune_update_artifacts() {
  # O timer do updater pode rodar a cada minuto. As podas abaixo fazem vários
  # find/sort e percorrem metadados de caches grandes; executá-las em todo tick
  # desperdiça I/O mesmo quando não há update. Faça manutenção pesada no máximo
  # uma vez por hora por padrão. A proteção de espaço continua rodando em todo
  # ciclo e pode bloquear updates independentemente desta poda.
  if ! updater_heavy_maintenance_due; then
    return 0
  fi

  local done_days="${DISCORD_AUTO_UPDATE_DONE_RETENTION_DAYS:-7}"
  local failed_days="${DISCORD_AUTO_UPDATE_FAILED_RETENTION_DAYS:-30}"
  local cancelled_days="${DISCORD_AUTO_UPDATE_CANCELLED_RETENTION_DAYS:-7}"
  local done_keep="${DISCORD_AUTO_UPDATE_DONE_KEEP_COUNT:-5}"
  local failed_keep="${DISCORD_AUTO_UPDATE_FAILED_KEEP_COUNT:-5}"
  local cancelled_keep="${DISCORD_AUTO_UPDATE_CANCELLED_KEEP_COUNT:-3}"
  local outbox_days="${DISCORD_AUTO_UPDATE_OUTBOX_RETENTION_DAYS:-7}"
  local receipt_days="${DISCORD_AUTO_UPDATE_DELIVERY_RECEIPT_RETENTION_DAYS:-30}"
  local incident_days="${DISCORD_AUTO_UPDATE_INCIDENT_RETENTION_DAYS:-30}"
  [[ "$done_days" =~ ^[0-9]+$ ]] || done_days=7
  [[ "$failed_days" =~ ^[0-9]+$ ]] || failed_days=30
  [[ "$cancelled_days" =~ ^[0-9]+$ ]] || cancelled_days=7
  [[ "$done_keep" =~ ^[0-9]+$ ]] || done_keep=5
  [[ "$failed_keep" =~ ^[0-9]+$ ]] || failed_keep=5
  [[ "$cancelled_keep" =~ ^[0-9]+$ ]] || cancelled_keep=3
  [[ "$outbox_days" =~ ^[0-9]+$ ]] || outbox_days=7
  [[ "$receipt_days" =~ ^[0-9]+$ ]] || receipt_days=30
  [[ "$incident_days" =~ ^[0-9]+$ ]] || incident_days=30
  prune_archive_root "$CANDIDATE_ROOT/done" "$done_days" "$done_keep"
  prune_archive_root "$CANDIDATE_ROOT/failed" "$failed_days" "$failed_keep"
  prune_archive_root "$CANDIDATE_ROOT/cancelled" "$cancelled_days" "$cancelled_keep"
  prune_archive_root "$REMOTE_RUNTIME_ARTIFACT_ROOT" 1 2
  prune_node_dependency_layers || true
  prune_typescript_cache || true
  find "$CANDIDATE_QUEUE_DONE_DIR" -type f -mtime "+$done_days" -delete 2>/dev/null || true
  find "$CANDIDATE_QUEUE_FAILED_DIR" -type f -mtime "+$failed_days" -delete 2>/dev/null || true
  find "$CANDIDATE_QUEUE_CANCELLED_DIR" -type f -mtime "+$cancelled_days" -delete 2>/dev/null || true
  find "$UPDATE_STATUS_OUTBOX_DIR" -type f -name '*.json' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_ALERT_OUTBOX_DIR" -type f -name '*.json' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_ALERT_OUTBOX_DIR" -type f -name '*.attachment' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_DELIVERY_RECEIPTS_DIR" -type f -mtime "+$receipt_days" -delete 2>/dev/null || true
  find "$UPDATE_INCIDENT_ROOT" -mindepth 2 -maxdepth 2 -type d -mtime "+$incident_days" -exec rm -rf -- {} + 2>/dev/null || true
  find "$UPDATE_INCIDENT_ROOT" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
  prune_rejected_remote_commits
  prune_updater_runtime_orphans
  mark_updater_heavy_maintenance
}

git_add_changed_files_or_reject() {
  local context="${1:-git add}"
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  local errfile
  errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-add.XXXXXX")"
  if git_add_changed_files 2>"$errfile"; then
    rm -f "$errfile" 2>/dev/null || true
    return 0
  fi

  LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
  rm -f "$errfile" 2>/dev/null || true

  # Arquivos/pastas novos podem ser criados pelo updater root antes do commit.
  # O git add roda como ubuntu; se o path ficou root-owned, normalize e tente uma vez.
  if printf '%s' "$LAST_ERROR_STDERR" | grep -qiE 'Permission denied|unable to index file|adding files failed'; then
    normalize_changed_file_permissions "$context" || true
    errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-add-retry.XXXXXX")"
    if git_add_changed_files 2>"$errfile"; then
      rm -f "$errfile" 2>/dev/null || true
      LAST_ERROR_STDERR=""
      return 0
    fi
    LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
    rm -f "$errfile" 2>/dev/null || true
  fi

  reject_local_candidate_safely \
    "Falha ao aplicar atualização" \
    "Não consegui preparar os arquivos do ZIP. A VPS foi restaurada e o candidato foi arquivado." \
    "$context: ${LAST_ERROR_STDERR:-git add falhou}"
}
