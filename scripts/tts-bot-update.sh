#!/usr/bin/env bash
set -Eeuo pipefail

# Fachada de compatibilidade temporária. O updater canônico vive em
# updater/core/atualizar.sh. Mantemos este caminho enquanto instalações,
# automações e pacotes antigos ainda podem chamá-lo diretamente.
REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
exec /usr/bin/env bash "$REPO_DIR/updater/core/atualizar.sh" "$@"
