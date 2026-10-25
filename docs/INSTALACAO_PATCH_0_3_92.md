# Music Agent 0.3.92 — Opus direto automático e persistente

Patch incremental para instalar sobre a **0.3.91**. Se ainda estiver na
0.3.90, aplique primeiro o patch 0.3.91 entregue nesta conversa; ele inclui
as correções do painel que este pacote pressupõe. O ZIP contém
somente arquivos alterados ou novos, dentro das pastas originais
`tts-bot-main/`. O Phone Worker continua **1.11.43**.

## O que muda

- `auto` passa a ser o padrão: cada nova faixa tenta copiar os pacotes Opus
  originais quando são compatíveis. Uma configuração explícita
  `MUSIC_AGENT_OPUS_DIRECT=off` continua sendo respeitada como padrão global.
- Escolhas explícitas de `auto` e `off` ficam salvas por servidor e sobrevivem
  ao reinício do agente e à atualização do runtime. O status mostra a
  preferência persistida mesmo sem uma sessão de música ativa.
- O ajuste temporário de bitrate do canal acontece antes da preparação do
  áudio. Usa a política existente, a permissão **Gerenciar canais**, o limite
  do servidor Discord e a carga de servidores tocando ou iniciando música.
- A preparação do canal compartilha trabalho entre comandos simultâneos.
  Quando houver preparação antecipada de voz, o ajuste e o handshake ocorrem
  em paralelo. Uma preparação cancelada não desfaz o ajuste de outra aceita.
- Após uma edição confirmada, o agente espera brevemente seu próprio cache
  Discord observar o novo bitrate. Ele não assume capacidade só porque a VPS
  pediu o ajuste. Sem confirmação, usa o limite observado e o caminho normal.

O volume padrão continua em **55%**. A seleção da fonte e os controles de
qualidade/proteção existentes são preservados. O painel continua mostrando
**Opus direto** somente enquanto a faixa atual realmente está copiando pacotes.

## Quando uma faixa pode usar Opus direto

Todas as músicas recebem a tentativa automática, mas nem todas permitem cópia.
O envio de áudio para Discord já usa Opus; **Opus direto** evita a recodificação
dos pacotes originais compatíveis. Para isso é necessário:

- Fonte Opus de 48 kHz, estéreo, pacotes de 20 ms, com cabeçalhos e pacotes
  aprovados pela validação existente e bitrate que caiba na saída/canal.
- Volume do bot em 100%, sem efeitos ou mistura de TTS, início compatível
  desde o começo da faixa e fonte suportada pelo caminho direto.

Por exemplo: uma fonte de 131 kbps continua sem cópia em canal limitado a
96 kbps. Se o servidor Discord permitir mais e o bot puder editar o canal,
o ajuste antecipado pode remover esse impedimento. O perfil 256 não ultrapassa
o limite do servidor Discord. Não há aumento automático de volume.

Volume diferente de 100%, efeitos, TTS, seek, fonte incompatível e outros
impedimentos mantêm o processamento em PCM e a codificação Opus normal.
Depois de um fallback, a faixa atual permanece nesse caminho até terminar;
uma próxima faixa compatível pode copiar novamente. FEC e bitrate do encoder
não modificam pacotes já codificados que estão sendo copiados. As limitações
de transientes na troca de encoder e de trim do caminho direto permanecem.

## 1. Instalar na VPS

Pare a música. Instale o ZIP pelo updater habitual ou mescle seu conteúdo em
`/home/ubuntu/bot`. Para instalação manual, envie o ZIP para
`~/patch-musica-0.3.92-incremental.zip` e execute na **VPS**:

```bash
unzip -l ~/patch-musica-0.3.92-incremental.zip

opus_auto_backup="$HOME/backup-musica-antes-0.3.92-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$opus_auto_backup" -C /home/ubuntu/bot \
  cogs/musica/comandos/tocar.py \
  cogs/musica/interface/componentes.py \
  cogs/musica/legado/roteador_audio.py \
  cogs/musica/reproducao/controle_remoto.py \
  cogs/musica/reproducao/inicio.py \
  cogs/musica/runtime_telefone/agente/configuracao.py \
  cogs/musica/runtime_telefone/agente/preparacao_audio.py \
  cogs/musica/runtime_telefone/agente/reproducao.py \
  cogs/musica/runtime_telefone/agente/servidor.py \
  scripts/testar-saida-musica.py

opus_auto_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.92.XXXXXX)
unzip -q ~/patch-musica-0.3.92-incremental.zip -d "$opus_auto_patch_dir"
cp -a "$opus_auto_patch_dir/tts-bot-main/." /home/ubuntu/bot/
printf '%s\n' "$opus_auto_backup"
```

