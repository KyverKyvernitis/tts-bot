# Importação de CDs — Termux Kit v8

O kit do telefone e o patch da VPS são entregas separadas. O importador lê o
Phone Worker 1.11.41 instalado, carrega `~/.phone-worker.env` e usa seu backend
UTAU. Não instala voicebanks, resamplers ou versões antigas do worker e não
escreve no runtime ativo. Não envie MIDI pelo Discord.

1. Descompacte o kit em `~/ttv-cd-kit`, preservando `deploy/ttv-cd`.
2. Dependências: Python, FFmpeg/FFprobe, yt-dlp, aiohttp e Pillow. Use as
   instalações existentes; nenhuma atualização do worker é necessária.
3. Coloque arquivos `.mid`/`.midi` em `~/storage/downloads/osaka-cd-midi/`.
4. Execute `python ~/ttv-cd-kit/deploy/ttv-cd/import-midi.py`.
5. Confira o caminho absoluto, tamanho, SHA-256 e `validation` no relatório
   `~/storage/downloads/osaka-cd-import-relatorio.json`. `ok: true` indica que
   o pacote passou na validação; `complete` e `pending_artifacts` dizem quais
   artefatos estão prontos. Um pacote validado pode conter amostra pendente.
6. Envie os ZIPs `osaka-cd-import-*.zip` ao updater da VPS ou configure a
   publicação autenticada opcional descrita abaixo. A publicação e a aplicação
   na VPS são etapas distintas.

## Identificação e seleção musical

Use nomes como `wowaka - Rolling Girl.mid` ou um JSON ao lado do MIDI com o
mesmo nome base. Exemplo:

```json
{
  "id": "rolling-girl",
  "title": "Rolling Girl",
  "artist": "wowaka",
  "aliases": ["Rollin Girl", "ローリンガール"],
  "artist_aliases": ["現実逃避P"],
  "seed_video_ids": ["vnw8zURAxkU"]
}
```

Os campos opcionais `track_index` e `channel` são índices a partir de zero;
o canal 9 é percussão. `start_ms` escolhe um deslocamento no MIDI original;
`max_duration_ms` (até 60000) e `max_notes` (até 128) delimitam o trecho
compatível com o worker. MIDI de até 8 MiB é aceito. A extração respeita o
tempo global, pausas e duração das notas. Em arranjos polifônicos, seleciona
uma faixa/canal e reduz a voz superior, registrando notas descartadas,
oitavas, pausas e a condição `vocal_line_ambiguous`. Isso não confirma uma
transcrição vocal. O MIDI original permanece intacto.

`search_query` complementa a busca do player. `allow_versions` autoriza
explicitamente variantes como `cover`, `live`, `remix` ou `instrumental`;
sem esse campo, versões diferentes são rejeitadas. Informe artista e aliases
adequados para uma identificação verificável. Se o MIDI representa a música
inteira, `source_is_full_song: true` permite comparar sua duração com a do
vídeo. Para trechos/adaptações, omita esse campo; opcionalmente declare a
duração total conhecida em `expected_song_duration_seconds`.

A escolha registra consultas, candidatos aceitos/rejeitados, motivos e
contagens verificadas. É o vídeo mais visto entre os candidatos compatíveis
inspecionados, sem afirmar uma busca exaustiva do YouTube. O campo
`YOUTUBE_API_KEY`/`GOOGLE_YOUTUBE_API_KEY` pode ser configurado no ambiente do
importador ou no arquivo confiável `~/.phone-worker.env`; alternativamente a pesquisa pública usa yt-dlp. Não coloque
chaves no sidecar, ZIP, relatório ou linha de comando.

## Recuperação de falhas

Use `--watch` para monitorar a pasta e repetir artefatos pendentes. Use
`--retry` para tentar imediatamente após uma correção ou liberar um
cooldown temporário; `--force` inclui arquivos já processados. Falhas
permanentes de MIDI/seleção aguardam mudança de entrada/configuração, sem
cooldown de uma hora. Arquivo MIDI, sidecar, candidatos/capa de teste,
configuração e release do worker participam da identidade de tentativa.

Amostras prontas cuja partitura canônica não mudou são reutilizadas,
inclusive após alteração somente da capa ou falha de rede. Uma nova
partitura invalida seu manifesto de amostra; os bytes anteriores ficam no
telefone para recuperação, mas não são publicados como áudio da nova
partitura. Bateria/temperatura e configuração indisponível produzem amostra
pendente. O próximo sucesso exporta novamente um CD completo, nunca um
ZIP contendo apenas MP3 e índice. O `--no-sample` permite preparar demais
artefatos, mas deixa explícito que o CD ainda não está completo.

## Pacotes e publicação

Cada ZIP contém somente um CD nos caminhos do updater:

- `cogs/tts/cd_song_scores.json`: incremento por ID, com hash MIDI separado
  do hash da partitura canônica.
- `cogs/tts/cd_song_videos.json`: metadados, auditoria de busca e hash da capa.
- `cogs/tts/cd/previews/index.json`: manifesto v2 da amostra, quando pronta.
- `cogs/tts/cd/packages/ID.json`: revisão, hashes e artefatos prontos/pendentes.
- `cogs/tts/cd/thumbnails/VIDEOID.jpg`: capa, quando pronta.
- `cogs/tts/cd/previews/ID.mp3`: áudio, quando pronto e decodificado.

O updater mescla por ID e preserva os outros CDs. O importador verifica CRC,
caminhos, JSONs, hashes, JPEG e, para áudio presente, FFprobe e decodificação
com FFmpeg antes de confirmar o catálogo local e o caminho final do ZIP.
O catálogo persistente fica em `~/.local/share/osaka-ttv-cd/`; preserve essa
pasta ao trocar o kit.

Para transferir automaticamente, o patch VPS precisa estar instalado e a
rota HTTPS existente precisa estar habilitada com
`OSAKA_CD_PUBLISH_ENABLED=1` e um `OSAKA_CD_PUBLISH_TOKEN` de no mínimo 32
caracteres. Configure o mesmo token somente no ambiente do telefone e use:

```sh
python ~/ttv-cd-kit/deploy/ttv-cd/import-midi.py --publish-url https://SEU_HOST/api/osaka/cd/publish --watch
```

A resposta `state: queued` comprova recebimento, não instalação.
`installed_on_vps: false` permanece até confirmação independente da
aplicação pelo updater. Os ZIPs locais continuam disponíveis para o envio
manual e rollback. Ao alterar a capa, a VPS recalcula cor/emoji pelo hash;
uma amostra compatível permanece válida. Ao aplicar a atualização, confira
`_cd`, a opção da música, a borda/capa/emoji e o botão de amostra.

Para testes isolados, `--candidates candidatos.json --cover-file capa.jpg`
usa metadados e JPEG locais sem pesquisa/download. Valide um pacote existente:

```sh
python ~/ttv-cd-kit/deploy/ttv-cd/package_validation.py CAMINHO_ABSOLUTO.zip --require-complete --audio-probe
```

O ZIP de amostra v7 usa manifesto legado e precisa de migração explícita
com sua partitura correspondente; não substitua o índice v2 por esse ZIP.
