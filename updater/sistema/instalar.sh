#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
# Templates gerais da VPS continuam em deploy/systemd/vps. As units que pertencem
# ao updater têm fonte canônica em updater/sistema. Quando TEMPLATE_DIR é
# fornecido pelo próprio updater, ele representa um overlay transacional e tem
# precedência sobre qualquer template do checkout.
TEMPLATE_DIR_EXPLICIT=0
if [[ -n "${TEMPLATE_DIR+x}" ]]; then
  TEMPLATE_DIR_EXPLICIT=1
fi
TEMPLATE_DIR="${TEMPLATE_DIR:-$REPO_DIR/deploy/systemd/vps}"
UPDATER_SYSTEM_DIR="${UPDATER_SYSTEM_DIR:-$REPO_DIR/updater/sistema}"
UPDATER_SUDOERS_DIR="${UPDATER_SUDOERS_DIR:-$REPO_DIR/updater/sudoers}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
JOURNALD_POLICY_SRC="${JOURNALD_POLICY_SRC:-$REPO_DIR/deploy/journald/60-tts-bot-storage.conf}"
JOURNALD_POLICY_DEST="${JOURNALD_POLICY_DEST:-/etc/systemd/journald.conf.d/60-tts-bot-storage.conf}"
TMPFILES_POLICY_SRC="${TMPFILES_POLICY_SRC:-$REPO_DIR/deploy/tmpfiles.d/tts-bot-storage.conf}"
TMPFILES_POLICY_DEST="${TMPFILES_POLICY_DEST:-/etc/tmpfiles.d/tts-bot-storage.conf}"
BACKUP_ROOT="${BACKUP_ROOT:-$REPO_DIR/data/systemd-backups}"
STATUS_FILE="${STATUS_FILE:-$REPO_DIR/data/vps-systemd-install-status.json}"
DRY_RUN=0
AUDIT_ONLY=0
FROM_UPDATER=0
INSTALL_LEGACY_VPS_LAVALINK=0
NOW="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$NOW"
CHANGED=0
ACTIONS=()
WARNINGS=()
UPDATER_TIMER_WAS_ENABLED=0
UPDATER_TIMER_WAS_ACTIVE=0
UPDATER_PATH_WAS_ENABLED=0
UPDATER_PATH_WAS_ACTIVE=0
JOURNALD_POLICY_CHANGED=0
TMPFILES_POLICY_CHANGED=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --audit) AUDIT_ONLY=1; DRY_RUN=1 ;;
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
  if [[ "$DRY_RUN" != "1" ]]; then
    mkdir -p "$(dirname "$STATUS_FILE")"
  fi
}

backup_live() {
  local rel="$1"
  local live="$SYSTEMD_DIR/$rel"
  local dest="$BACKUP_DIR/$rel"
  [[ "$DRY_RUN" != "1" ]] || return 0
  if [[ -e "$live" || -L "$live" ]]; then
    mkdir -p "$(dirname "$dest")"
    cp -a "$live" "$dest" 2>/dev/null || true
  fi
}

is_updater_unit() {
  case "${1:-}" in
    bot-updater.service|bot-updater.timer|bot-updater.path|bot-updater-alert@.service) return 0 ;;
    *) return 1 ;;
  esac
}

template_source() {
  local rel="${1:?}"
  local src="$TEMPLATE_DIR/$rel"

  # Um overlay fornecido pelo pipeline representa exatamente os arquivos que
  # foram preparados para a transação atual e, por isso, sempre vence.
  if [[ "${TEMPLATE_DIR_EXPLICIT:-1}" == "1" ]]; then
    [[ -f "$src" && ! -L "$src" ]] && printf '%s' "$src"
    return 0
  fi

  # Fora de um overlay, as units do updater são propriedade de updater/sistema.
  if is_updater_unit "$rel"       && [[ -n "${UPDATER_SYSTEM_DIR:-}" ]]       && [[ -f "$UPDATER_SYSTEM_DIR/$rel" && ! -L "$UPDATER_SYSTEM_DIR/$rel" ]]; then
    printf '%s' "$UPDATER_SYSTEM_DIR/$rel"
    return 0
  fi

  [[ -f "$src" && ! -L "$src" ]] && printf '%s' "$src"
  return 0
}

