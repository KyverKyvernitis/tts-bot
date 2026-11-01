#!/usr/bin/env bash
set -euo pipefail

pururu_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pururu_installer="$pururu_script_dir/install-voicebank.py"
if [[ ! -f "$pururu_installer" ]]; then
  pururu_installer="$pururu_script_dir/deploy/pururu-utau/termux/install-voicebank.py"
fi
if [[ $# -eq 0 ]]; then
  set -- --download
fi
for pururu_argument in "$@"; do
  case "$pururu_argument" in
    --help|-h|--check-archive-library)
      python "$pururu_installer" "$@"
      exit $?
      ;;
  esac
done
pururu_dry_run=0
pururu_repair=0
for pururu_argument in "$@"; do
  [[ "$pururu_argument" != "--dry-run" ]] || pururu_dry_run=1
  [[ "$pururu_argument" != "--repair-runtime" ]] || pururu_repair=1
done
if [[ "$pururu_repair" != "1" ]] && ! python "$pururu_installer" --check-archive-library; then
  if [[ "$pururu_dry_run" == "1" ]]; then
    exit 1
  fi
  if command -v pkg >/dev/null 2>&1; then
    pkg install -y libarchive
  else
    printf '%s\n' 'Instale libarchive para ler o arquivo oficial RAR da Pururu.' >&2
    exit 1
  fi
fi
python "$pururu_installer" "$@"
if [[ "$pururu_dry_run" == "1" ]]; then
  exit 0
fi
pururu_worker_dir="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
if [[ -f "$pururu_worker_dir/start-phone-worker.sh" ]]; then
  bash "$pururu_worker_dir/start-phone-worker.sh" --restart
else
  printf '%s\n' 'Banco instalado. Reinicie o phone worker para ativar a Pururu Púrpura.'
fi
