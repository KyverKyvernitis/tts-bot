# Patch — Cog de Tickets modularizada

## Comandos
- `ticket`: publica/republica o painel público de atendimento no canal atual.
- `ticketedit`: abre o editor staff do sistema de tickets.

## Fluxos
- Parceria: mostra confirmação e cria ticket privado após confirmar.
- Denúncia: abre modal Components V2 com aviso e select de até 10 tipos configuráveis; depois cria ticket.
- Sugestão: abre modal e envia a sugestão para o canal configurado, sem criar ticket.
- Outros: abre modal simples e cria ticket privado.

## Organização
- Nova package modular `cogs/tickets/`.
- Configuração persistida no `SettingsDB` em `tickets` por servidor.
- Views persistentes para painel público e ações dos tickets.
- Transcript HTML opcional ao fechar ticket.
