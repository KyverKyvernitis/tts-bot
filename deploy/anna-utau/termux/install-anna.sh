#!/usr/bin/env bash
set -euo pipefail

anna_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
anna_installer="$anna_script_dir/install-voicebank.py"
if [[ ! -f "$anna_installer" ]]; then
  anna_installer="$anna_script_dir/deploy/anna-utau/termux/install-voicebank.py"
fi
if [[ $# -eq 0 ]]; then
  set -- --download
fi
python "$anna_installer" "$@"

anna_dry_run=0
for anna_argument in "$@"; do
  [[ "$anna_argument" != "--dry-run" ]] || anna_dry_run=1
done
if [[ "$anna_dry_run" == "1" ]]; then
  exit 0
fi
anna_worker_dir="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
if [[ -f "$anna_worker_dir/start-phone-worker.sh" ]]; then
  bash "$anna_worker_dir/start-phone-worker.sh" --restart
else
  printf '%s\n' 'Banco instalado. Reinicie o phone worker para ativar a Anna Nyui.'
fi
