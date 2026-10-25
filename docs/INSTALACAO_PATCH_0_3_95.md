# Music Agent 0.3.95 — volume e efeitos na saída Opus

Patch incremental **sobre a 0.3.94**, somente com arquivos alterados ou novos
nas pastas originais `tts-bot-main/`. O Phone Worker continua **1.11.43**.

## O que foi implementado

O caminho automático agora pode entregar pacotes Opus ao VoiceClient também
quando a música usa volume diferente de 100%, bassboost, nightcore ou
slowed/reverb. O mixer aplica ganho, efeitos locais e mistura de TTS antes de
uma codificação de saída por frame de 20 ms. Não há uma segunda codificação
desse pacote pelo VoiceClient.

Quando uma faixa começa com volume/efeitos que exigem processamento, usa o
PCM decodificado da fonte original e o encoder do mixer. Não é necessário
preparar uma versão Opus intermediária da AAC antes de processar o volume.
O padrão de volume continua **55%**.

Há duas situações distintas:

- **Opus direto:** cópia dos pacotes da fonte Opus compatível, original ou
  preparada, sem alterar suas amostras no mixer.
- **Opus processado:** o mixer altera o som e gera novos pacotes Opus. O painel
  mostra esse nome, o bitrate aplicado e a fonte original, por exemplo
  `Opus processado · 160 kbps · fonte AAC 128 kbps`.

Alterar volume ou aplicar efeitos exige processamento interno de PCM. Não é
possível manter os mesmos pacotes comprimidos e também alterar o som que eles
representam. O novo caminho mantém o transporte Opus, sem apresentar a
recodificação como cópia original sem perdas.

Volume e bassboost são aplicados no mixer atual, com a suavização de ganho
existente. Nightcore e reverb aquecem uma nova fonte de música na posição da
faixa e só depois a entregam ao mesmo mixer; a conexão de voz, o encoder e o
TTS são preservados. Isso não promete transição sample-exact ou ausência de
transientes. Os limites do canal, qualidade e proteção continuam sendo usados.

O volume zero agora é um valor válido, inclusive no ciclo de duck/unduck do
TTS. A fala pode continuar audível enquanto a música está mutada. O teto
público de volume permanece 150%.

Depois que uma faixa passa a ser processada, permanece nesse caminho até
terminar. Retirar os efeitos e voltar a 100% não alterna repetidamente entre
encoders durante a mesma faixa; a próxima faixa compatível pode copiar.
Se existir um mixer legado com TTS ativo, ele é preservado até uma troca
segura, para não cortar a fala.

## 1. Instalar na VPS

Pare a música. Instale pelo updater habitual, mesclando `tts-bot-main/` em
`/home/ubuntu/bot`. Para instalação manual, envie o ZIP para
`~/patch-musica-0.3.95-incremental.zip` na **VPS**.

Se também baixou o `.sha256`, confira no diretório dos dois arquivos:

```bash
cd "$HOME"
sha256sum -c patch-musica-0.3.95-incremental.sha256
unzip -l patch-musica-0.3.95-incremental.zip
```

Faça backup dos arquivos de produção e aplique:

