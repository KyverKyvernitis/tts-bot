#!/usr/bin/env bash
# Execute na VPS para encerrar somente o serviço opcional introduzido em 0.3.83.
set -euo pipefail
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  printf '%s\n' 'Execute com sudo bash deploy/music-agent-vps/return-to-termux.sh' >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  printf '%s\n' 'systemd não disponível; nenhuma alteração feita.'
  exit 0
fi
if systemctl cat music-agent-voice.service >/dev/null 2>&1; then
  systemctl disable --now music-agent-voice.service
  printf '%s\n' 'Serviço opcional de voz na VPS parado e desabilitado.'
else
  printf '%s\n' 'Serviço opcional de voz na VPS não instalado.'
fi
printf '%s\n' 'Use MUSIC_AGENT_VOICE_EXECUTOR=termux e MUSIC_AGENT_DIRECT_API_ENABLED=false no controller.'
printf '%s\n' 'Atualize/reinicie controller e Music Agent do Termux; secrets não foram alterados.'
