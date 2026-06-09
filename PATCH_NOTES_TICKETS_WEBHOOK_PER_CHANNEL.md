# Tickets — webhook por canal e avatar do servidor

Correção focada no envio visual por webhook do sistema de atendimento.

## Corrigido

- O webhook visual agora é tratado explicitamente como recurso por canal.
- Ao publicar/usar o painel em outro canal, o bot não tenta reaproveitar identidade/cache de outro canal.
- Webhooks antigos com nome `Atendimento` agora têm nome/avatar sincronizados pelo menos uma vez por processo.
- A foto do servidor é aplicada no webhook do canal e também enviada como `avatar_url` por mensagem.
- Se faltar permissão de `Gerenciar Webhooks`, o fluxo continua com fallback normal pelo bot.

## Arquivos alterados

- `cogs/tickets/webhooks.py`
