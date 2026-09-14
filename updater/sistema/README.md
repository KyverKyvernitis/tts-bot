# Infraestrutura do updater

Este diretório contém a infraestrutura systemd que pertence ao updater. Os nomes
das units permanecem `tts-bot-updater.*` durante a migração para preservar o
estado e as dependências já instaladas na VPS.

Arquivos canônicos:

- `tts-bot-updater.service`: execução transacional do updater;
- `tts-bot-updater.path`: disparo imediato quando entra candidato na fila;
- `tts-bot-updater.timer`: fallback periódico do `.path`;
- `tts-bot-alert@.service`: alerta de falha usado pelo bot/systemd;
- `instalar.sh`: sincroniza units, sudoers e políticas operacionais na VPS.

As units próprias do updater não possuem mais cópias em `deploy/systemd/`, e o
instalador antigo em `scripts/` foi removido. Templates gerais da VPS continuam
em `deploy/systemd/` porque pertencem a outros subsistemas.
