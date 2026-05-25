#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
TEMPLATE_DIR="${TEMPLATE_DIR:-$REPO_DIR/deploy/systemd/vps}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
BACKUP_ROOT="${BACKUP_ROOT:-$REPO_DIR/data/systemd-backups}"
STATUS_FILE="${STATUS_FILE:-$REPO_DIR/data/vps-systemd-install-status.json}"
DRY_RUN=0
FROM_UPDATER=0
INSTALL_LEGACY_VPS_LAVALINK=0
NOW="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$NOW"
CHANGED=0
ACTIONS=()
WARNINGS=()

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --from-updater) FROM_UPDATER=1 ;;
    --install-legacy-vps-lavalink) INSTALL_LEGACY_VPS_LAVALINK=1 ;;
    *) echo "argumento desconhecido: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '[vps-systemd] %s\n' "$*"; }
action() { ACTIONS+=("$*"); log "$*"; }
warn() { WARNINGS+=("$*"); log "AVISO: $*"; }

require_root() {
  if [[ "$(id -u)" != "0" ]]; then
    echo "Execute como root: sudo $0" >&2
    exit 1
  fi
}

ensure_paths() {
  if [[ ! -d "$TEMPLATE_DIR" ]]; then
    echo "diretório de templates não encontrado: $TEMPLATE_DIR" >&2
    exit 1
  fi
  mkdir -p "$BACKUP_DIR" "$(dirname "$STATUS_FILE")"
}

backup_live() {
  local rel="$1"
  local live="$SYSTEMD_DIR/$rel"
  local dest="$BACKUP_DIR/$rel"
  if [[ -e "$live" || -L "$live" ]]; then
    mkdir -p "$(dirname "$dest")"
    cp -a "$live" "$dest" 2>/dev/null || true
  fi
}

install_file() {
  local rel="$1"
  local src="$TEMPLATE_DIR/$rel"
  local dst="$SYSTEMD_DIR/$rel"
  if [[ ! -f "$src" ]]; then
    warn "template ausente: $rel"
    return 0
  fi
  backup_live "$rel"
  if [[ "$DRY_RUN" == "1" ]]; then
    action "dry-run: instalaria $rel"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  install -m 0644 "$src" "$dst"
  CHANGED=1
  action "instalado: $rel"
}

install_dir_files() {
  local rel_dir="$1"
  local src_dir="$TEMPLATE_DIR/$rel_dir"
  local dst_dir="$SYSTEMD_DIR/$rel_dir"
  local src rel
  if [[ ! -d "$src_dir" ]]; then
    warn "diretório de templates ausente: $rel_dir"
    return 0
  fi
  mkdir -p "$dst_dir"
  while IFS= read -r -d '' src; do
    rel="$rel_dir/${src#$src_dir/}"
    install_file "$rel"
  done < <(find "$src_dir" -type f -print0 | sort -z)
}

