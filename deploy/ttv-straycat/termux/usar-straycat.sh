#!/usr/bin/env bash
set -euo pipefail
ttv_straycat_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python "$ttv_straycat_dir/configurar-straycat.py" "$@"
