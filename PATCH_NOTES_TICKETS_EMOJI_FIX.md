# Patch — Tickets emoji custom e interações do editor

Correções:

- Sanitiza emojis de opções antes de montar selects do Discord.
- Aceita emoji unicode normal e emoji custom completo (`<:nome:id>` e `<a:nome:id>`).
- Emoji custom truncado/quebrado não derruba mais Preview, Textos ou Opções do painel.
- Views dinâmicas usam fallback seguro quando o Discord recusa o emoji de um SelectOption.
- Mensagens/resumos exibem emoji já normalizado para evitar `<:emoji:id` quebrado na tela.

Arquivos alterados:

- `cogs/tickets/utils.py`
- `cogs/tickets/views.py`
- `cogs/tickets/modals.py`
- `cogs/tickets/cog.py`
