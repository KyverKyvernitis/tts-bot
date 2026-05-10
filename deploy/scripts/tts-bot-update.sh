#!/usr/bin/env bash
set -Eeuo pipefail

# Mantém compatibilidade com instalações antigas que executam
# /usr/local/bin/tts-bot-update.sh. A lógica real fica no repositório para que
# futuros patches atualizem o updater sem depender de copiar este arquivo de novo.
exec /home/ubuntu/bot/scripts/tts-bot-update.sh "$@"