install_file() {
  local rel="$1"
  local src=""
  local dst="$SYSTEMD_DIR/$rel"
  src="$(template_source "$rel")"
  if [[ ! -f "$src" ]]; then
    warn "template ausente: $rel"
    return 0
  fi
  if [[ -f "$dst" && ! -L "$dst" ]] \
      && cmp -s "$src" "$dst" \
      && [[ "$(stat -c '%a' "$dst" 2>/dev/null || true)" == "644" ]]; then
    action "mantido sem regravar: $rel"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    action "dry-run: atualizaria $rel"
    return 0
  fi
  backup_live "$rel"
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
  [[ "$DRY_RUN" == "1" ]] || mkdir -p "$dst_dir"
  while IFS= read -r -d '' src; do
    rel="$rel_dir/${src#$src_dir/}"
    install_file "$rel"
  done < <(find "$src_dir" -type f -print0 | sort -z)
}

install_storage_policy_file() {
  local src="${1:?}" dst="${2:?}" backup_rel="${3:?}" label="${4:?}" changed_var="${5:?}"
  local backup_dest="$BACKUP_DIR/$backup_rel"
  if [[ ! -f "$src" || -L "$src" ]]; then
    warn "política ausente ou insegura: $label"
    return 0
  fi
  if [[ -f "$dst" && ! -L "$dst" ]] \
      && cmp -s "$src" "$dst" \
      && [[ "$(stat -c '%a' "$dst" 2>/dev/null || true)" == "644" ]]; then
    action "política mantida sem regravar: $label"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    action "dry-run: atualizaria política $label"
    return 0
  fi
  if [[ -e "$dst" || -L "$dst" ]]; then
    mkdir -p "$(dirname "$backup_dest")"
    cp -a "$dst" "$backup_dest" 2>/dev/null || true
  fi
  mkdir -p "$(dirname "$dst")"
  install -m 0644 "$src" "$dst"
  printf -v "$changed_var" '%s' 1
  CHANGED=1
  action "política instalada: $label"
}

