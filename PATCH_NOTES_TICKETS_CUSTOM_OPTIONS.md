# Patch — Tickets: opções personalizadas e editor de textos separado

## Incluído

- `ticketedit > Opções` agora tem `➕ Adicionar opção` no checkbox group.
- Ao marcar `➕ Adicionar opção` e enviar, o bot cria uma nova opção personalizada no painel.
- Novo submenu `🎛️ Opções do painel` no editor:
  - edita opções nativas e personalizadas;
  - permite mudar nome, emoji, descrição, fluxo e canal de destino.
- O painel público passa a ser montado pelas opções configuradas, não mais apenas pelos quatro tipos fixos.
- `Parceria`, `Denúncia`, `Sugestão` e `Outros` agora podem ter nome/emoji/descrição/fluxo ajustados.
- `Textos` agora abre um editor ephemeral separado com selects.
- Cada texto abre em um modal próprio, evitando o modal gigante que não cabia no celular.
- Fluxos suportados por opção:
  - `confirm_ticket`
  - `modal_ticket`
  - `modal_channel`
  - `direct_ticket`

## Compatibilidade

- Configurações antigas de `enabled` e `texts` são migradas automaticamente para `option_items`.
- Denúncia mantém suporte ao select de tipos configuráveis.
- Transcript, permissões, webhook do servidor e fechamento continuam usando as configs existentes.
