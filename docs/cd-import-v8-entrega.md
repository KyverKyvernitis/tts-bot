# Importação de CDs — correção v8 e recuperação de Rolling Girl

Entregas: `osaka-cd-vps-incremental-v8.zip`, `osaka-cd-termux-kit-v8.zip` e
`osaka-rolling-girl-r1.zip`. O patch da VPS contém código, documentação e testes;
o ZIP do CD contém somente os artefatos de Rolling Girl. Todos os caminhos
internos são relativos à raiz esperada pelo updater. Nenhum pacote contém
credenciais, cookies, banco de dados ou alterações ao Phone Worker.

## Estado verificável

| Parte | Resultado |
|---|---|
| ZIP principal antigo `osaka-cd-import-20261013-072007-bceafe9c.zip` | Não incluído em nenhum anexo; não há acesso ao Downloads do telefone para afirmar se existe lá. Foi gerado um pacote novo com nome distinto. |
| MIDI simplificado | Fonte exata bceafe9c…, 72 notas, 13.413 ms. |
| Partitura recuperada | Notas/velocidades/onsets/durações iguais às do parser v7 no modo lyrics=message. Hash canônico ecb0a351…. |
| MP3 fornecido | SHA 6c5b39a5…, 217.382 bytes; FFprobe 13,557551 s; decode completo, sinal não silencioso. |
| Vídeo oficial | `vnw8zURAxkU`, wowaka / Rollin Girl / ローリンガール. |
| Vídeo selecionado antes | `xPhPHO-sKlY` é um tributo ao vivo de Magical Mirai 2019; versão distinta rejeitada. |
| Visualizações oficiais | 44.107.888 em snapshot do índice web consultado, com origem/data registradas; não contador atual. Refresh live pendente. |
| Thumbnail | URL oficial registrada; JPEG local pendente. Não houve download nesta sessão. |
| Emoji/cor de Rolling Girl | Dependem do JPEG correto; pendentes. Código de hash/invalidação corrigido. |
| Catálogo isolado | 10 CDs preservados + Rolling Girl; hot reload e consumidor real da amostra validados. |
| VPS / Discord real | Não acessíveis nesta sessão; nenhum pacote foi instalado ou publicado remotamente. |
| Síntese física nova | Não executada: assets UTAU/resampler e env reais não estão nos anexos. MP3 existente foi conservado; backend 1.11.41 inalterado. |

A amostra foi migrada com a partitura original recuperada e os hashes de origem.
Isso comprova o vínculo dos artefatos fornecidos; não representa uma nova
transcrição acústica nem uma nova síntese no telefone. A adaptação simplificada
continua identificada como instrumental para teste, sem confirmação vocal.

## Aplicar na VPS

1. Aplique primeiro `osaka-cd-vps-incremental-v8.zip` pelo updater existente e
   aguarde sua conclusão. Essa atualização de código pode seguir a política
   normal de reinício do bot. O worker do telefone não é atualizado.
2. Depois aplique `osaka-rolling-girl-r1.zip`. O updater novo mescla por ID e
   conserva outros CDs. O catálogo recarrega sem novo reinício. Essa versão
   possui partitura, metadados e amostra; declara capa pendente.
3. Abra `_cd`, selecione Rolling Girl e use **Ouvir amostra** com Teto/pt-BR.
   Confirme resposta privada, playback completo e ausência de ida à fila/call.
   Selecione **Usar esse CD** e confira a preferência persistida.
4. Quando o kit gerar o pacote com JPEG, aplique a versão resultante e confira
   capa local, emoji reutilizado, borda colorida e atualização de painéis ativos.
   Painéis expirados devem continuar expirados.

Aplique os pacotes sequencialmente. Candidatos concorrentes que conflitem são
recusados pelo Git com segurança; reimporte depois de aplicar o anterior.
Não envie MIDIs ao Discord. Para o futuro, o telefone envia ZIPs de artefatos
pela rota HTTP, sem mensagens Discord.

## Recuperar no Termux e completar a capa

Extraia o kit em `~/osaka-cd-termux-kit-v8`. Dependências: Python, Bash,
FFmpeg/FFprobe, Pillow, aiohttp e yt-dlp; são separadas do renderer já instalado.
Se necessário instale apenas dependências do importador (`pkg install python
ffmpeg` e `python -m pip install pillow aiohttp yt-dlp`); não instale kits UTAU
antigos nem sobrescreva a release ativa. O kit não faz essas instalações sozinho.
O importador usa o arquivo confiável `~/.phone-worker.env` (ou PHONE_WORKER_ENV).

Execute na raiz do kit:

```sh
python deploy/ttv-cd/recover-rolling-girl.py
python deploy/ttv-cd/import-midi.py --inbox examples/rolling-girl --retry --no-sample
```

