# Music Agent 0.3.94 — preparar AAC e outros formatos para Opus direto

Patch incremental **sobre a 0.3.93**, com somente os arquivos alterados ou novos
nas pastas originais `tts-bot-main/`. O Phone Worker permanece **1.11.43**.

## O que explica as imagens

A 0.3.93 estava instalada. A faixa “Notion” aparecia com fonte **AAC a 128 kbps**.
Essa versão copiava fontes Opus compatíveis; a preparação de AAC/MP3/Vorbis
ainda não estava implementada. O campo AAC descrevia a fonte: a saída normal
para o Discord já era codificada em Opus.

O contador de `_musicaarquivo status` mede o arquivamento, não quantas músicas
estão copiando Opus. Esse contador não comprova o modo da faixa atual.

## O que muda nesta versão

- Fontes Opus compatíveis continuam usando seus pacotes originais.
- AAC, MP3, Vorbis, fontes mono ou com outra taxa de amostragem podem ser
  preparadas em Opus estéreo 48 kHz, com pacotes de 20 ms. A preparação começa
  a produzir áudio antes do fim da entrada; não espera converter a música
  inteira antes de iniciar.
- Usa a maior capacidade de cópia permitida pelo canal e pelo teto configurado
  (256 kbps por padrão), com CBR limitado ao tamanho de pacote suportado. Um
  canal de 96 kbps continua limitando a saída. A preparação preserva o filtro
  de reamostragem de alta qualidade e os ajustes solicitados de FEC/perda.
- Arquivos antigos continuam nos mesmos posts e formatos. A preparação usa os
  originais quando a música é tocada. Para arquivos multipartes incompatíveis,
  o caminho PCM existente une as partes e um único encoder prepara o fluxo.
- A versão completa preparada pode ficar em cache no telefone. A gravação só
  é confirmada após validar o fim do fluxo e o sucesso do FFmpeg. Pular a faixa
  cedo pode deixar a versão incompleta, que é descartada. Falha de cache não
  deve impedir a reprodução.
- O painel só mostra **Opus direto** com confirmação da cópia na faixa atual.
  Uma fonte AAC preparada pode aparecer como
  **Opus direto · 256 kbps · fonte AAC 128 kbps**. O codec e o bitrate originais
  continuam preservados; o painel distingue origem e saída.

O modo `auto/off` continua salvo por servidor. **O volume padrão continua em
55%**. Para copiar os pacotes, use volume do bot em **100%**, sem efeitos ou
TTS misturado. Volume, efeitos, TTS, seek e outras condições de processamento
podem exigir PCM. O patch não aumenta o volume automaticamente.

Converter AAC/MP3/Vorbis para Opus envolve perdas e não recupera detalhes que
faltavam no original. O cache evita repetir a conversão enquanto a versão
válida estiver disponível. Se houver mistura/processamento durante uma faixa
preparada, pode ocorrer uma geração adicional de codificação no fallback.
FEC solicitado não significa proteção comprovada em todos os pacotes Opus.
Não há promessa de transparência perceptiva ou de zero falhas de rede.

## 1. Instalar na VPS

Pare a música. Instale pelo updater habitual, mesclando `tts-bot-main/` em
`/home/ubuntu/bot`. Para instalação manual, envie o ZIP para
`~/patch-musica-0.3.94-incremental.zip` na **VPS**.

Se baixou também o arquivo `.sha256`, confira no diretório dos dois arquivos:

```bash
cd "$HOME"
sha256sum -c patch-musica-0.3.94-incremental.sha256
unzip -l patch-musica-0.3.94-incremental.zip
```

Faça backup dos arquivos de produção e aplique:

```bash
opus_prepare_backup="$HOME/backup-musica-antes-0.3.94-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$opus_prepare_backup" -C /home/ubuntu/bot \
  cogs/musica/agente_telefone/conversao.py \
  cogs/musica/agente_telefone/monitor.py \
  cogs/musica/interface/componentes.py \
  cogs/musica/nucleo/modelos.py \
  cogs/musica/reproducao/sincronizacao.py \
  cogs/musica/runtime_telefone/agente/buffer_pcm.py \
  cogs/musica/runtime_telefone/agente/configuracao.py \
  cogs/musica/runtime_telefone/agente/estado.py \
  cogs/musica/runtime_telefone/agente/mixer_pcm.py \
  cogs/musica/runtime_telefone/agente/preparacao_audio.py \
  cogs/musica/runtime_telefone/agente/servidor.py \
  scripts/testar-saida-musica.py

opus_prepare_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.94.XXXXXX)
unzip -q ~/patch-musica-0.3.94-incremental.zip -d "$opus_prepare_patch_dir"
cp -a "$opus_prepare_patch_dir/tts-bot-main/." /home/ubuntu/bot/
printf '%s\n' "$opus_prepare_backup"
```

Guarde o caminho do backup. Se a instalação foi manual, ou se o updater não
publicou o runtime, execute:

```bash
cd /home/ubuntu/bot
/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python - <<'PY'
from dotenv import load_dotenv
import runpy
import sys

load_dotenv('/home/ubuntu/bot/.env', override=False)
sys.argv = ['scripts/core-worker-automation.py', 'after-update', '--force-agent']
runpy.run_path('scripts/core-worker-automation.py', run_name='__main__')
PY

sudo systemctl restart tts-bot.service
sudo systemctl is-active tts-bot.service
```

O ZIP não inclui `.env`, tokens, chaves ou dados de música. VPS e telefone
precisam ser atualizados porque a preparação e o painel mudaram.

## 2. Atualizar o telefone e o auxiliar

