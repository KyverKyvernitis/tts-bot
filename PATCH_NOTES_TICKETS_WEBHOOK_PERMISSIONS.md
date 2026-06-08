# Patch — Tickets: webhook visual e permissões editáveis

## Resumo

- Adiciona opção **Usar webhook do servidor** em `ticketedit > Opções`.
- Quando ligada, mensagens visuais do fluxo de atendimento tentam sair com **nome e foto do servidor**.
- Se faltar permissão de webhook ou a API recusar o envio por webhook, o bot cai automaticamente para envio normal.
- Adiciona área **🔐 Permissões** no select principal do `ticketedit`.
- Permite editar permissões separadas para:
  - `@everyone`
  - cargos staff
  - autor do ticket
- Aplica as permissões configuradas ao criar canais de ticket.
- O botão de adicionar usuário usa as mesmas permissões configuradas para o autor do ticket.

## Arquivos alterados

- `cogs/tickets/cog.py`
- `cogs/tickets/constants.py`
- `cogs/tickets/modals.py`
- `cogs/tickets/permissions.py`
- `cogs/tickets/utils.py`
- `cogs/tickets/views.py`
- `cogs/tickets/webhooks.py`
- `db.py`
- `changed_files.txt`

## Validação

Executado:

```bash
python3 -m compileall -q cogs/tickets db.py
```
