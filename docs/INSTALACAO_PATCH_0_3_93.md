# Music Agent 0.3.93 — cópia Opus e arquivos multipartes

Patch incremental **sobre a 0.3.92**. Contém somente arquivos alterados ou novos
dentro de `tts-bot-main/`, mantendo as pastas originais. O Phone Worker continua
**1.11.43**. Se ainda usa uma versão anterior, aplique primeiro os patches
anteriores, na ordem, até a 0.3.92.

## O que foi implementado

1. **Orçamento separado para cópia.** `requested.bitrate_kbps` continua sendo o
   alvo do encoder usado no processamento PCM. `requested.copy_bitrate_kbps`
   descreve a capacidade disponível para copiar pacotes originais: considera
   o canal observado e o teto configurado. Uma fonte com picos acima do alvo
   do encoder pode copiar se esses pacotes couberem no orçamento real. Os
   limites de tamanho/duração dos pacotes continuam sendo verificados.
2. **Leitura Opus contínua de arquivos divididos em partes.** Valida o
   manifesto, cabeçalhos, CRC, duração, ordem e os pacotes repetidos de
   preparação das partes seguintes. Esses pacotes precisam corresponder aos
   anteriores antes de serem descartados. Um único decoder acompanha a faixa;
   as emendas não reiniciam o decoder nem reenviam o preroll duplicado.
3. **Antecipação limitada da próxima parte.** A leitura dos anexos ocorre fora
   da thread de voz, com backpressure e somente a parte atual e uma próxima
   antecipada. Usa a cota compartilhada existente. URLs são resolvidas pelas
   verificações atuais do arquivo; tamanho e SHA-256 são conferidos durante a
   leitura. URLs assinadas vencidas podem ser renovadas.
4. **Recuperação para PCM na posição consumida.** Se o caminho direto falhar
   depois do início, os frames válidos já preparados são consumidos e o caminho
   PCM é preparado a partir da posição reproduzida, considerando o pre-skip
   inicial. Falhas de rede ainda podem introduzir uma espera. O patch não
   promete ausência de gaps quando a rede não entrega o áudio.

Os anexos originais, manifestos e posts não são regravados. Arquivos Opus
compatíveis já existentes podem usar o novo caminho sem novo arquivamento.
Este patch implementa a melhoria da cópia e o leitor multipartes; a conversão
em lote de MP3/AAC/Vorbis e um cache persistente de versões preparadas ficam
para uma etapa posterior. Um `.ogg` pode conter Vorbis, portanto a extensão
sozinha não comprova compatibilidade.

O modo automático persistente da 0.3.92 continua ativo. O volume padrão de
55%, efeitos, TTS, fila e a escolha da fonte são preservados. Para copiar,
o volume do bot precisa ser 100%, sem efeitos/TTS misturado, com fonte e
pacotes compatíveis que caibam no canal. Seek continua usando PCM. Pacotes
de duração incompatível, como 40 ms, continuam pelo processamento normal.

## 1. Instalar na VPS

Pare a música. Instale pelo updater habitual, mesclando o conteúdo de
`tts-bot-main/` em `/home/ubuntu/bot`, ou faça a instalação manual abaixo.
Envie o ZIP para `~/patch-musica-0.3.93-incremental.zip` na **VPS**.

Se também baixou o `.sha256`, confira os dois arquivos no mesmo diretório:

```bash
cd "$HOME"
sha256sum -c patch-musica-0.3.93-incremental.sha256
unzip -l patch-musica-0.3.93-incremental.zip
```

Faça backup dos arquivos de produção e aplique:

```bash
opus_parts_backup="$HOME/backup-musica-antes-0.3.93-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$opus_parts_backup" -C /home/ubuntu/bot \
  cogs/musica/runtime_telefone/agente/audio_segmentado.py \
  cogs/musica/runtime_telefone/agente/buffer_pcm.py \
  cogs/musica/runtime_telefone/agente/configuracao.py \
  cogs/musica/runtime_telefone/agente/estado.py \
  cogs/musica/runtime_telefone/agente/mixer_pcm.py \
  cogs/musica/runtime_telefone/agente/preparacao_audio.py \
  cogs/musica/runtime_telefone/agente/reproducao.py \
  cogs/musica/runtime_telefone/agente/servidor.py \
  scripts/testar-saida-musica.py

opus_parts_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.93.XXXXXX)
unzip -q ~/patch-musica-0.3.93-incremental.zip -d "$opus_parts_patch_dir"
cp -a "$opus_parts_patch_dir/tts-bot-main/." /home/ubuntu/bot/
printf '%s\n' "$opus_parts_backup"
```

Guarde o caminho do backup. O ZIP não inclui `.env`, tokens, chaves ou estado
das preferências. Se a instalação foi manual, ou se o updater ainda não
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

## 2. Atualizar o telefone

No **Termux**, com o transporte conectado e a música parada:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado: Music Agent **0.3.93**, disponível, conectado ao Discord e com
dependências de voz prontas. O bootstrap continua anunciando Phone Worker
1.11.43; o status do Music Agent é quem deve mostrar 0.3.93.
Se o arquivo foi atualizado mas o processo ainda anunciar a versão anterior:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Baixe **testar-saida-musica-v5.py** em Downloads e copie o auxiliar:

```bash
cp "$HOME/storage/downloads/testar-saida-musica-v5.py" "$HOME/testar-saida-musica.py"
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode auto
python ~/testar-saida-musica.py status --guild 927002914449424404
```

A preferência `auto/off` continua salva por servidor. O novo auxiliar permite
ver os campos de capacidade de cópia e progresso da leitura multipartes.

