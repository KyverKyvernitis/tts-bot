# Patch — Tickets: imagem do painel e fluxo de fechamento

## Mudanças

- Adiciona campo `URL da imagem do painel` no modal `ticketedit > Painel público`.
- O painel público mostra a imagem configurada quando o link começa com `http://` ou `https://`.
- Remove o botão `Cancelar` da confirmação de parceria, deixando apenas `Criar ticket`.
- Simplifica as ações dentro do ticket, deixando apenas o botão `Fechar`.
- A confirmação de fechamento agora é enviada no próprio canal do ticket, não ephemeral.
- A confirmação de fechamento usa webhook com nome/foto do servidor quando `Usar webhook do servidor` estiver ligado.
- Mantém `Confirmar fechamento` e `Cancelar` na confirmação de fechamento.
- Transcript continua automático ao fechar quando estiver configurado.

## Validação

- `python3 -m compileall -q cogs/tickets db.py`
- Teste de integridade do ZIP com `unzip -t`.
