#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${EUID}" -eq 0 ]]; then
  exec /usr/bin/python3 "$script_dir/ssh_phone_vps.py" "$@"
fi
exec sudo -n /usr/bin/python3 "$script_dir/ssh_phone_vps.py" "$@"
