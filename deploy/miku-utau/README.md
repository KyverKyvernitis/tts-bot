# Miku English CVVC no Termux

Este kit instala o port **MIKU_V4_English / Mocaloid Generator** divulgado por
[DEMO](https://demosvocas.blogspot.com/2025/12/mikuv4xbeta-utau.html).
O [ZIP de terceiros](https://drive.google.com/file/d/1VoNNANLMEW-TEcqLPWa1hxgATWZPMfwP/view)
é `mukki english cvvc.zip`, com 71.819.802 bytes e SHA-256
`e1fc9a37a7e45e0ad4209b2b0f37d90541b2528c85d1b58a81c0b399ecaf79be`.
Não é um banco UTAU oficial da Crypton; a autorização e a licença de distribuição
não foram confirmadas. O kit não inclui os áudios nem baixa ou executa programas
do arquivo de voz.

Reutiliza Python, FFmpeg e o ESPER já instalado no Ubuntu ARM64
`voicepeak-arm64`. Não precisa de VOICEPEAK, Box64 ou reinstalação do Ubuntu.
A Miku fica em `~/voicebanks/hatsune-miku-english`, separada das duas banks da Teto.
Os aliases X-SAMPA originais, o sufixo `_D4`, o `prefix.map` e os avisos do ZIP
são preservados.

## Instalar e gerar os dois MP3s

Depois de extrair o kit em `~/miku-utau-termux-kit`, rode:

```bash
python "$HOME/miku-utau-termux-kit/deploy/miku-utau/termux/install-voicebank.py" \
  --archive "$HOME/storage/downloads/mukki english cvvc.zip" \
  --container voicepeak-arm64 --esper-root "$HOME/.esper-utau"

python "$HOME/miku-utau-termux-kit/deploy/miku-utau/termux/validate-miku.py" \
  --container voicepeak-arm64 --timeout 120
```

Se o Android salvou o ZIP com outro nome, altere somente o valor de `--archive`.
O instalador confere o SHA-256 antes da publicação. Um arquivo diferente gera
erro e não substitui uma bank existente. A segunda execução confirma o inventário
da bank instalada e reaplica somente as configurações da Miku, quando necessário.

As amostras ficam em **Downloads/miku-comparacao**:

- `01-miku-portugues.mp3`: “Olá. Eu sou a Miku. Como você está?”
- `02-miku-english.mp3`: “Hello. I am Miku. How are you?”
- Os WAVs correspondentes e `miku-comparacao.json` com o resultado dos testes.

O teste usa tom normal (offset de 0 semitons) e velocidade 1,0. Confere PCM
audível, identidade da voz, idioma, tom, velocidade e decodificação do MP3.
Ouça os arquivos para avaliar timbre e pronúncia; gerar um arquivo válido não
comprova naturalidade ou inteligibilidade. Português adaptado aproxima os sons
brasileiros usando o banco inglês.

## Habilitar no bot

O instalador registra somente `PHONE_WORKER_MIKU_*` em `~/.phone-worker.env`,
com backup antes de uma alteração, e preserva os ajustes e comentários da Teto.
O comando ESPER é reutilizado: são aceitos `resampler.py` e
`esper-utau-resampler`, tanto diretamente quanto depois de `python`. Com
`--esper-root`, cria o comando da Miku diretamente da ponte instalada nesse root,
sem depender dos comandos antigos salvos para a Teto ou a Miku. A preferência
do usuário e o comando da Teto são preservados.

Desde o kit v4, o instalador lê as opções declaradas pelo parser da ponte, sem executá-la. Usa
`--implementation-id` somente quando disponível. Nas versões antigas, remove
esse argumento e usa `--cache-dir` com um cache próprio da Miku identificado
pelo SHA-256 da ponte. Isso mantém a invalidação dos caches quando o código
muda e preserva a instalação do ESPER. Se a bank já foi instalada pelo v3,
o instalador apenas confere seu inventário e corrige as configurações da Miku.

Sem comando ou root explícito, a ponte instalada
`~/.esper-utau/bin/resampler.py` é usada. Nenhum `.env` ou código do ZIP é executado
durante a instalação. As banks existentes em uma pasta sem marcador válido são
preservadas.

A atualização do bot/worker precisa incluir o suporte à Miku. Após concluir a
instalação e as amostras, reinicie o worker pelo fluxo normal que você já usa.
No Discord, abra **TTV → Configurar → Escolha sua vocaloid → Hatsune Miku** e
escolha **Português adaptado** ou **Inglês**. A Teto continua selecionada até essa
troca; instalar o banco não muda as preferências dos usuários.

Os parâmetros `--env`, `--directory`, `--esper-root` do instalador e `--voicebank`,
`--output-dir` do validador permitem usar outros caminhos. `--timeout` admite
1 a 120 segundos por amostra. O relatório não imprime tokens do worker.

## Otimização e medição no celular

O worker **1.11.38** e o kit v5 usam até quatro grupos de fragmentos da Miku
em paralelo. Notas do mesmo WAV continuam em sequência para preservar o cache
de análise do ESPER. Tom, velocidade, montagem e fingerprints de áudio são
preservados. O limite da Teto continua em duas tarefas.

O validador agora usa o mesmo cache de fragmentos do bot:
`~/phone-worker/cache/miku-fragments`. Isso permite reaproveitar no bot as
amostras com os mesmos parâmetros. As variáveis existentes
`PHONE_WORKER_MIKU_RENDER_THREADS` (1 a 4) e
`PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR` são respeitadas. O kit não reinstala a
bank nem modifica a ponte do ESPER já instalada.

Após aplicar a atualização do bot pelo updater, confira **1.11.38** no status.
Extraia o kit v5 no mesmo diretório e execute o validador normalmente para
gerar os dois MP3s e preencher o cache compartilhado.

Para medir duas versus quatro tarefas no aparelho:

```bash
python "$HOME/miku-utau-termux-kit/deploy/miku-utau/termux/benchmark-miku.py" \
  --container voicepeak-arm64 \
  --report "$HOME/storage/downloads/miku-desempenho.json"
```

O teste aquece a análise em um cache temporário, depois alterna duas rodadas
com 2 e 4 tarefas. Mede fragmentos novos e a repetição com fragmentos prontos,
confere WAVs idênticos e remove os temporários. Não altera o `.env`, a bank ou
os caches do bot. `--language en-US` mede a frase em inglês; `--rounds 1`
reduz a quantidade de testes.

Compare `worker_synth_ms_median` na fase `fragmentos_novos` do relatório.
Os campos `resampler_calls`, `fragment_cache_hits`, `fragment_cache_misses` e
`render_workers` mostram o trabalho executado. Quatro tarefas não garantem
metade do tempo: núcleos disponíveis, temperatura e outros aplicativos afetam
a medição. O benchmark não mede o cache de frases inteiras do bot.
