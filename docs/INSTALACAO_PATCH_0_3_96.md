# Music Agent 0.3.96 — reduzir recodificações adicionais

Patch incremental **sobre a 0.3.95**, com os arquivos alterados e novos nas
pastas originais `tts-bot-main/`. O Phone Worker continua **1.11.43**.

## O que mudou

Quando uma música AAC, MP3 ou outra fonte foi preparada em Opus para tocar
por cópia, alterar volume, ativar bassboost ou iniciar TTS exige processar o
som. Agora o agente tenta abrir novamente **a fonte original**, aquecer PCM
na posição atual e entregá-lo ao mesmo mixer. Assim, depois da troca, evita
manter a cadeia `original → Opus preparado → PCM → Opus processado`.

A fonte atual continua tocando enquanto a original aquece. A preparação tem
um limite de quatro segundos; se falhar ou a música mudar, o agente abandona
a tentativa e preserva a reprodução existente. Há um curto intervalo em que
o processamento ainda pode usar o Opus preparado. A troca mantém o encoder,
a conexão de voz, TTS, volume, efeitos, fila e relógio da música. Ela ocorre
entre leituras de áudio, sem reiniciar a faixa. Não é uma garantia de
transição sample-exact ou ausência de transientes.

Músicas Opus originais não precisam dessa troca. Nightcore e Slowed + Reverb
já aqueciam PCM da fonte original. Uma música que começa com volume diferente
de 100% ou com efeitos continua usando o original desde o início.

O arquivamento também passa a usar a mesma seleção de áudio da reprodução:
favorece Opus nativo entre opções comparáveis, preservando idioma, preferências
do extrator e configurações explícitas. Não força uma opção inferior só para
obter Opus. Isso vale para **novos downloads**; arquivos já preparados para
envio, músicas arquivadas, anexos e manifestos não são reescritos.

Volume e efeitos ainda exigem processamento e nova codificação de saída.
O painel continua mostrando **Opus direto** apenas para cópia e **Opus
processado** para o som alterado. O teto do canal é mantido: um canal de
96 kbps não passa a entregar 256 kbps com este patch. Não há conversão sem
perdas de AAC/MP3 para Opus nem promessa de eliminar falhas de rede.

## 1. Instalar na VPS

Pare a música. Use o updater habitual ou envie o ZIP e seu `.sha256` para
o diretório pessoal da **VPS**. A instalação manual abaixo pressupõe o bot
em `/home/ubuntu/bot` e a versão 0.3.95 já instalada.

Confira o pacote:

```bash
cd "$HOME"
sha256sum -c patch-musica-0.3.96-incremental.sha256
unzip -l patch-musica-0.3.96-incremental.zip
```

Faça backup dos arquivos de produção que serão substituídos e aplique:

```bash
opus_original_backup="$HOME/backup-musica-antes-0.3.96-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$opus_original_backup" -C /home/ubuntu/bot \
  cogs/musica/runtime_telefone/agente/preparacao_audio.py \
  cogs/musica/runtime_telefone/agente/reproducao.py \
  cogs/musica/runtime_telefone/agente/servidor.py \
  cogs/musica/runtime_telefone/agente/mixer_pcm.py \
  cogs/musica/runtime_telefone/agente/utilitarios.py \
  cogs/musica/runtime_telefone/agente/validade_stream.py \
  scripts/testar-saida-musica.py

opus_original_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.96.XXXXXX)
unzip -q "$HOME/patch-musica-0.3.96-incremental.zip" -d "$opus_original_patch_dir"
cp -a "$opus_original_patch_dir/tts-bot-main/." /home/ubuntu/bot/
printf '%s\n' "$opus_original_backup"
```

Guarde o caminho impresso do backup. Se instalou manualmente, ou o updater
não publicou o runtime, publique com o Python do bot e reinicie o serviço:

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

Esperado no último comando: `active`. O ZIP não inclui `.env`, tokens,
chaves, cache ou músicas. Não é necessário mexer no SSH ou refazer arquivos
de áudio para instalar este patch.