```bash
opus_effects_backup="$HOME/backup-musica-antes-0.3.95-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$opus_effects_backup" -C /home/ubuntu/bot \
  cogs/musica/agente_telefone/conversao.py \
  cogs/musica/agente_telefone/monitor.py \
  cogs/musica/interface/componentes.py \
  cogs/musica/nucleo/modelos.py \
  cogs/musica/reproducao/sincronizacao.py \
  cogs/musica/runtime_telefone/agente/configuracao.py \
  cogs/musica/runtime_telefone/agente/estado.py \
  cogs/musica/runtime_telefone/agente/mixer_pcm.py \
  cogs/musica/runtime_telefone/agente/reproducao.py \
  cogs/musica/runtime_telefone/agente/servidor.py \
  scripts/testar-saida-musica.py

opus_effects_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.95.XXXXXX)
unzip -q ~/patch-musica-0.3.95-incremental.zip -d "$opus_effects_patch_dir"
cp -a "$opus_effects_patch_dir/tts-bot-main/." /home/ubuntu/bot/
printf '%s\n' "$opus_effects_backup"
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

O ZIP não contém `.env`, tokens, chaves ou músicas. Atualize a VPS e o runtime
do telefone, porque a saída e o painel mudaram.

## 2. Atualizar o telefone

No **Termux**, com transporte conectado e música parada:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado: Music Agent **0.3.95**, disponível e conectado ao Discord. O bootstrap
continua anunciando Worker 1.11.43. Se o arquivo foi atualizado mas o processo
ainda anunciar a versão anterior, reinicie uma vez:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Baixe **testar-saida-musica-v7.py** em Downloads e copie:

```bash
cp "$HOME/storage/downloads/testar-saida-musica-v7.py" "$HOME/testar-saida-musica.py"
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode auto
python ~/testar-saida-musica.py status --guild 927002914449424404
```

## 3. Testar volume e efeitos

No Discord, anote o volume atual, use `_volume 55` e inicie uma música desde o
começo. Esse primeiro teste não exige volume 100. Durante a faixa consulte:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Esperado durante o áudio musical:

- `version: 0.3.95`, `requested.opus_direct: auto`.
- `passthrough.opus_transport: true`.
- `passthrough.processed_active: true` e `output_mode: opus_processed`.
- `processed_bitrate_kbps` informa o valor aplicado ao encoder.
- `applied.output_mode: opus_processed` e o painel mostra **Opus processado**.
- `copy_active: false` é esperado nesse teste. `volume_requires_pcm` explica
  por que a cópia pura não está ativa; não significa falha do transporte Opus.

Faça estes testes no Discord:

1. Alterne `_volume 0` e `_volume 55`. O alvo muda imediatamente; o mixer
   preserva a pequena rampa de ganho para evitar cliques. No zero, envie um TTS
   e confirme que a música permanece mutada depois da fala.
2. No painel, abra **Mais opções** e ative/desative **Bassboost**. Verifique
   a mudança no som, sem reinício da música ou perda da fila.
3. Ative **Nightcore** e depois **Slowed + Reverb**. Os dois modos são
   mutuamente exclusivos como antes. Verifique a troca de efeito, TTS e fila.
4. Teste pausa/retomada e pular. Pausa, reconexão e TTS isolado não comprovam
   processamento musical ativo, por isso podem remover o indicador momentaneamente.
5. Desligue os efeitos, use `_volume 100` e inicie uma próxima faixa Opus
   compatível. O painel pode voltar para **Opus direto**. Não é preciso forçar
   uma alternância de encoder no meio da faixa anterior.

O volume 150% também é suportado, com o limiter existente. Use o volume de
escuta adequado no aparelho. Ao terminar, restaure o volume anterior.

Opcionalmente, para comparar o perfil de saída processada de 256 kbps em um
canal que permita esse bitrate:

```bash
python ~/testar-saida-musica.py profile --guild 927002914449424404 --profile quality_256
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Isso não ultrapassa o teto do canal nem recupera detalhes ausentes na fonte.
Perfis de saída continuam em memória até reiniciar; a preferência `auto/off`
continua persistida por servidor.

## 4. Registrar a validação

No Termux, antes dos testes:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E '\[music-start\]|audio_source_selected|opus_encoder_updated|audio_effect|tts_duck|tts_restore' \
  | tee ~/music-start-tests/validacao-opus-0.3.95.log
```

Depois pressione Ctrl+C e salve o status durante uma música com volume 55 ou
efeito ativo:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/saida-opus-0.3.95.json
cp ~/music-start-tests/validacao-opus-0.3.95.log ~/storage/downloads/
cp ~/music-start-tests/saida-opus-0.3.95.json ~/storage/downloads/
```

## Validação e reversão

Os testes nativos verificam pacotes de 20 ms e comparam o PCM entregue ao
encoder com o mixer anterior para volumes 0/55/100/150 e bassboost 0–6.
Também verificam uma codificação por frame, preservação do encoder/player,
TTS, pausa, fontes substituídas, cancelamento dos efeitos, fila, mudo após TTS
e evidência atual até o painel. A troca da fonte de efeito renova a prova de
envio sem duplicar a medição de primeiro áudio da música.

Os testes locais adaptam as fronteiras Discord/rede e não comprovam qualidade
perceptiva, latência no telefone ou ausência de transientes. Conversão e
processamento continuam envolvendo codificação com perdas. Se a faixa já era
uma versão Opus preparada, o processamento pode adicionar uma geração de
codificação, como no fallback anterior.

Para voltar à 0.3.94, pare a música e restaure seu backup pelo caminho real:

```bash
tar -xzf "$HOME/backup-musica-antes-0.3.95-AAAAMMDDTHHMMSS.tar.gz" -C /home/ubuntu/bot
```

Publique novamente o runtime, reinicie `tts-bot.service`, execute o bootstrap
no telefone e inicie o Music Agent. Confirme 0.3.94. Músicas, posts, cache,
preferências persistidas e transporte SSH permanecem preservados.
