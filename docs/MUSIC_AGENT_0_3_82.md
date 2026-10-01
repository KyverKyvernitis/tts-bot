# Music Agent 0.3.82: reprodução e arquivo no Discord

Esta atualização mantém o áudio definitivo apenas no canal de fórum já
configurado. Os diretórios temporários continuam disponíveis para downloads,
conversão, divisão e retomada de uploads. A reprodução de arquivos do fórum
usa streaming e buffers em RAM; não cria um acervo local de músicas.

## Reprodução

- `_play` de um link conhecido consulta primeiro o índice do arquivo. Uma faixa
  arquivada entra na fila sem uma nova consulta de metadados ao provedor.
  Links de álbuns e playlists mantêm o fluxo de expansão existente.
- URLs do CDN são renovadas conforme a expiração assinada, com margem de
  segurança. Pedidos simultâneos compartilham a mesma resolução; as URLs
  permanecem apenas no cache volátil e são invalidadas após falhas.
- O manifesto do arquivo registra codec, taxa, canais e duração reais, obtidos
  por ffprobe. O arquivamento copia os pacotes comprimidos quando o formato
  permite; o fallback converte uma única vez a partir do original.
- Músicas maiores que o upload permitido são divididas em anexos da mesma
  thread e continuam sendo uma única faixa na fila. O agente prepara somente
  a parte seguinte, mantém o mixer e aplica seek na parte correspondente.
- Efeitos em faixas divididas usam processamento contínuo, mantendo o estado
  do reverb entre as partes. Volume, TTS e ducking continuam funcionando.
- A confirmação de início ignora silêncio criado por falta de frames. Ganho
  de TTS e mistura estéreo respeitam a proteção contra clipping.

## Arquivamento e aprendizagem

O limite fixo de 600 segundos e o limite fixo de 20 MiB foram removidos. Cada
anexo respeita `guild.filesize_limit`, com margem de upload. Downloads usam
orçamento de disco livre e monitoramento de crescimento, inclusive em fontes
fragmentadas. Falta de espaço, falhas de rede e limitações transitórias voltam
para a fila com backoff; não apagam o item aprendido nem o pausam para sempre
depois de cinco tentativas. O trabalho continua enquanto o agente responde,
inclusive em arquivamentos demorados.

Downloads e referências parciais ficam em
`<tempdir>/core-music-archive-<uid>/<chave>` e permitem retomar um upload
interrompido. Os segmentos já preparados são reutilizados após verificar sua
origem, hashes e tamanhos; isso evita repetir uploads por mudanças de bytes
do contêiner a cada preparação. Todos os bytes são removidos após confirmar os anexos e o
manifesto. Trabalhos abandonados expiram em 24 horas por padrão, mesmo que não
haja novos uploads; locks protegem trabalhos em andamento.

`MUSIC_AGENT_ARCHIVE_TEMP_TTL_SECONDS` ajusta essa janela entre 60 segundos e
7 dias. Não é retenção permanente nem cache de reprodução. O limite de
concorrência e os buffers continuam limitados para proteger VPS e Termux.

As escolhas e aliases aprendidos persistem em SQLite, sem descarte por
quantidade. `MUSIC_SEARCH_CHOICE_MEMORY_MAX_ENTRIES` limita somente o cache em
RAM. Consultas exatas usam índices em disco; consultas aproximadas usam FTS5
quando disponível e um índice de tokens/trigramas como fallback. O orçamento
`MUSIC_SEARCH_CHOICE_MEMORY_CANDIDATE_LIMIT` limita o trabalho por busca, não
o tamanho da aprendizagem. Escolha, índices e evento pendente de arquivamento
são gravados na mesma transação. O coordenador confirma eventos após aceitá-los
na fila persistente, com ordem FIFO.

Os bancos locais guardam metadados, referências, filas e aprendizagem. O áudio
definitivo e o manifesto ficam no fórum atual. A capacidade real continua
dependendo do armazenamento para os bancos, do espaço temporário disponível e
dos limites de upload e API do Discord.

## Atualização

1. Aplicar o pacote sobre a raiz do repositório e reiniciar o bot na VPS pelo
   procedimento já usado na instalação.
2. Usar a atualização existente do Phone Worker/Termux para distribuir a mesma
   fonte e reiniciar o Music Agent. O agente deve anunciar `0.3.82`.
3. Conferir `_play` de uma faixa conhecida, seek numa faixa dividida e TTS
   durante a reprodução. Manter o canal de fórum configurado atualmente.

O produtor e o worker incluem todos os novos módulos. A release imutável
continua cabendo nos 64 membros aceitos pelo bootstrap 1.0.0: documentação,
exemplos de configuração e dois utilitários de instalação permanecem no
checkout e no instalador, fora da release de execução. O hash é calculado
sobre o mesmo conjunto no produtor e no aparelho.

Arquivos antigos do fórum continuam legíveis. A migração atualiza a mesma
thread e só considera a nova versão disponível após confirmar sua publicação.
O bot exige agente 0.3.82 para os novos uploads; a atualização não altera
tokens, cookies ou o arquivo de ambiente instalado.

Dependências: as já usadas pelo projeto, incluindo discord.py, aiohttp,
yt-dlp, FFmpeg e ffprobe. FTS5 é opcional. Nenhum serviço de armazenamento
externo ou nova dependência Python é obrigatório.

Os testes automatizados cobrem durabilidade, retomada, referências, streaming,
divisão de Opus com FFmpeg real e compatibilidade do pacote com o bootstrap
original. Latência e qualidade em uma call real ainda precisam ser medidas
no aparelho e na rede de produção; não há promessa de um tempo fixo de início.