A recuperação copia somente os dados do CD para o catálogo local, preserva outros
IDs e gera um ZIP com áudio pronto. A segunda chamada pesquisa metadados/contagens
atuais e obtém a capa pelo pipeline de música; conserva o MP3 porque o hash musical
é o mesmo. O sidecar inclui aliases `Rollin Girl` e `ローリンガール`, com o vídeo
official como seed, sem lógica específica no importador. Os relatórios dizem
`complete: false` até JPEG e metadados estarem prontos. Arquivos em Downloads têm
caminho absoluto, tamanho, SHA-256 e validação explícitos no relatório.

Para outros MIDIs, use nomes `Artista - Música.mid` e coloque-os em
`~/storage/downloads/osaka-cd-midi`. Use sidecar para aliases ou seletores quando
necessário, sem mudar código. Inicie:

```sh
python deploy/ttv-cd/import-midi.py --watch
```

O MIDI original de Rolling Girl fica separado em `examples/polyphonic`, com ID
`rolling-girl-polyphonic-test`; não substitui o teste simplificado de 72 notas.
A extração real produziu 128 notas / 43,990 s de um arranjo de 2.880 notas /
188,663 s. Usa faixa/canal + voz superior heurística, preserva andamento e marca
ambiguidade. Os limites 60 s/128 notas do worker são respeitados por recorte; não
foram removidos nem ampliados sem suporte do runtime.

## Publicação HTTP opcional

A rota nova reutiliza a fila, worktree, validações e rollback do updater. Está
**desativada por padrão**. Na configuração do serviço da VPS, habilite
`OSAKA_CD_PUBLISH_ENABLED=1` e configure `OSAKA_CD_PUBLISH_TOKEN` com um segredo
aleatório separado, de pelo menos 32 caracteres. Exponha o webserver por HTTPS
pela infraestrutura existente. No processo do kit, forneça o mesmo token somente
pelo ambiente e use:

```sh
python deploy/ttv-cd/import-midi.py --watch --publish-url https://SEU-HOST/api/osaka/cd/publish
```

Não coloque o token em argumentos, sidecars ou ZIPs. O cliente recusa HTTP e
redirecionamentos, valida o pacote e envia seu SHA. Resposta `queued` indica
recebimento; `unchanged` é retry idempotente. Ambos mantêm `installed: false` até
verificação independente do updater. Falhas de publicação ficam pendentes e
reutilizam o mesmo ZIP validado. A sessão não configurou nem chamou uma VPS real.

## Correções e testes

- Env UTAU carregado com semântica do launcher em subprocesso isolado. Renderer
  1.11.41 importado por namespace próprio; versão antiga não pode sombreá-lo.
  Nenhum bytecode/cache é gravado na release; cache de síntese é separado.
- Extração MIDI com tempo global, overlaps FIFO, faixa/canal, recorte preservando
  pausas e registro de ambiguidade. Entrada/config alterada invalida a falha.
- `--retry` ignora cooldown manualmente; erros permanentes aguardam correção,
  erros temporários recebem backoff. Recuperação exporta um CD consistente.
- SHA MIDI distinto do SHA musical; preview v2 e manifesto por CD. Mudança de
  capa conserva canto; mudança musical invalida áudio e exige revisão superior.
- Mescla incremental validada, rejeição de traversal/duplicatas/artefatos sem
  índices e decodificação JPEG/MP3. Rota aceita somente dados CD, sem código.
- Watcher inclui JPG/MP3/índices; página de 25 opções; painel expirado não revive.

106 casos/assertions passaram em testes isolados: 46 extração MIDI via runner
stdlib (subset pytest de raises/parametrize), 14 metadados, 9 importação, 9
catálogo/amostra, 10 pacote/updater, 3 handler HTTP, 7 configuração UTAU, 4 cliente
HTTP e 4 recuperação. Outros 1.000 arranjos gerados passaram pelo validador real
do worker. JPEGs/MP3s nos testes de artefatos foram decodificados de fato; áudio
sintético nesses testes valida o transporte/codec, não a voz da Teto. O MP3 de
Rolling Girl foi testado separadamente.

A execução dos testes substituiu `asyncio.to_thread` por chamadas inline onde o
sandbox não permite wakeup socket; o pipeline de produção conserva os threads.
A suíte de SDK Discord está escrita, mas não executada: discord.py/pytest não
estavam instalados. Testes HTTP executaram o handler real com request double,
sem servidor Flask/WSGI real. Não há teste de publicação por rede nem sessão
Discord ativa. Essas limitações constam nas evidências e não viraram `complete`.

Veja `INSTRUCOES-IMPORTACAO.md`, `METADATA.md` e `updater/README.md` no código;
`evidence/` no kit conserva relatórios e fontes públicas, sem valores secretos.