install_storage_policies() {
  install_storage_policy_file \
    "$JOURNALD_POLICY_SRC" "$JOURNALD_POLICY_DEST" \
    "journald.conf.d/$(basename "$JOURNALD_POLICY_DEST")" \
    "limite do journal" JOURNALD_POLICY_CHANGED
  install_storage_policy_file \
    "$TMPFILES_POLICY_SRC" "$TMPFILES_POLICY_DEST" \
    "tmpfiles.d/$(basename "$TMPFILES_POLICY_DEST")" \
    "retenção do cache Snap" TMPFILES_POLICY_CHANGED
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
  local current tmp
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

HEALTH_DISABLED = '# TEMP_DISABLED_HEALTHCHECK_UNTIL_PATCH_20260524 * * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_DISABLED = '# TEMP_DISABLED_EMERGENCY_20260524 */5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'
HEALTH_ACTIVE = '* * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_ACTIVE = '*/5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'

REDIRECT_ONLY = {'>/dev/null 2>&1', '>>/dev/null 2>&1'}

def is_redirect_only(line):
    stripped = line.strip()
    return stripped in REDIRECT_ONLY or stripped in {'2>&1', '&>/dev/null'}

def classify(line):
    if 'healthcheck.sh' in line:
        return 'health'
    if 'resource-check.sh' in line:
        return 'resource'
    return None

def is_disabled(line):
    stripped = line.lstrip()
    return stripped.startswith('#') or 'TEMP_DISABLED' in line

def canonical_for(line, kind):
    disabled = is_disabled(line)
    if kind == 'health':
        return HEALTH_DISABLED if disabled else HEALTH_ACTIVE
    return RESOURCE_DISABLED if disabled else RESOURCE_ACTIVE

out = []
has_disabled_health = False
has_active_health = False
has_disabled_resource = False
has_active_resource = False
pending_kind = None
pending_line = None

def flush_pending():
    global pending_kind, pending_line, has_disabled_health, has_active_health, has_disabled_resource, has_active_resource
    if pending_kind is None or pending_line is None:
        return
    line = canonical_for(pending_line, pending_kind)
    disabled = is_disabled(pending_line)
    if pending_kind == 'health':
        if disabled:
            if not has_disabled_health:
                out.append(line)
                has_disabled_health = True
        else:
            if not has_active_health:
                out.append(line)
                has_active_health = True
    else:
        if disabled:
            if not has_disabled_resource:
                out.append(line)
                has_disabled_resource = True
        else:
            if not has_active_resource:
                out.append(line)
                has_active_resource = True
    pending_kind = None
    pending_line = None

for raw in text.splitlines():
    line = raw.rstrip('\r')
    stripped = line.strip()

    if is_redirect_only(line):
        # Broken remnants from a split cron line must never remain active by themselves.
        continue

    kind = classify(line)
    if kind is not None:
        flush_pending()
        pending_kind = kind
        pending_line = line
        # The next line might be a stray redirect; flushing immediately is still safe
        # because redirect-only lines are skipped. Keeping a pending slot lets future
        # variants be handled without duplicating lines.
        flush_pending()
        continue

    flush_pending()
    out.append(line)

flush_pending()

dst.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_CRON
  if ! cmp -s "$current" "$tmp"; then
    if [[ "$DRY_RUN" == "1" ]]; then
      action "dry-run: normalizaria crontab ubuntu"
    else
      mkdir -p "$BACKUP_DIR"
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

backup_sudoers_live() {
  local rel="$1"
  local live="$SUDOERS_DIR/$rel"
  local dest="$BACKUP_DIR/sudoers.d/$rel"
  [[ "$DRY_RUN" != "1" ]] || return 0
  if [[ -e "$live" || -L "$live" ]]; then
    mkdir -p "$(dirname "$dest")"
    cp -a "$live" "$dest" 2>/dev/null || true
  fi
}

install_sudoers_files() {
  local src_dir="$UPDATER_SUDOERS_DIR"
  local src rel dst tmp
  if [[ ! -d "$src_dir" ]]; then
    action "sudoers canônico do updater ausente"
    return 0
  fi
  while IFS= read -r -d '' src; do
    [[ "${src##*/}" == "bot-updater-start" ]] || continue
    rel="${src#$src_dir/}"
    dst="$SUDOERS_DIR/$rel"
    if [[ "$DRY_RUN" == "1" ]]; then
      action "dry-run: instalaria sudoers $rel"
      continue
    fi
    tmp="${TMPDIR:-/tmp}/sudoers-${rel//\//_}.$$"
    cp "$src" "$tmp"
    chmod 0440 "$tmp"
    if ! visudo -cf "$tmp" >/dev/null; then
      rm -f "$tmp"
      warn "sudoers inválido: $rel"
      return 1
    fi
    if [[ -f "$dst" && ! -L "$dst" ]] \
        && cmp -s "$tmp" "$dst" \
        && [[ "$(stat -c '%a' "$dst" 2>/dev/null || true)" == "440" ]]; then
      rm -f "$tmp"
      action "sudoers mantido sem regravar: $rel"
      continue
    fi
    backup_sudoers_live "$rel"
    mkdir -p "$(dirname "$dst")"
    install -m 0440 "$tmp" "$dst"
    rm -f "$tmp"
    CHANGED=1
    action "sudoers instalado: $rel"
  done < <(find "$src_dir" -type f -print0 | sort -z)
}

chmod_scripts() {
  # Não altera modos de arquivos rastreados no Git. O systemd chama scripts via
  # /usr/bin/env bash nos templates, justamente para não sujar o repo com
  # mode change 100644=>100755 e bloquear git pull --ff-only.
  action "scripts mantidos sem chmod para preservar repo limpo"
}

install_units() {
  local unit
  for unit in \
    tts-bot.service \
    bot-updater.service bot-updater.timer bot-updater.path \
    bot-updater-alert@.service \
    cleanup-audio-temp.service cleanup-audio-temp.timer \
    sinuca-activity-server.service \
    phone-worker-watch.service phone-worker-watch.timer; do
    install_file "$unit"
  done
  install_dir_files "tts-bot.service.d"
}

capture_updater_timer_state() {
  local unit prefix=bot-updater
  declare -gA UPDATER_TRIGGER_ENABLED=() UPDATER_TRIGGER_ACTIVE=()
  for unit in bot-updater.timer bot-updater.path tts-bot-updater.timer tts-bot-updater.path; do
    UPDATER_TRIGGER_ENABLED[$unit]=0
    UPDATER_TRIGGER_ACTIVE[$unit]=0
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      UPDATER_TRIGGER_ENABLED[$unit]=1
    fi
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
      UPDATER_TRIGGER_ACTIVE[$unit]=1
    fi
  done
  UPDATER_TIMER_WAS_ENABLED="${UPDATER_TRIGGER_ENABLED[$prefix.timer]}"
  UPDATER_TIMER_WAS_ACTIVE="${UPDATER_TRIGGER_ACTIVE[$prefix.timer]}"
  UPDATER_PATH_WAS_ENABLED="${UPDATER_TRIGGER_ENABLED[$prefix.path]}"
  UPDATER_PATH_WAS_ACTIVE="${UPDATER_TRIGGER_ACTIVE[$prefix.path]}"
}

updater_migration_ready() {
  local receipt="${UPDATER_MIGRATION_FILE:-$REPO_DIR/data/updater/systemd-migration.json}"
  [[ -f "$receipt" && ! -L "$receipt" ]] || return 1
  python3 - "$receipt" <<'PY_MIGRATION_READY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if data.get('ready') is True and data.get('service') == 'bot-updater.service' else 1)
PY_MIGRATION_READY
}

required_updater_template() {
  local rel="${1:?}" src
  src="$(template_source "$rel")"
  [[ -n "$src" && -s "$src" && ! -L "$src" ]] || {
    warn "template obrigatório ausente: $rel" >&2
    return 1
  }
  printf '%s' "$src"
}

preflight_updater_migration() {
  local unit src
  for unit in bot-updater.service bot-updater.timer bot-updater.path bot-updater-alert@.service; do
    src="$(required_updater_template "$unit")" || return 1
    # Não seguir máscaras ou links locais ao instalar templates privilegiados.
    if [[ -L "$SYSTEMD_DIR/$unit" ]]; then
      warn "unit canônica mascarada ou vinculada: $unit"
      return 1
    fi
    case "$unit" in
      bot-updater.service)
        grep -Fxq 'ExecStart=/usr/bin/env bash /home/ubuntu/bot/updater/core/atualizar.sh' "$src" || return 1 ;;
      bot-updater.timer|bot-updater.path)
        grep -Fxq 'Unit=bot-updater.service' "$src" || return 1 ;;
    esac
  done
  src="$UPDATER_SUDOERS_DIR/bot-updater-start"
  [[ -s "$src" && ! -L "$src" && ! -L "$SUDOERS_DIR/bot-updater-start" ]] || return 1
  visudo -cf "$src" >/dev/null || return 1
}

