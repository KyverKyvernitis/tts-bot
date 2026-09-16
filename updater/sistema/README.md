# Infraestrutura do updater

Este diretório contém a infraestrutura systemd que pertence ao updater.
A Wave 47a instala `bot-updater.*` e preserva os arquivos das units antigas
para permitir a transição do processo em execução. A Wave 47b remove as quatro
units antigas e o sudoers legado após validar a migração, a inatividade do
serviço antigo e a ausência de consumidores de `OnFailure` antigos.
Timer e path preservam individualmente seus estados habilitado/ativo, inclusive
quando desativados para manutenção. O instalador valida templates e sudoers
antes de aplicar; uma falha restaura a família anterior sem parar o updater.

Após a 48, overlays incompletos são rejeitados antes da escrita de units. O
serviço novo aguarda o lock compartilhado quando a execução anterior ainda
está terminando. Uma ativação via `.path` assim não fica reiniciando em ciclo.

Arquivos canônicos:

- `bot-updater.service`: execução transacional do updater;
- `bot-updater.path`: disparo imediato quando entra candidato na fila;
- `bot-updater.timer`: fallback periódico do `.path`;
- `bot-updater-alert@.service`: alerta de falha usado pelo bot/systemd;
- `instalar.sh`: sincroniza units, sudoers e políticas operacionais na VPS.

As units próprias do updater não possuem mais cópias em `deploy/systemd/`, e o
instalador antigo em `scripts/` foi removido. Templates gerais da VPS continuam
em `deploy/systemd/` porque pertencem a outros subsistemas.