## 3. Testar música online e arquivo de uma parte

Anote o volume atual do bot. Para exercitar a cópia, no Discord use
`_volume 100`, desligue efeitos e inicie uma faixa Opus desde o começo. Ajuste
o volume de escuta do aparelho conforme necessário; o patch não eleva o volume
automaticamente. Teste primeiro uma faixa online e depois uma arquivada com
apenas um anexo de áudio.

Durante cada faixa, no Termux:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Confira:

- `version: 0.3.93` e `requested.opus_direct: auto`.
- `requested.copy_bitrate_kbps`: pode ser maior que `requested.bitrate_kbps`.
  Por exemplo, encoder 163 e orçamento de cópia 256 são válidos.
- `passthrough.copy_active: true`, `copied_packets` crescendo e
  `applied.output_mode: opus_direct` quando a faixa realmente copia.
- Painel mostrando **Opus direto** no ciclo normal de atualização.

O perfil de qualidade define o encoder de fallback. A cópia mantém o bitrate
e os pacotes da fonte; não aumenta artificialmente a qualidade deles. Um canal
de 96 kbps continua limitando a cópia, mesmo com perfil 256. Se não copiar,
leia `reason` e `reasons` antes de alterar qualquer configuração.

## 4. Testar um arquivo dividido em partes

Escolha um post do arquivo com pelo menos dois anexos de áudio. Ignore capa e
manifesto nessa contagem. O áudio precisa ser Opus compatível; arquivos antigos
sem continuidade comprovável continuam em PCM.

Inicie a música **desde o começo** e deixe passar naturalmente por uma emenda.
Não use seek para chegar à emenda: seek exige PCM e não exercita esse caminho.
Consulte o status antes e depois:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Dentro de `audio_output.passthrough.source`, procure:

- `archive_reader_continuous: true`.
- `archive_segments_total` maior que 1.
- `archive_segments_completed` aumentando ao ler as partes.
- `archive_preroll_packets_skipped` e `archive_preroll_samples_skipped`
  aumentando depois da primeira emenda.

Esses campos descrevem o leitor, que pode estar um pequeno buffer à frente do
áudio ouvido. A confirmação de cópia continua sendo `copy_active: true`, junto
dos contadores de pacotes. Na escuta, verifique ausência de repetição, salto,
clique ou pausa na emenda. Deixe passar por mais de uma emenda, se possível.

Depois teste um TTS, pausa/retomada, mudança de volume e pular a faixa. O caminho
PCM deve preservar esses comandos; a faixa pode ficar em PCM até terminar,
como nas versões anteriores. Voltar a 100% não força cópia no meio dela.
Uma próxima faixa compatível pode copiar novamente.

Ao terminar, restaure o volume anterior (`_volume 55` se era o padrão).

## 5. Coletar logs e comparar o primeiro áudio

No Termux, inicie a captura antes dos testes:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E '\[music-start\]|audio_source_selected|opus_encoder_updated|opus_direct_prepare_fallback|archive_opus_pcm_fallback|channel_bitrate_hint_observed' \
  | tee ~/music-start-tests/validacao-opus-0.3.93.log
```

Use as mesmas faixas e rede dos testes anteriores: primeira faixa depois de
reiniciar, faixa seguinte sem reiniciar, URL, arquivo de uma parte e arquivo
multipartes. Compare `command_to_dispatch_ms` e `agent_to_first_packet_ms`
separadamente; não some etapas que ocorreram em paralelo. O patch não aguarda
baixar a música inteira para iniciar, mas a latência real depende da rede,
resolução da fonte e conexão de voz.

Pressione Ctrl+C depois da captura. Salve também o status durante a reprodução:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/saida-opus-0.3.93.json
cp ~/music-start-tests/validacao-opus-0.3.93.log ~/storage/downloads/
cp ~/music-start-tests/saida-opus-0.3.93.json ~/storage/downloads/
```

`archive_opus_pcm_fallback` indica a posição usada para recuperar pelo caminho
PCM. Esse evento informa uma recuperação; não comprova sozinho que a parte
estava corrompida. Se a rede/Discord também falhar no caminho PCM, a recuperação
da sessão existente continua sendo necessária. Não force uma queda de internet
no primeiro teste de continuidade; valide isso separadamente depois.

## Validação e reversão

Os testes nativos geram áudio com FFmpeg/libopus, segmentam com o código de
produção e verificam os pacotes originais e o PCM contínuo. Também verificam
fallback de pacotes de 40 ms, rejeição de preroll diferente com CRC válido,
mudanças de capacidade do canal, ciclo de vida dos streams, cancelamento e
posição de recuperação. As fronteiras Discord/HTTP são adaptadas nos testes
locais; a validação de rede e escuta no telefone acontece após a instalação.

A limitação anterior de trim inicial/final em pacotes crus permanece: o caminho
direto não promete corte sample-exact. A continuidade entre as partes é
validada sem reenviar os pacotes de preparação duplicados. Os limites do canal,
do Opus e dos controles de processamento continuam ativos.

Para voltar à 0.3.92, pare a música e restaure o backup usando seu caminho real:

```bash
tar -xzf "$HOME/backup-musica-antes-0.3.93-AAAAMMDDTHHMMSS.tar.gz" -C /home/ubuntu/bot
```

Publique novamente o runtime com o comando da etapa 1, reinicie
`tts-bot.service`, execute o bootstrap no telefone e inicie o Music Agent.
Confirme a versão 0.3.92. Os arquivos de música e as preferências persistidas
permanecem preservados, assim como o transporte SSH do Phone Worker 1.11.43.