preflight_legacy_updater_removal() {
  local unit state found=0
  for unit in tts-bot-updater.service tts-bot-updater.timer tts-bot-updater.path tts-bot-alert@.service; do
    [[ ! -e "$SYSTEMD_DIR/$unit" && ! -L "$SYSTEMD_DIR/$unit" ]] || found=1
  done
  [[ ! -e "$SUDOERS_DIR/tts-bot-updater-start" && ! -L "$SUDOERS_DIR/tts-bot-updater-start" ]] || found=1
  [[ "$found" == 1 ]] || return 0
  if ! updater_migration_ready; then
    warn "remoção legada exige a migração 47a concluída"
    return 1
  fi
  state="$(systemctl show -p ActiveState --value tts-bot-updater.service)" || return 1
  case "$state" in
    inactive|failed) ;;
    *) warn "serviço legado ainda está em execução; conclua a Wave 47a antes da 47b"; return 1 ;;
  esac
  # De dentro do updater, a própria nova família precisa estar conduzindo a
  # transação. Não remover o único serviço operacional a partir da ponte antiga.
  if [[ "$FROM_UPDATER" == 1 ]]; then
    state="$(systemctl show -p ActiveState --value bot-updater.service)" || return 1
    case "$state" in
      active|activating|reloading) ;;
      *) warn "remoção legada requer execução pela nova família bot-updater"; return 1 ;;
    esac
  fi
  for unit in tts-bot-updater.timer tts-bot-updater.path; do
    if systemctl is-active --quiet "$unit" || systemctl is-enabled --quiet "$unit"; then
      warn "gatilho legado ainda habilitado/ativo: $unit"
      return 1
    fi
  done
  if [[ -n "$(systemctl list-units 'tts-bot-alert@*.service' --state=active,activating --no-legend --plain)" ]]; then
    warn "alerta legado ainda em execução"
    return 1
  fi
  # Drop-ins locais podem manter um OnFailure antigo apesar dos templates do
  # repositório. Detectar o consumidor antes de excluir seu destino.
  python3 - "$SYSTEMD_DIR" <<'PY_LEGACY_CONSUMERS'
import sys
from pathlib import Path
root = Path(sys.argv[1])
legacy = {'tts-bot-updater.service', 'tts-bot-updater.timer', 'tts-bot-updater.path', 'tts-bot-alert@.service'}
for pattern in ('*.service', '*.conf', '*.timer', '*.path'):
    for path in root.rglob(pattern):
        if path.is_symlink() or path.name in legacy or not path.is_file():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            value = line.strip()
            if value and not value.startswith(('#', ';')) and '=' in value:
                if 'tts-bot-alert@' in value or any('tts-bot-updater.' + ext in value for ext in ('service', 'timer', 'path')):
                    print(f'referência legada ainda ativa: {path}: {value}', file=sys.stderr)
                    raise SystemExit(1)
