# Capas do painel `_cd`

O painel usa um vídeo fixo por música e não pesquisa o YouTube ao abrir ou trocar
uma seleção. Este kit baixa as capas uma vez no Termux, usando os mesmos módulos
de download, verificação e conversão FFmpeg do cache de música do projeto.

O bot já pode mostrar a URL pública da thumbnail. Para servir as capas pelo próprio
Discord mesmo com o worker offline, aplique no bot o ZIP de capas exportado abaixo.

```bash
unzip -o "$HOME/storage/downloads/ttv-cd-termux-kit-v3.zip" -d "$HOME/ttv-cd-kit"
python "$HOME/ttv-cd-kit/deploy/ttv-cd/cache-thumbnails.py" \
  --refresh-videos \
  --export-zip "$HOME/storage/downloads/ttv-cd-capas.zip" \
  --report "$HOME/storage/downloads/ttv-cd-capas.json"
```

Aplique `ttv-cd-capas.zip` pelo atualizador do bot. O ZIP exportado armazena
`cogs/tts/cd_song_videos.json` e as capas em `cogs/tts/cd/thumbnails/`,
fora das pastas `data/` ignoradas pelo Git. O cache permanente fica em
`$HOME/.cache/core-worker/ttv/cd/thumbnails/<video-id>.jpg`, fora das releases.
A síntese, a versão do worker e as preferências de voz não são alteradas.

O script precisa do Python com `aiohttp` e `discord.py` já usados pelo worker e de
`ffmpeg`/`ffprobe`. Se FFmpeg não estiver instalado: `pkg install ffmpeg`.
O kit contém cópias idênticas dos cinco módulos de imagem do domínio de música;
não depende de uma cópia completa do repositório no telefone.

`--refresh-videos` compara as visualizações dos resultados originais que
correspondem à música. Usa uma chave `YOUTUBE_API_KEY`/`GOOGLE_YOUTUBE_API_KEY` já
configurada no ambiente, ou o `yt-dlp` instalado. Não imprime chaves. Se o provedor
não retornar contagens, preserva o vídeo curado e registra isso no catálogo.
Somente a capa da seleção vencedora é baixada. Covers, remix, live e karaokê são
rejeitados. A comparação é limitada aos candidatos inspecionados, sem prometer que
a busca encontrou todo vídeo existente no YouTube.

Sem `--refresh-videos`, o script usa diretamente a seleção curada. As contagens do
catálogo são snapshots de páginas públicas e podem estar desatualizadas. Nenhum
JPEG de produção foi baixado no ambiente de desenvolvimento: o acesso a `ytimg`
foi bloqueado pelo proxy da sessão. O download real ocorre no Termux.

Arquivos inválidos ou grandes não substituem uma capa existente. Cada imagem tem
limite de 2 MiB, é verificada pelo FFmpeg/FFprobe e convertida para JPEG de até
768 px. Reexecutar com JPEGs válidos já prontos valida e preserva seus bytes. Caches com formato diferente de JPEG são baixados novamente, sem exportar um PNG disfarçado de `.jpg`. Publicação
usa `os.replace`, compatível com Python Android sem `os.link`.