Guarde o caminho do backup. Para conferir a integridade do download, se também
baixou o arquivo `.sha256`, execute no diretório dos dois arquivos:

```bash
cd "$HOME"
sha256sum -c patch-musica-0.3.92-incremental.sha256
```

O ZIP não inclui `.env`, tokens, chaves ou dados de músicas. Se a instalação
foi manual, ou se o updater ainda não publicou o runtime, execute:

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

Esperado: publicação concluída e serviço `active`. A atualização precisa
chegar à VPS e ao runtime do telefone.

## 2. Atualizar o telefone e o auxiliar

No **Termux**, com o transporte conectado:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado: agente **0.3.92**, `available: true`, `discord_ready: true` e nenhuma
dependência de voz ausente. A versão do Phone Worker continua 1.11.43.
Se o arquivo já foi atualizado mas o processo ainda anunciar a versão anterior,
com a música parada reinicie uma vez e confira novamente:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Baixe o auxiliar **testar-saida-musica-v4.py** em Downloads e copie:

```bash
cp "$HOME/storage/downloads/testar-saida-musica-v4.py" "$HOME/testar-saida-musica.py"
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode auto
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Esperado no status: `requested.opus_direct: auto` e, dentro de
`audio_output.persistence`, `persistent: true`, `ready: true`, `stored: true`,
`mode: auto`. Apenas observar o padrão automático, sem salvá-lo explicitamente,
pode mostrar `stored: false`; isso também é válido.

## 3. Testar persistência

Com a música parada, reinicie o agente e consulte o mesmo servidor:

```bash
python ~/testar-music-agent.py restart
python ~/testar-saida-musica.py status --guild 927002914449424404
```

O modo salvo deve continuar `auto`. Para testar também a escolha de desligar:

```bash
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode off
python ~/testar-music-agent.py restart
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Esperado: `requested.opus_direct: off` e `persistence.mode: off`. Ao concluir,
restaure a tentativa automática:

```bash
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode auto
```

As escolhas são independentes por servidor. `reset` apaga a preferência desse
servidor e volta ao padrão global; também limpa os outros ajustes de saída da
sessão. Perfil e proteção continuam em memória até reiniciar. Não use `reset`
apenas para alternar o modo se quiser manter esses ajustes.

A preferência fica por padrão em
`~/phone-worker/state/music-agent-output-preferences.json`, fora das releases.
Se `PHONE_WORKER_DIR` estiver configurado, usa `state/` desse diretório;
`MUSIC_AGENT_OUTPUT_PREFERENCES_FILE` permite escolher outro caminho. A gravação
é atômica, com permissões 0600, e mantém a versão anterior válida em `.bak`.
As decisões de áudio consultam uma cópia em memória sem esperar a gravação.
Arquivo inválido é preservado e desativa cópia até correção; o status mostra
`ready: false` e `error_type`. Não apague esse estado durante a atualização.

## 4. Testar cópia real e fallback

No Discord, anote o volume atual. Para exercitar a cópia direta, use
`_volume 100`, desligue efeitos e inicie desde o começo uma faixa Opus
compatível. Ajuste o volume de escuta do aparelho conforme necessário.
Não é preciso mudar o volume do bot para usar normalmente o patch.

No Termux, consulte durante a música:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Confira `audio_output.passthrough.copy_active: true` e
`audio_output.applied.output_mode: opus_direct`. O painel deve mostrar
**Opus direto** no ciclo normal de atualização. `auto` sozinho não comprova
cópia. Se ficar em PCM, leia `reason` e `reasons`; por exemplo,
`volume_requires_pcm` ou `source_bitrate_limit` explicam impedimentos.
O bitrate mostrado junto ao título continua descrevendo a fonte.