PY_LEGACY_CONSUMERS
}

remove_legacy_updater_files() {
  [[ "$DRY_RUN" != 1 ]] || { action "dry-run: removeria somente a família legada verificada"; return 0; }
  # Revalidar o serviço antigo após instalar as novas units. Isso cobre uma
  # partida concorrente entre o preflight e a limpeza dos arquivos.
  preflight_legacy_updater_removal || return 1
  local unit
  for unit in tts-bot-updater.service tts-bot-updater.timer tts-bot-updater.path tts-bot-alert@.service; do
    if [[ -e "$SYSTEMD_DIR/$unit" || -L "$SYSTEMD_DIR/$unit" ]]; then
      systemctl disable "$unit" >/dev/null || return 1
      rm -f -- "$SYSTEMD_DIR/$unit" || return 1
      CHANGED=1
    fi
  done
  if [[ -e "$SUDOERS_DIR/tts-bot-updater-start" || -L "$SUDOERS_DIR/tts-bot-updater-start" ]]; then
    rm -f -- "$SUDOERS_DIR/tts-bot-updater-start" || return 1
    CHANGED=1
  fi
  systemctl daemon-reload || return 1
  action "família legada removida; bot-updater é a infraestrutura canônica"
}


begin_updater_migration() {
  UPDATER_MIGRATION_OPEN=0
  [[ "$DRY_RUN" != "1" ]] || return 0
  UPDATER_MIGRATION_BACKUP="$BACKUP_DIR/updater-migration"
  UPDATER_MIGRATION_FILE="${UPDATER_MIGRATION_FILE:-$REPO_DIR/data/updater/systemd-migration.json}"
  mkdir -p "$UPDATER_MIGRATION_BACKUP"
  local unit file
  declare -ga UPDATER_SAVED_FILES=() UPDATER_EXISTING_FILES=()
  for unit in bot-updater.service bot-updater.timer bot-updater.path bot-updater-alert@.service \
      tts-bot-updater.service tts-bot-updater.timer tts-bot-updater.path tts-bot-alert@.service \
      tts-bot.service; do
    save_updater_migration_file "$SYSTEMD_DIR/$unit"
  done
  for file in "$SYSTEMD_DIR"/tts-bot.service.d/*.conf; do
    [[ -e "$file" || -L "$file" ]] || continue
    save_updater_migration_file "$file"
  done
  save_updater_migration_file "$SUDOERS_DIR/bot-updater-start"
  save_updater_migration_file "$SUDOERS_DIR/tts-bot-updater-start"
  save_updater_migration_file "$UPDATER_MIGRATION_FILE"
  UPDATER_MIGRATION_OPEN=1
}

save_updater_migration_file() {
  local file="${1:?}" index="${#UPDATER_SAVED_FILES[@]}" existed=0
  if [[ -e "$file" || -L "$file" ]]; then
    cp -a -- "$file" "$UPDATER_MIGRATION_BACKUP/$index"
    existed=1
  fi
  UPDATER_SAVED_FILES+=("$file")
  UPDATER_EXISTING_FILES+=("$existed")
}

set_updater_trigger_state() {
  local unit="${1:?}" enabled="${2:?}" active="${3:?}"
  if [[ "$enabled" == 1 ]]; then
    systemctl enable "$unit" >/dev/null || return 1
  else
    systemctl disable "$unit" >/dev/null || return 1
  fi
  if [[ "$active" == 1 ]]; then
    systemctl start "$unit" || return 1
  else
    systemctl stop "$unit" || return 1
  fi
}

apply_updater_service_policy() {
  local unit
  # A cópia antiga do updater pode continuar rodando até encerrar a transação.
  # Desativar timer/path não para esse serviço; o lock continua compartilhado.
  for unit in tts-bot-updater.timer tts-bot-updater.path; do
    if [[ -e "$SYSTEMD_DIR/$unit" || -L "$SYSTEMD_DIR/$unit" ]]; then
      systemctl disable --now "$unit" >/dev/null || return 1
    fi
  done
  if [[ "$FROM_UPDATER" != 1 ]]; then
    UPDATER_TIMER_WAS_ENABLED=1
    UPDATER_TIMER_WAS_ACTIVE=1
    UPDATER_PATH_WAS_ENABLED=1
    UPDATER_PATH_WAS_ACTIVE=1
  fi
  set_updater_trigger_state bot-updater.timer "$UPDATER_TIMER_WAS_ENABLED" "$UPDATER_TIMER_WAS_ACTIVE" || return 1
  set_updater_trigger_state bot-updater.path "$UPDATER_PATH_WAS_ENABLED" "$UPDATER_PATH_WAS_ACTIVE" || return 1
  action "bot-updater.path/timer sincronizados; timer mantido como fallback; manutenção preservada"
}

verify_updater_installation() {
  [[ "$DRY_RUN" != 1 ]] || return 0
  local unit src actual expected active
  for unit in bot-updater.service bot-updater.timer bot-updater.path bot-updater-alert@.service; do
    src="$(required_updater_template "$unit")" || return 1
    [[ -f "$SYSTEMD_DIR/$unit" && ! -L "$SYSTEMD_DIR/$unit" ]] || return 1
    cmp -s "$src" "$SYSTEMD_DIR/$unit" || return 1
    if [[ "$unit" != bot-updater-alert@.service ]]; then
      [[ "$(systemctl show -p LoadState --value "$unit")" == loaded ]] || return 1
      [[ "$(systemctl show -p FragmentPath --value "$unit")" == "$SYSTEMD_DIR/$unit" ]] || return 1
    fi
  done
  cmp -s "$UPDATER_SUDOERS_DIR/bot-updater-start" "$SUDOERS_DIR/bot-updater-start" || return 1
  visudo -cf "$SUDOERS_DIR/bot-updater-start" >/dev/null || return 1
  for unit in bot-updater.timer bot-updater.path; do
    expected="$UPDATER_TIMER_WAS_ENABLED"; active="$UPDATER_TIMER_WAS_ACTIVE"
    if [[ "$unit" == bot-updater.path ]]; then
      expected="$UPDATER_PATH_WAS_ENABLED"; active="$UPDATER_PATH_WAS_ACTIVE"
    fi
    actual=0
    if systemctl is-enabled --quiet "$unit"; then actual=1; fi
    [[ "$actual" == "$expected" ]] || return 1
    actual=0
    if systemctl is-active --quiet "$unit"; then actual=1; fi
    [[ "$actual" == "$active" ]] || return 1
  done
}

commit_updater_migration() {
  [[ "$DRY_RUN" != 1 ]] || return 0
  mkdir -p "$(dirname "$UPDATER_MIGRATION_FILE")"
  python3 - "$UPDATER_MIGRATION_FILE" <<'PY_MIGRATION_COMMIT'
import datetime, json, os, sys, tempfile
from pathlib import Path
path = Path(sys.argv[1])
fd, name = tempfile.mkstemp(prefix='.systemd-migration-', dir=path.parent)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump({'ready': True, 'service': 'bot-updater.service',
                   'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, path)
finally:
    if os.path.exists(name):
        os.unlink(name)
PY_MIGRATION_COMMIT
  UPDATER_MIGRATION_OPEN=0
}

updater_install_failed() {
  local rc="${1:-1}" index file unit restore_ok=1
  trap - ERR
  if [[ "${UPDATER_MIGRATION_OPEN:-0}" == 1 ]]; then
    for index in "${!UPDATER_SAVED_FILES[@]}"; do
      file="${UPDATER_SAVED_FILES[$index]}"
      if ! rm -f -- "$file"; then restore_ok=0; continue; fi
      if [[ "${UPDATER_EXISTING_FILES[$index]}" == 1 ]]; then
        if ! cp -a -- "$UPDATER_MIGRATION_BACKUP/$index" "$file"; then restore_ok=0; fi
      fi
    done
    systemctl daemon-reload || restore_ok=0
    for unit in bot-updater.timer bot-updater.path tts-bot-updater.timer tts-bot-updater.path; do
      if [[ -e "$SYSTEMD_DIR/$unit" || -L "$SYSTEMD_DIR/$unit" ]]; then
        set_updater_trigger_state "$unit" "${UPDATER_TRIGGER_ENABLED[$unit]}" "${UPDATER_TRIGGER_ACTIVE[$unit]}" || restore_ok=0
      else
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
      fi
    done
    warn "falha na migração; restauração dos arquivos/gatilhos: $restore_ok; backup: $UPDATER_MIGRATION_BACKUP"
  fi
  exit "$rc"
}


apply_service_policy() {
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  systemctl daemon-reload
  if [[ "${JOURNALD_POLICY_CHANGED:-0}" == "1" ]]; then
    if systemctl restart systemd-journald.service; then
      action "limite persistente do journal aplicado"
    else
      warn "política do journal instalada, mas o reload falhou"
    fi
  fi
  if [[ "${TMPFILES_POLICY_CHANGED:-0}" == "1" ]]; then
    if systemd-tmpfiles --clean "$TMPFILES_POLICY_DEST"; then
      action "retenção do cache Snap aplicada"
    else
      warn "política tmpfiles instalada, mas a limpeza inicial falhou"
    fi
  fi
  systemctl reset-failed tts-bot.service bot-updater.service bot-updater.path bot-updater-alert@tts-bot.service >/dev/null 2>&1 || true
  systemctl enable tts-bot.service >/dev/null 2>&1 || true

  apply_updater_service_policy

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

  systemctl disable --now phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
  action "phone-lavalink-watch removido do fluxo: Lavalink/NodeLink não é mais usado"
}

prune_systemd_backups() {
  local keep_count="${SYSTEMD_BACKUP_KEEP_COUNT:-3}"
  local -a backup_names=()
  local index name target
  [[ "$DRY_RUN" != "1" && -d "$BACKUP_ROOT" && ! -L "$BACKUP_ROOT" ]] || return 0
  [[ "$keep_count" =~ ^[0-9]+$ ]] || keep_count=3
  (( keep_count < 1 )) && keep_count=1
  (( keep_count > 20 )) && keep_count=20

  mapfile -t backup_names < <(
    find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
      | grep -E '^[0-9]{8}-[0-9]{6}$' \
      | sort -r || true
  )
  for ((index=keep_count; index<${#backup_names[@]}; index++)); do
    name="${backup_names[$index]}"
    [[ "$name" =~ ^[0-9]{8}-[0-9]{6}$ ]] || continue
    target="$BACKUP_ROOT/$name"
    [[ -d "$target" && ! -L "$target" && "$(dirname -- "$target")" == "$BACKUP_ROOT" ]] || continue
    rm -rf -- "$target"
    action "backup systemd antigo removido: $name"
  done
}

write_status() {
  local actions_json warnings_json backup_dir_value=""
  actions_json="$(printf '%s\n' "${ACTIONS[@]:-}" | python3 -c 'import json,sys; print(json.dumps([x for x in sys.stdin.read().splitlines() if x], ensure_ascii=False))')"
  warnings_json="$(printf '%s\n' "${WARNINGS[@]:-}" | python3 -c 'import json,sys; print(json.dumps([x for x in sys.stdin.read().splitlines() if x], ensure_ascii=False))')"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  [[ -d "$BACKUP_DIR" ]] && backup_dir_value="$BACKUP_DIR"
  cat > "$STATUS_FILE" <<EOF_STATUS
{"ok": true, "changed": ${CHANGED}, "from_updater": ${FROM_UPDATER}, "timestamp": "$(date -Iseconds)", "template_dir": "$TEMPLATE_DIR", "updater_system_dir": "$UPDATER_SYSTEM_DIR", "updater_sudoers_dir": "$UPDATER_SUDOERS_DIR", "backup_dir": "$backup_dir_value", "actions": $actions_json, "warnings": $warnings_json}
EOF_STATUS
  chown ubuntu:ubuntu "$STATUS_FILE" 2>/dev/null || true
}

audit_one_file() {
  local rel="$1" src="" dst="$SYSTEMD_DIR/$rel"
  src="$(template_source "$rel")"
  if [[ ! -f "$src" ]]; then
    warn "audit: template ausente: $rel"
    return 0
  fi
  if [[ ! -e "$dst" && ! -L "$dst" ]]; then
    action "audit: só no repo: $rel"
    return 0
  fi
  if [[ -L "$dst" ]]; then
    local target
    target="$(readlink "$dst" 2>/dev/null || true)"
    if [[ "$target" == "/dev/null" ]]; then
      action "audit: live mascarado: $rel -> /dev/null"
    else
      warn "audit: live é symlink inesperado: $rel -> $target"
    fi
    return 0
  fi
  if cmp -s "$src" "$dst"; then
    action "audit: igual: $rel"
  else
    warn "audit: diferente: $rel"
  fi
}

audit_vps_systemd() {
  local unit src rel live name
  action "audit: comparando templates do repo com $SYSTEMD_DIR"
  for unit in \
    tts-bot.service \
    bot-updater.service bot-updater.timer bot-updater.path \
    bot-updater-alert@.service \
    cleanup-audio-temp.service cleanup-audio-temp.timer \
    sinuca-activity-server.service \
    phone-worker-watch.service phone-worker-watch.timer; do
    audit_one_file "$unit"
  done
  if [[ -d "$TEMPLATE_DIR/tts-bot.service.d" ]]; then
    while IFS= read -r -d '' src; do
      rel="tts-bot.service.d/${src#$TEMPLATE_DIR/tts-bot.service.d/}"
      audit_one_file "$rel"
    done < <(find "$TEMPLATE_DIR/tts-bot.service.d" -type f -print0 | sort -z)
  fi

  while IFS= read -r -d '' live; do
    name="${live#$SYSTEMD_DIR/}"
    case "$name" in
      *.backup.*|*.disabled.*|*.disabled|*.tmp) continue ;;
      tts-bot.service|bot-updater.service|bot-updater.timer|bot-updater.path|bot-updater-alert@.service|cleanup-audio-temp.service|cleanup-audio-temp.timer|sinuca-activity-server.service|phone-worker-watch.service|phone-worker-watch.timer|tts-bot.service.d/*)
        [[ -n "$(template_source "$name")" ]] || warn "audit: existe só na VPS: $name"
        ;;
      lavalink.service|lavalink.service.d/*)
        action "audit: Lavalink VPS legado ignorado/mantido fora: $name"
        ;;
    esac
  done < <(find "$SYSTEMD_DIR" -maxdepth 2 \( -type f -o -type l \) -print0 2>/dev/null | sort -z)

  for policy in \
    "$JOURNALD_POLICY_SRC|$JOURNALD_POLICY_DEST|limite do journal" \
    "$TMPFILES_POLICY_SRC|$TMPFILES_POLICY_DEST|retenção do cache Snap"; do
    IFS='|' read -r src live name <<< "$policy"
    if [[ -f "$src" && -f "$live" && ! -L "$live" ]] && cmp -s "$src" "$live"; then
      action "audit: política igual: $name"
    elif [[ ! -f "$live" && ! -L "$live" ]]; then
      action "audit: política existe só no repo: $name"
    else
      warn "audit: política diferente ou insegura: $name"
    fi
  done
}

audit_crontab() {
  local current tmp
  current="${TMPDIR:-/tmp}/vps-cron-audit-current.$$"
  tmp="${TMPDIR:-/tmp}/vps-cron-audit-normalized.$$"
  if ! sudo -u ubuntu -H crontab -l > "$current" 2>/dev/null; then
    action "audit: crontab ubuntu ausente ou ilegível"
    rm -f "$current" "$tmp"
    return 0
  fi
  python3 - "$current" "$tmp" <<'PY_AUDIT_CRON'
import sys
from pathlib import Path
src = Path(sys.argv[1])
dst = Path(sys.argv[2])
text = src.read_text(encoding='utf-8', errors='replace')
# Only detect the exact emergency mistake here; normalization itself is handled
# by normalize_crontab() so audit remains read-only.
broken = any(line.strip() == '>/dev/null 2>&1' for line in text.splitlines())
dst.write_text('broken_redirect=' + ('1' if broken else '0') + '\n', encoding='utf-8')
PY_AUDIT_CRON
  if grep -q '^broken_redirect=1$' "$tmp" 2>/dev/null; then
    warn "audit: crontab ubuntu tem redirect solto; rode instalador sem --audit para corrigir"
  else
    action "audit: crontab ubuntu sem redirect solto"
  fi
  rm -f "$current" "$tmp"
}

run_audit() {
  audit_vps_systemd
  audit_crontab
}

main() {
  require_root
  ensure_paths
  if [[ "$AUDIT_ONLY" == "1" ]]; then
    run_audit
    write_status
    log "auditoria concluída; nenhuma alteração aplicada"
    return 0
  fi
  preflight_updater_migration
  preflight_legacy_updater_removal
  capture_updater_timer_state
  begin_updater_migration
  trap 'updater_install_failed "$?"' ERR
  chmod_scripts
  install_units
  install_storage_policies
  install_sudoers_files
  sanitize_lavalink_references
  mask_vps_lavalink
  normalize_crontab
  apply_service_policy
  verify_updater_installation
  remove_legacy_updater_files
  verify_updater_installation
  commit_updater_migration
  trap - ERR
  prune_systemd_backups
  write_status
  if [[ -d "$BACKUP_DIR" ]]; then
    log "concluído; backup desta execução em $BACKUP_DIR"
  else
    log "concluído; nenhum backup novo foi necessário"
  fi
}

main "$@"