## 2. Atualizar o telefone

No **Termux**, com o transporte conectado e a música parada:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado: Music Agent **0.3.96**, `available: true` e
`discord_ready: true`. O bootstrap continua anunciando Worker **1.11.43**;
o hash do runtime muda. Se os arquivos já estiverem atualizados mas o
processo ainda anunciar 0.3.95, reinicie uma vez:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Baixe o auxiliar **testar-saida-musica-v8.py** em Downloads e copie:

```bash
cp "$HOME/storage/downloads/testar-saida-musica-v8.py" "$HOME/testar-saida-musica.py"
python "$HOME/testar-saida-musica.py" direct --guild 927002914449424404 --mode auto
python "$HOME/testar-saida-musica.py" status --guild 927002914449424404
```

O auxiliar usa as configurações e o token já instalados. `status` só consulta.
O modo `auto` é salvo por servidor e não altera o volume ou os efeitos.

## 3. Registrar a validação

Abra uma sessão do Termux antes dos testes e deixe registrando:

```bash
mkdir -p "$HOME/music-start-tests"
tail -n 0 -F "$HOME/phone-worker/music_agent.log" \
  | grep --line-buffered -E '\[music-start\]|original_pcm_handoff|audio_source_selected|audio_effect|tts_duck|tts_restore' \
  | tee "$HOME/music-start-tests/validacao-opus-0.3.96.log"
```

Faça as consultas de status em outra sessão do Termux. Para encerrar a
captura, pressione **Ctrl+C** na sessão do log.

## 4. Testar a troca para o original

Anote seu volume e efeitos atuais para restaurar depois. No Discord:

1. Desligue os efeitos e use `_volume 100`.
2. Inicie uma faixa cuja fonte seja AAC/MP3 e que seja preparada em Opus.
   Pode ser uma música já arquivada; o original arquivado é preservado.
3. Durante a música, consulte o status no Termux:

```bash
python "$HOME/testar-saida-musica.py" status --guild 927002914449424404 \
  | tee "$HOME/music-start-tests/antes-troca-0.3.96.json"
```

Antes da troca, procure em `audio_output.passthrough`:

- `copy_active: true` e `output_mode: opus_direct`.
- `pcm_origin: prepared_opus`.
- Em `source`, `prepared_opus: true` e o codec da fonte preparada.

Se a faixa já mostrar `pcm_origin: source`, ela pode ser Opus nativo ou já
estar usando o original; escolha outra para exercitar a preparação. Se
começou com volume 55 ou algum efeito, o PCM original desde o início é
esperado e esse teste específico não foi exercitado.

No Discord, altere para `_volume 55`. O volume deve mudar no mixer atual.
Após alguns segundos, consulte:

```bash
python "$HOME/testar-saida-musica.py" status --guild 927002914449424404 \
  | tee "$HOME/music-start-tests/depois-troca-0.3.96.json"
```

Esperado quando a tentativa tiver sucesso e a música estiver audível:

- `pcm_origin: original`: o mixer passou a consumir PCM reaberto do original.
- `processed_active: true`, `output_mode: opus_processed` e `copy_active: false`.
- `opus_transport: true`; `processed_bitrate_kbps` mostra o encoder aplicado.
- Uma entrada `original_pcm_handoff` registra o resultado da tentativa.
- A faixa segue na posição atual, com fila e TTS preservados.

`pcm_origin` identifica a entrada atual; sozinho não comprova cópia ou
processamento musical ativo. Pausa, silêncio ou TTS isolado podem remover
momentaneamente a prova de saída musical. Se a original não abrir a tempo,
`pcm_origin` pode continuar `prepared_opus`: a reprodução anterior é mantida
e o resultado da tentativa deve ser conferido no log.

Repita **com uma nova faixa preparada em cada teste**:

1. A 100%, ative **Bassboost** em **Mais opções**. Confira a troca para
   `original` e a mudança no som.
2. A 100%, envie uma fala pelo TTS habitual. Confira se a fala e a música
   continuam corretas e se o volume normal retorna depois do duck.
