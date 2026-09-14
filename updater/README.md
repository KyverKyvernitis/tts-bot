# Updater

Esta pasta é a raiz canônica do sistema de atualização do projeto.

## Estrutura

- `core/`: orquestração e regras centrais do updater.
- `utilitarios/`: segurança, snapshots Git, smoke de runtime e seleção de testes.
- `testes/`: testes específicos da estrutura modular do updater.
- `discord/`, `sistema/` e `sudoers/`: serão migrados em rodadas estruturais seguintes, mantendo fachadas temporárias nos caminhos antigos enquanto houver consumidores legados.

## Entrypoint

O entrypoint canônico é `updater/core/atualizar.sh`.

`scripts/tts-bot-update.sh` permanece apenas como fachada de compatibilidade e deve desaparecer quando todos os consumidores antigos tiverem sido migrados. O serviço systemd já executa diretamente o entrypoint canônico.

## Regra de modularização

As rodadas estruturais não alteram os contratos funcionais do updater. Fila, segurança, worktree, validação, promoção, rollback, recovery, dispatch por `.path`, tempos e integração Discord devem manter o comportamento do baseline anterior.
