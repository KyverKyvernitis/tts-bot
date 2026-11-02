#!/usr/bin/env bash
set -euo pipefail
pururu_profile_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pururu_profile_helper="$pururu_profile_dir/configurar-straycat.py"
if [[ ! -f "$pururu_profile_helper" ]]; then
  pururu_profile_helper="$pururu_profile_dir/../../ttv-straycat/termux/configurar-straycat.py"
fi
for pururu_profile_arg in "$@"; do
  case "$pururu_profile_arg" in
    --restore-pururu-profile|--restore-pururu-profile=*|--pururu-profile|--pururu-profile=*)
      exec python "$pururu_profile_helper" "$@"
      ;;
  esac
done
exec python "$pururu_profile_helper" --pururu-profile pururu-ptbr-v1 "$@"
