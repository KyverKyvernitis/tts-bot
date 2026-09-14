#!/usr/bin/env bash
set -Eeuo pipefail

# Fachada temporária de compatibilidade. A implementação canônica pertence ao updater.
REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
exec /usr/bin/env bash "$REPO_DIR/updater/sistema/instalar.sh" "$@"
