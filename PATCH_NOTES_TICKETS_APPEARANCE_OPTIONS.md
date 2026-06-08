# Tickets — aparência do painel, opções e emojis

- Separado o editor de texto do painel da aparência.
- Adicionada área `🎨 Aparência` no `ticketedit` para editar cor, imagem principal e imagem lateral.
- Adicionado `side_image_url` no painel para imagem lateral/thumbnail quando o cliente/lib suportar Components V2 com thumbnail.
- Aumentado o campo de emoji da opção para aceitar emojis personalizados completos, incluindo `<:nome:id>` e `<a:nome:id>`.
- Convertidos emojis customizados para `PartialEmoji` ao montar selects.
- Adicionado botão cinza `🗑️` no editor de opções do painel.
- Opções customizadas são removidas; opções nativas são desativadas.
- Bloqueia remoção/desativação quando isso deixaria o painel sem nenhuma opção ativa.