3. Durante o aquecimento, pule para outra música. A tentativa anterior não
   pode substituir a música nova, reiniciar sua posição ou apagar a fila.
4. Teste pausa/retomada, volume zero, retorno ao volume normal e TTS no mudo.
   O zero deve continuar válido, sem a música voltar audível após a fala.
5. Se usa mais de um servidor, confirme que volume, TTS e fonte de um
   servidor não alteram a reprodução do outro.

Depois de processada, a faixa continua nesse caminho até terminar. Voltar
ao volume 100 não força uma alternância de encoder no meio da mesma música.

## 5. Conferir Opus nativo e arquivamento

Desligue os efeitos, use volume 100 e inicie uma faixa Opus original
compatível. `copy_active: true` com `pcm_origin: source` é esperado. Ao
alterar volume ou iniciar TTS, o mixer pode processar esse original; não há
uma preparação intermediária para remover e não é necessária a troca
`original_pcm_handoff` dessa faixa.

Para o arquivamento, acompanhe uma música **nova** arquivada pelo fluxo
habitual e depois reproduza-a. Quando o extrator oferece opções comparáveis,
a escolha de Opus deve seguir a mesma política da reprodução online. AAC
ainda é uma escolha válida quando a alternativa é superior segundo essa
política, quando existe preferência de idioma ou quando a configuração
explícita pede outro formato. Não apague posts, manifestos ou anexos para
forçar o teste. Downloads já preparados são retomados sem alterar os bytes.

## 6. Salvar resultados e testar localmente

Durante uma música processada, salve o status final e copie os registros
para Downloads:

```bash
python "$HOME/testar-saida-musica.py" status --guild 927002914449424404 \
  | tee "$HOME/music-start-tests/saida-opus-0.3.96.json"
cp "$HOME/music-start-tests/validacao-opus-0.3.96.log" "$HOME/storage/downloads/"
cp "$HOME/music-start-tests/antes-troca-0.3.96.json" "$HOME/storage/downloads/"
cp "$HOME/music-start-tests/depois-troca-0.3.96.json" "$HOME/storage/downloads/"
cp "$HOME/music-start-tests/saida-opus-0.3.96.json" "$HOME/storage/downloads/"
```

Na VPS, os testes locais do patch podem ser executados sem conectar ao
Discord ou baixar músicas:

```bash
cd /home/ubuntu/bot
/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python -m unittest \
  tests.test_music_audio_selection_shared \
  tests.test_music_original_pcm_hook \
  tests.test_music_original_pcm_routing \
  tests.test_music_original_pcm_native \
  tests.test_music_output_helper -v
```

Os testes verificam a seleção compartilhada, limites de propriedade das
fontes, cancelamento, troca entre leituras, preservação do mixer e os
metadados do auxiliar. As fronteiras Discord, extrator e rede são adaptadas;
o resultado local não mede latência ou qualidade perceptiva no seu telefone.
O teste nativo usa FFmpeg e libopus locais para abrir AAC real, copiar Opus
preparado e depois conferir o PCM do original entregue ao encoder. Se essas
dependências não estiverem disponíveis, esse teste é marcado como ignorado.
Ao terminar, restaure o volume e os efeitos que você anotou.

## 7. Reverter para 0.3.95

Pare a música e restaure os arquivos de produção do backup pelo caminho real:

```bash
tar -xzf "$HOME/backup-musica-antes-0.3.96-AAAAMMDDTHHMMSS.tar.gz" -C /home/ubuntu/bot
```

Publique novamente o runtime com o comando da seção 1, reinicie
`tts-bot.service`, execute o bootstrap no Termux e inicie o Music Agent.
Confirme **0.3.95**. O auxiliar v8 também lê a versão anterior; nesse caso,
`pcm_origin` não estará disponível. Arquivos de documentação e testes novos
podem permanecer sem afetar o serviço. Originais, músicas arquivadas, cache
e preferências persistidas não precisam ser restaurados por este patch.