truthy_env() {
  local key="$1" value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    value="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | sed -E 's/^[[:space:]]*export[[:space:]]+//' | cut -d= -f2- || true)"
  fi
  value="${value%$'\r'}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n')"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

sanitize_lavalink_references() {
  local file
  for file in "$SYSTEMD_DIR/tts-bot.service" "$SYSTEMD_DIR"/tts-bot.service.d/*.conf; do
    [[ -f "$file" ]] || continue
    if grep -qE 'lavalink\.service|wait-audio-node-ready\.py' "$file" 2>/dev/null; then
      backup_live "${file#$SYSTEMD_DIR/}"
      if [[ "$DRY_RUN" == "1" ]]; then
        action "dry-run: sanitizaria ${file#$SYSTEMD_DIR/}"
        continue
      fi
      python3 - "$file" <<'PY_SANITIZE'
import sys
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text(encoding='utf-8', errors='replace')
out = []
for line in text.splitlines():
    stripped = line.strip()
    if stripped.startswith(('Wants=', 'Requires=', 'After=', 'Before=')) and 'lavalink.service' in stripped:
        key, value = line.split('=', 1)
        parts = [part for part in value.split() if part != 'lavalink.service']
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
PY_SANITIZE
      CHANGED=1
      action "sanitizado: ${file#$SYSTEMD_DIR/}"
    fi
  done
}

mask_vps_lavalink() {
  if [[ "$INSTALL_LEGACY_VPS_LAVALINK" == "1" ]] || truthy_env VPS_LAVALINK_ENABLED; then
    warn "VPS_LAVALINK_ENABLED ativo; não vou mascarar lavalink.service"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    action "dry-run: manteria lavalink.service local inativo/mascarado"
    return 0
  fi
  systemctl stop lavalink.service >/dev/null 2>&1 || true
  systemctl disable lavalink.service >/dev/null 2>&1 || true
  systemctl reset-failed lavalink.service >/dev/null 2>&1 || true
  if [[ -e "$SYSTEMD_DIR/lavalink.service" && ! -L "$SYSTEMD_DIR/lavalink.service" ]]; then
    backup_live "lavalink.service"
    mv "$SYSTEMD_DIR/lavalink.service" "$SYSTEMD_DIR/lavalink.service.disabled.$NOW" 2>/dev/null || true
  fi
  ln -sfn /dev/null "$SYSTEMD_DIR/lavalink.service"
  CHANGED=1
  action "lavalink.service local mantido mascarado/inativo"
}

normalize_crontab() {
  local current tmp changed
  current="${TMPDIR:-/tmp}/vps-cron-current.$$"
  tmp="${TMPDIR:-/tmp}/vps-cron-normalized.$$"
  if ! sudo -u ubuntu -H crontab -l > "$current" 2>/dev/null; then
    action "crontab ubuntu ausente ou ilegível; nada a normalizar"
    rm -f "$current" "$tmp"
    return 0
  fi
  python3 - "$current" "$tmp" <<'PY_CRON'
import sys
from pathlib import Path
src = Path(sys.argv[1])
dst = Path(sys.argv[2])
text = src.read_text(encoding='utf-8', errors='replace')
health_line = '# TEMP_DISABLED_HEALTHCHECK_UNTIL_PATCH_20260524 * * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
resource_line = '# TEMP_DISABLED_EMERGENCY_20260524 */5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'
out = []
has_health = False
has_resource = False
for line in text.splitlines():
    s = line.strip()
    if s == '>/dev/null 2>&1':
        continue
    if 'healthcheck.sh' in line:
        if line.lstrip().startswith('#') or 'TEMP_DISABLED' in line:
            if not has_health:
                out.append(health_line)
                has_health = True
            continue
    if 'resource-check.sh' in line:
        if line.lstrip().startswith('#') or 'TEMP_DISABLED' in line:
            if not has_resource:
                out.append(resource_line)
                has_resource = True
            continue
    out.append(line)
dst.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_CRON
  if ! cmp -s "$current" "$tmp"; then
    if [[ "$DRY_RUN" == "1" ]]; then
      action "dry-run: normalizaria crontab ubuntu"
    else
      cp -a "$current" "$BACKUP_DIR/ubuntu.crontab" 2>/dev/null || true
      sudo -u ubuntu -H crontab "$tmp"
      CHANGED=1
      action "crontab ubuntu normalizado"
    fi
  else
    action "crontab ubuntu já limpo"
  fi
  rm -f "$current" "$tmp"
}

chmod_scripts() {
  local rel
  for rel in \
    start.sh alert.sh notify-failure.sh healthcheck.sh resource-check.sh cleanup-audio-temp.sh \
    scripts/tts-bot-update.sh scripts/install-vps-systemd-units.sh \
    scripts/phone-worker-watch.sh scripts/phone-lavalink-watch.sh scripts/sync-phone-worker.sh; do
    if [[ -f "$REPO_DIR/$rel" && "$DRY_RUN" != "1" ]]; then
      chmod +x "$REPO_DIR/$rel" 2>/dev/null || true
    fi
  done
}

install_units() {
  local unit
  for unit in \
    tts-bot.service \
    tts-bot-updater.service tts-bot-updater.timer \
    tts-bot-alert@.service \
    callkeeper.service \
    cleanup-audio-temp.service cleanup-audio-temp.timer \
    sinuca-activity-server.service \
    phone-worker-watch.service phone-worker-watch.timer \
    phone-lavalink-watch.service phone-lavalink-watch.timer; do
    install_file "$unit"
  done
  install_dir_files "tts-bot.service.d"
}

apply_service_policy() {
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  systemctl daemon-reload || true
  systemctl reset-failed tts-bot.service tts-bot-updater.service tts-bot-alert@tts-bot.service.service >/dev/null 2>&1 || true
  systemctl enable tts-bot.service >/dev/null 2>&1 || true
  systemctl enable --now tts-bot-updater.timer >/dev/null 2>&1 || true
  systemctl enable --now cleanup-audio-temp.timer >/dev/null 2>&1 || true
  systemctl start cleanup-audio-temp.service >/dev/null 2>&1 || true

  if truthy_env PHONE_WORKER_WATCH_ENABLED; then
    systemctl enable --now phone-worker-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-worker-watch.service >/dev/null 2>&1 || true
    action "phone-worker-watch ativo por PHONE_WORKER_WATCH_ENABLED=true"
  else
    systemctl disable --now phone-worker-watch.timer phone-worker-watch.service >/dev/null 2>&1 || true
    action "phone-worker-watch instalado, mas inativo por padrão"
  fi

  if truthy_env PHONE_LAVALINK_WATCH_ENABLED || truthy_env AUX_LAVALINK_ENABLED; then
    systemctl enable --now phone-lavalink-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-lavalink-watch.service >/dev/null 2>&1 || true
    action "phone-lavalink-watch ativo por env"
  else
    systemctl disable --now phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
    action "phone-lavalink-watch instalado, mas inativo por padrão"
  fi
}

write_status() {
  local actions_json warnings_json
  actions_json="$(printf '%s\n' "${ACTIONS[@]:-}" | python3 -c 'import json,sys; print(json.dumps([x for x in sys.stdin.read().splitlines() if x], ensure_ascii=False))')"
  warnings_json="$(printf '%s\n' "${WARNINGS[@]:-}" | python3 -c 'import json,sys; print(json.dumps([x for x in sys.stdin.read().splitlines() if x], ensure_ascii=False))')"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  cat > "$STATUS_FILE" <<EOF_STATUS
{"ok": true, "changed": ${CHANGED}, "from_updater": ${FROM_UPDATER}, "timestamp": "$(date -Iseconds)", "template_dir": "$TEMPLATE_DIR", "backup_dir": "$BACKUP_DIR", "actions": $actions_json, "warnings": $warnings_json}
EOF_STATUS
  chown ubuntu:ubuntu "$STATUS_FILE" 2>/dev/null || true
}

main() {
  require_root
  ensure_paths
  chmod_scripts
  install_units
  sanitize_lavalink_references
  mask_vps_lavalink
  normalize_crontab
  apply_service_policy
  write_status
  log "concluído; backups em $BACKUP_DIR"
}

main "$@"