No **Termux**, com o transporte conectado e a música parada:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado: Music Agent **0.3.94**, `available: true`, `discord_ready: true`, sem
dependências de voz ausentes. O bootstrap continua anunciando Worker 1.11.43.
Se o arquivo foi atualizado mas o processo ainda anunciar a versão anterior:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Baixe **testar-saida-musica-v6.py** em Downloads e copie:

```bash
cp "$HOME/storage/downloads/testar-saida-musica-v6.py" "$HOME/testar-saida-musica.py"
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode auto
python ~/testar-saida-musica.py status --guild 927002914449424404
```

## 3. Repetir o teste da faixa AAC

Anote o volume atual. No Discord, use `_volume 100`, desligue efeitos e inicie
“Notion” novamente desde o começo. Ajuste o volume de escuta do aparelho
conforme necessário. A atualização não altera retroativamente a fonte da
faixa que já estava tocando antes dela.

Durante a música, no Termux:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Confira:

- `version: 0.3.94` e `requested.opus_direct: auto`.
- `preparation.candidate: true` quando os requisitos permitem preparação.
  Se estiver falso, leia `preparation.reasons` e confirme volume/efeitos.
- `passthrough.copy_active: true`, `copied_packets` crescendo e
  `applied.output_mode: opus_direct` quando há cópia real.
- Em `passthrough.source`: `prepared_opus: true`, codec original em
  `prepared_source_codec` e alvo em `prepared_bitrate_kbps`.
- O painel mostra **Opus direto**, indicando também a fonte AAC original.
  Se o canal permitir somente 96, espere 96 no alvo preparado, não 256.

O perfil define o encoder de fallback. O orçamento do canal/configuração
define o limite da preparação para cópia. Subir um perfil não ultrapassa o
teto do canal nem melhora a informação contida no AAC original.

Deixe uma faixa completa tocar para concluir o cache. Depois toque-a novamente
com as mesmas configurações e consulte o status: `prepared_cache_hit: true`
confirma reutilização. Não basta selecionar `auto` ou observar o contador de
arquivamento para comprovar cópia/cache.

Teste também uma Opus compatível, uma arquivada AAC e, se disponível, uma
arquivada multipartes. Em seguida verifique pausa/retomada, TTS, volume e pular.
Esses controles podem levar a faixa ao processamento PCM até terminar; a
próxima faixa compatível pode voltar a copiar. Restaure o volume anterior ao
concluir (`_volume 55` se era o padrão).

## Cache e espaço

O cache fica por padrão em `~/phone-worker/state/prepared-opus-cache`, fora das
releases. Usa até **256 MiB**, com limite de **64 MiB por item**, preservando
uma reserva de espaço livre. Remove entradas antigas para respeitar o orçamento.
Arquivos temporários incompletos não são versões prontas. O cache não regrava
posts, anexos nem manifestos e não é uma conversão permanente de todo o acervo.

As identidades usam referências/hashes do arquivo ou vídeos YouTube reconhecidos,
além dos parâmetros de codificação, proteção e reamostragem. URLs mutáveis sem
identidade segura continuam sendo preparadas, mas sem cache persistente.
Mudanças de canal/configuração podem exigir outra versão. Entradas removidas
do cache são preparadas novamente quando necessário.

Configurações opcionais: `MUSIC_AGENT_PREPARED_OPUS_CACHE_DIR` escolhe o diretório;
`MUSIC_AGENT_PREPARED_OPUS_CACHE_MB=0` desliga somente o cache, mantendo a
preparação em streaming. Não é necessário editar o ambiente para usar o padrão.

## 4. Registrar teste e velocidade

No Termux, inicie a captura antes de tocar:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E '\[music-start\]|opus_preparation|opus_direct_prepare_fallback|audio_source_selected|opus_encoder_updated|archive_opus_pcm_fallback' \
  | tee ~/music-start-tests/validacao-opus-0.3.94.log
```

Compare a primeira reprodução da AAC com uma reprodução após concluir o cache,
usando a mesma faixa, rede e canal. Observe os tempos até o primeiro pacote e
anote cortes, distorção e falhas dos controles. A preparação não espera EOF
para começar, mas a latência real precisa ser medida no telefone.

Depois pressione Ctrl+C e salve também o status durante a reprodução:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/saida-opus-0.3.94.json
cp ~/music-start-tests/validacao-opus-0.3.94.log ~/storage/downloads/
cp ~/music-start-tests/saida-opus-0.3.94.json ~/storage/downloads/
```

Se houver fallback de preparação, confira se o FFmpeg oferece o encoder:

```bash
ffmpeg -hide_banner -encoders 2>/dev/null | grep -F libopus
```

## Validação e reversão

Os testes locais incluem AAC/MP3/Vorbis com FFmpeg/libopus real, pacotes CBR de
20 ms limitados ao canal, áudio antes de EOF, cache completo e incompleto,
reutilização sem FFmpeg, originais preservados, mudança de capacidade, controles,
proveniência até o painel, cancelamento e parâmetros de qualidade. As fronteiras
Discord/rede são adaptadas; a escuta e a latência no telefone ainda precisam
ser validadas após instalar. As limitações anteriores de trim em pacotes crus
e de transientes na mudança de encoder continuam válidas.

Para voltar à 0.3.93, pare a música e restaure o backup com seu caminho real:

```bash
tar -xzf "$HOME/backup-musica-antes-0.3.94-AAAAMMDDTHHMMSS.tar.gz" -C /home/ubuntu/bot
```

Publique novamente o runtime, reinicie `tts-bot.service`, atualize o telefone
com o bootstrap e inicie o agente. Confirme 0.3.93. O cache pode permanecer no
telefone, mas a versão anterior não o usa. Os originais, preferências de modo
e transporte SSH continuam preservados.
