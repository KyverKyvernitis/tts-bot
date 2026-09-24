# Music Agent 0.3.56 — fila lógica, múltiplas playlists e edição virtual

## Alterações

- A fila pública passa a expor `logical_queue_size`, `queue_layout` e `virtual_playlists`. `virtual_playlist` continua disponível como compatibilidade para clientes antigos.
- Cada inserção de playlist recebe `instance_id` próprio. A mesma playlist pode ser adicionada duas ou mais vezes sem colisão de cursor, cache ou ação de fila.
- Segmentos virtuais têm `block_end_offset`, permitindo representar partes independentes da mesma playlist sem materializar metadados ou áudio de toda a coleção.
- O shuffle passa a incluir a fila lógica inteira: faixas já prontas, músicas avulsas e segmentos de todas as playlists. Para preservar a arquitetura lazy, coleções grandes são representadas por blocos virtuais limitados; os blocos são misturados globalmente e cada bloco recebe uma permutação interna determinística aplicada tanto na UI quanto no refill do player.
- Itens de páginas virtuais recebem identidade estável (`instance_id` + índice de origem). Eles podem ser tocados agora, removidos ou movidos sem resolver os demais itens da playlist.
- Remover uma música virtual divide o cursor ao redor do índice excluído, evitando duplicação. Mover materializa apenas a faixa escolhida e a insere na posição lógica pedida.
- O controlador da VPS reconstrói páginas a partir do layout lógico completo, inclusive quando uma página atravessa duas playlists diferentes. Apenas os metadados que aparecem na página são consultados.
- O painel de fila volta a permitir salto direto de página por modal e o seletor também aparece em páginas virtuais. As mensagens técnicas sobre “playlist virtual” e “metadata sob demanda” foram removidas.

## Compatibilidade e limites

A reprodução continua lazy: shuffle, navegação e remoção não resolvem áudio de todas as músicas. O embaralhamento de playlists virtuais extensas usa uma permutação hierárquica: blocos limitados são distribuídos pela fila e os itens de cada bloco também são embaralhados. Isso faz todas as músicas participarem sem criar um objeto remoto/stream para cada faixa. A distribuição não pretende ser uma permutação uniforme perfeita entre 10 mil itens, mas evita o comportamento anterior de manter grandes janelas na ordem original.

Clientes antigos podem continuar usando `virtual_playlist`; clientes atualizados devem preferir `virtual_playlists` e `queue_layout` para conhecer a ordem lógica completa.

## Validação

Os testes específicos cobrem duas inserções da mesma playlist, páginas que atravessam múltiplas playlists, shuffle da fila lógica, remoção/movimento virtual sem duplicação, destinos inválidos sem mutação e a interface de salto/seleção virtual. A suíte completa do projeto deve ser executada antes da distribuição do pacote.
