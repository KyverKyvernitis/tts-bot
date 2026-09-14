# Updater

Implementação canônica do atualizador do bot.

## Estrutura

- `core/atualizar.sh`: orquestrador e entrypoint principal.
- `core/configuracao.sh`: prioridade e configuração operacional.
- `core/estado.sh`: estado runtime persistido da execução.
- `core/git.sh`: operações Git e snapshots transacionais.
- `core/registros.sh`: logs, evidências, incidentes e alertas técnicos.
- `core/tempos.sh`: medição e formatação de tempos.
- `core/fila.sh`: fila de candidatos, arquivamento e dispatch.
- `core/validacao.sh`: preflight, saúde e validação de candidatos/remoto.
- `core/candidato.sh`: isolamento, artefatos, commit e promoção de candidatos locais.
- `core/progresso.sh`: status, progresso, histórico e entrega visual no Discord.
- `core/mudancas.sh`: diff, classificação de impacto, fast reload e proteção de mudanças locais.
- `core/aplicacao.sh`: systemd, deploy do bot/site, publicação e releases de runtime.
- `core/recuperacao.sh`: rollback transacional e tratamento de falhas.
- `utilitarios/`: auxiliares Python canônicos com nomes em português.
- `testes/`: contratos da arquitetura e helpers de teste.

Os caminhos antigos em `scripts/`, `deploy/scripts/` e `utility/update_*` são
fachadas transitórias de compatibilidade. Código novo deve apontar para
`updater/`.

O entrypoint executa uma cópia runtime estável de `atualizar.sh` e preserva em
`TTS_BOT_UPDATER_SOURCE_DIR` a revisão dos módulos carregada no início. Assim,
um update do próprio updater não mistura versões durante a mesma execução.

Na etapa atual da modularização, o restante do fluxo foi separado em:

- `core/manutencao.sh`: retenção, limpeza de artefatos e proteção de espaço em disco.
- `core/persistencia.sh`: estado durável do candidato e evidências de recuperação.
- `core/reversao.sh`: pedidos de reverter/reaplicar e publicação do resultado da reversão.
- `core/orquestracao.sh`: sequência transacional principal (fila local, rollback ou commit remoto, validação e deploy).
- `core/finalizacao.sh`: consolidação de saúde/tempos, card final, log técnico e entrega idempotente.

Com isso, `core/atualizar.sh` fica responsável principalmente por bootstrap, estado inicial, carregamento dos módulos, locks e traps transacionais.


## Discord

A integração do updater com o bot fica em `updater/discord/`:

- `cartoes.py`: renderer, estado visual, logs técnicos e reconciliação;
- `preparacao.py`: validação/extracao do ZIP e criação do candidato;
- `progresso.py`: endpoint interno, progressão e edição de status;
- `controles.py`: botões, detalhes, cancelamento, rollback e reaplicação;
- `eventos.py`: eventos do Discord, reload e recepção de anexos;
- `integracao.py`: composição dos mixins e inicialização do estado.

`bot.py` mantém apenas a composição com `IntegracaoDiscordUpdaterMixin` e os hooks gerais do bot.

## Infraestrutura da VPS

A infraestrutura própria do updater é canônica em `updater/sistema/` e
`updater/sudoers/`. Os caminhos antigos em `deploy/systemd/`,
`deploy/sudoers.d/` e `scripts/install-vps-systemd-units.sh` permanecem nesta
fase apenas como compatibilidade para pacotes e instalações anteriores.

## Infraestrutura da VPS

A infraestrutura própria do updater é canônica em `updater/sistema/` e
`updater/sudoers/`. Os caminhos antigos em `deploy/systemd/`,
`deploy/sudoers.d/` e `scripts/install-vps-systemd-units.sh` permanecem nesta
fase apenas como compatibilidade para pacotes e instalações anteriores.
