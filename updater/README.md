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
- `utilitarios/`: auxiliares Python canônicos com nomes em português.
- `testes/`: contratos da arquitetura e helpers de teste.

Os caminhos antigos em `scripts/`, `deploy/scripts/` e `utility/update_*` são
fachadas transitórias de compatibilidade. Código novo deve apontar para
`updater/`.

O entrypoint executa uma cópia runtime estável de `atualizar.sh` e preserva em
`TTS_BOT_UPDATER_SOURCE_DIR` a revisão dos módulos carregada no início. Assim,
um update do próprio updater não mistura versões durante a mesma execução.