Com uma faixa copiando, teste pausa/retomada, um TTS e alteração de volume.
Quando houver fallback, o painel deve voltar para **Opus**, a reprodução deve
continuar e a próxima faixa compatível pode usar cópia. Teste também pular,
voltar, fila e uma busca com seleção. Apenas abrir a seleção de resultados
não deve alterar o bitrate do canal; selecionar uma faixa inicia a preparação.

Ao terminar, restaure o volume anterior (`_volume 55` se era o padrão).
Ao encerrar a sessão, o ajuste temporário do canal usa a restauração existente,
respeitando alterações feitas manualmente por administradores.

## 5. Registrar velocidade e diagnóstico

Para comparar com os testes anteriores, use as mesmas faixas e condições:
primeira faixa após reinício, próxima faixa sem reiniciar, busca e URL. Não
troque de rede no meio da comparação. Anote se houve cortes, distorção ou
falha de comandos. No Termux, deixe a captura rodando e faça os testes:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E '\[music-start\]|channel_bitrate_hint_observed|audio_source_selected|opus_encoder_updated|opus_preferences_save_failed' \
  | tee ~/music-start-tests/validacao-opus-0.3.92.log
```

Pressione Ctrl+C depois dos testes. Salve o status e copie os resultados:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/saida-opus-0.3.92.json
cp ~/music-start-tests/validacao-opus-0.3.92.log ~/storage/downloads/
cp ~/music-start-tests/saida-opus-0.3.92.json ~/storage/downloads/
```

`channel_bitrate_hint_observed` informa se o cache observou a edição ou se
houve timeout. A espera padrão no agente é 0,75 s, configurável por
`MUSIC_AGENT_CHANNEL_BITRATE_HINT_WAIT_SECONDS` e limitada a 1,5 s. Só ocorre
quando há indicação de edição confirmada e o cache ainda está abaixo dela.
A preparação na VPS tem prazo total de 2 s, incluindo lock, consulta e edição.
Falha ou falta de permissão permite continuar usando o limite observado.
O tempo de espera do cache entra na medição até o primeiro pacote; etapas
paralelas não devem ser somadas para estimar o tempo total.

Na VPS, para investigar erro recente do serviço:

```bash
sudo journalctl -u tts-bot.service --since '10 minutes ago' --no-pager \
  | grep -E 'music|Traceback|ERROR'
```

## Validação local e reversão

Os testes locais cobrem persistência/reinício, erros de gravação, isolamento
por servidor, leitura sem bloquear o áudio, API real com payload do auxiliar,
cache/timeout/cancelamento, concorrência de preparações, restauração e limites
de carga. Também cobrem o caminho Opus nativo com FFmpeg/libopus, fallback,
telemetria e indicador do painel. As fronteiras Discord são simuladas: é
necessário validar no telefone o ganho de tempo e o comportamento na rede real.
O patch não promete que todas as faixas possam copiar nem um ganho medido
antes desse teste.

Resultado: **283 testes unittest passaram**, incluindo os testes nativos.
Outras **64 verificações existentes** passaram usando adaptadores locais de
fixtures; duas verificações de resolução com threads não foram validadas nesse
ambiente. Essa segunda execução não equivale a uma execução completa do pytest.
O bundle de runtime foi publicado localmente e extraído com os bootstraps
1.0.0 e 1.0.1: hashes, CRC, permissões e sintaxe Python 3.10 aprovados.

Para voltar à 0.3.91, pare a música, restaure os arquivos de produção do backup
e publique novamente o runtime com o comando da etapa 1. Por exemplo, usando
o caminho real do backup que você guardou:

```bash
tar -xzf "$HOME/backup-musica-antes-0.3.92-AAAAMMDDTHHMMSS.tar.gz" -C /home/ubuntu/bot
```

Depois reinicie `tts-bot.service`, rode o bootstrap no Termux e inicie o agente.
Confirme 0.3.91. O arquivo de preferências permanece no telefone, mas a versão
anterior não o utiliza. A configuração SSH/Phone Worker 1.11.43 é preservada.
