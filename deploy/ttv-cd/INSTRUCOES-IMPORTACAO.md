# Importação de CDs — Termux Kit v9

Instalação e operação estão em [README-TERMUX-KIT.md](README-TERMUX-KIT.md).
O kit do telefone e a atualização inicial da VPS são entregas separadas.
Depois dessa instalação inicial, o watch publica novos CDs automaticamente,
sem envio manual de pacotes para o updater e sem reiniciar o bot.

## Sidecar e seleção musical

Coloque o MIDI em `~/storage/downloads/osaka-cd-midi/`. Nomes no formato
`Artista - Música.mid` definem artista e título. Um JSON ao lado do MIDI com
o mesmo nome-base permite declarar informações adicionais:

```json
{
  "id": "rolling-girl",
  "title": "Rolling Girl",
  "artist": "wowaka",
  "aliases": ["Rollin Girl", "ローリンガール"],
  "artist_aliases": ["現実逃避P"],
  "seed_video_ids": ["vnw8zURAxkU"],
  "max_parts": 3,
  "include_bass": true
}
```

`track_index` e `channel` indicam a melodia principal, com índices a partir de
zero; o canal 9 é percussão. `start_ms`, `max_duration_ms` (até 60000) e
`max_notes` (até 128 por parte) delimitam o trecho da amostra. O trecho usa
um deslocamento comum para todas as partes; sua duração pode ser reduzida
quando uma parte adicional ultrapassa o orçamento de notas.

O arranjo completo suporta até três partes, dez minutos, 24.000 notas
cantáveis e 60.000 notas originais. MIDI de até 8 MiB é aceito. Faixas,
notas originais, ticks, tempo, pedal e critérios de seleção permanecem na
proveniência. A extração registra ambiguidade vocal e descartes; não confirma
uma transcrição vocal nem inventa harmonias ausentes no arquivo.

`search_query` complementa a busca do player. `allow_versions` autoriza
explicitamente variantes como `cover`, `live`, `remix` ou `instrumental`;
sem esse campo, versões diferentes são rejeitadas. Informe artista e aliases
adequados para identificação. Se o MIDI representa a música inteira,
`source_is_full_song: true` permite comparar sua duração com a do vídeo.
Para trechos/adaptações, omita esse campo; a duração total conhecida pode ser
informada em `expected_song_duration_seconds`.

A busca compara contagens verificadas entre candidatos compatíveis consultados,
sem garantia de encontrar todos os vídeos do YouTube. O vídeo anterior é
verificado novamente para evitar regressão causada por sua posição na busca.
Contagens antigas não escolhem o vencedor de uma busca nova. Aliases anteriores
sobrevivem quando a identidade não muda e o sidecar omite esses campos;
`aliases: []` ou `artist_aliases: []` remove explicitamente os aliases.

A pesquisa pública usa yt-dlp; `YOUTUBE_API_KEY`/`GOOGLE_YOUTUBE_API_KEY` pode
ser lida do ambiente do telefone ou de `~/.phone-worker.env`. Não coloque
chaves no sidecar, ZIP, relatório ou linha de comando.

## Configuração UTAU

O backend UTAU e o runtime ativo do Phone Worker 1.11.41 são preservados.
O importador carrega o ambiente real do worker e faz pedidos monofônicos
compatíveis ao serviço ativo autenticado. Não instala voicebanks, resamplers
ou kits antigos. As partes compartilham a mesma linha do tempo, são alinhadas
em WAV/PCM e misturadas antes de gerar o MP3.

## Artefatos e hashes

Cada pacote contém um incremento por música nos caminhos relativos validados:

- `cogs/tts/cd_song_scores.json`: melodia do painel, revisão e referências.
- `cogs/tts/cd_song_videos.json`: metadados, auditoria de busca e hash da capa.
- `cogs/tts/cd/arrangements/ID.json`: arranjo completo e proveniência MIDI.
- `cogs/tts/cd/packages/ID.json`: manifesto v2 e artefatos prontos/pendentes.
- `cogs/tts/cd/previews/index.json`: manifesto v3 da amostra, quando pronta.
- `cogs/tts/cd/thumbnails/VIDEOID.jpg`: JPEG validado, quando pronto.
- `cogs/tts/cd/previews/ID.mp3`: MP3 validado e decodificado, quando pronto.

O hash do MIDI, o hash da partitura do painel, o hash do arranjo completo,
o hash do trecho harmonizado e os hashes binários são distintos. A validação
confere os vínculos exatos e a origem musical da amostra, os caminhos, CRC,
JSONs, JPEG, a decodificação completa do MP3 e sua duração antes da publicação.
Uma composição aceita até 700 ms de diferença para pre-roll e padding MP3;
amostras monofônicas antigas têm tolerância de dois segundos para preservar
ataques e caudas históricos. Áudio decodificável de duração incompatível é
rejeitado mesmo quando todos os hashes binários estão corretos.

Uma mudança na harmonia invalida a amostra mesmo quando a melodia principal
continua igual. Alterações só de capa ou proveniência, sem alteração musical,
preservam áudio compatível. Amostras v2 só migram explicitamente quando o
novo arranjo tem uma parte e a mesma partitura; elas nunca representam um
novo arranjo de duas ou três partes. Bytes anteriores continuam disponíveis
para recuperação, mas não são publicados como áudio de outra partitura.

O catálogo local fica em `~/.local/share/osaka-ttv-cd/`, fora do kit. Preserve
essa pasta. A VPS mantém gerações isoladas do catálogo fora do repositório,
valida e troca a versão ativa atomicamente. A confirmação `activated` informa
que o bot carregou os dados; `received` ou `applied` é uma etapa anterior.

## Testes isolados e recuperação

`--candidates candidatos.json --cover-file capa.jpg` usa metadados e JPEG
locais sem pesquisa/download. `--no-sample` permite preparar demais artefatos
com amostra explicitamente pendente. Um pacote validado pode estar incompleto;
`complete` e `pending_artifacts` informam isso.

```sh
python deploy/ttv-cd/package_validation.py CAMINHO_ABSOLUTO.zip --require-complete --audio-probe
```

Falhas permanentes de MIDI/seleção aguardam mudança de entrada/configuração.
MIDIs, sidecars, arquivos de teste e configuração do worker participam da
identidade da tentativa. `--retry` ignora cooldown e permite nova tentativa
manual; `--force` reimporta arquivos já processados. A fila persistente recupera
falhas temporárias de publicação sem resintetizar artefatos prontos.
Erros permanentes da amostra aguardam mudança de entrada/configuração ou
`--retry`. A recuperação sem MIDI mantém cooldown e detecta correções na
autenticação do worker por uma identidade opaca, sem registrar credenciais.
Ativar a publicação depois de uma importação offline aproveita o último ZIP
validado; confirmações `activated` persistem entre reinícios do kit.

Um watch reserva o catálogo até terminar. Encerre com Ctrl+C antes de atualizar
arquivos Python ou iniciar uma importação manual. Watchers antigos precisam
ser encerrados antes da primeira instalação v9; use `pgrep -af import-midi.py`
e `kill -INT PID` somente para o processo do importador desejado.
