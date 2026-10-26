# Music Agent 0.3.97 — fim da fila, STOP vazio e texto de espera

Patch incremental sobre **Music Agent 0.3.96 com o patch de interface da
estimativa de fila instalado**, mantendo as pastas originais `tts-bot-main/`.
Esta atualização deve ser aplicada na **VPS e no telefone**. O Music Agent
passa para **0.3.97**; o Phone Worker continua **1.11.43** e recebe um novo
hash de runtime.

## O que muda no Discord

Quando a última música terminar naturalmente, o bot edita a mensagem do
painel existente. A mesma mensagem deve ficar apenas com este texto:

```text
-# Parece que acabou as músicas <:emoji_86:1564113432905654353>
```

O cartão, a capa, a barra, os botões e o menu desaparecem. Se a mensagem tiver
sido apagada, o bot não publica outra para anunciar o fim. Uma falha de edição
também não autoriza repostar o painel. Atualizações posteriores do monitor ou
o encerramento por inatividade não devem restaurar o cartão antigo.

O Discord não permite remover a marca interna de Components V2 de uma mensagem
já enviada. A apresentação acima usa somente um `TextDisplay` no nível superior,
sem `Container`, preservando o ID da mensagem e a aparência de texto simples.

O comando `_stop` e seus aliases ficam silenciosos quando não houver atividade
musical: não respondem, não publicam painel e não consultam o worker apenas
para executar essa parada vazia. Histórico, conexão de voz isolada ou TTS sozinho
não representam atividade musical. Música pausada, fila e reprodução ainda em
preparação continuam podendo ser interrompidas normalmente. O agente também
verifica seu estado real e retorna `ignored: true` para um STOP vazio, cobrindo
o intervalo em que o espelho da VPS ainda pode estar atrasado.

A última linha da confirmação de inclusão passa a usar texto menor, sem negrito:

```text
-# ⏳ Vai começar em: 3 minutos e 7 segundos
```

O texto respeita singular e plural, por exemplo `1 minuto e 1 segundo`. Os
minutos são o total da espera, mesmo quando ela supera uma hora. A confirmação
mantém a estimativa calculada ao adicionar a faixa; não vira uma contagem
regressiva. Em pausa, a forma é `-# ⏸️ Após retomar: ...`; sem dados suficientes,
aparece `-# ⏳ Previsão indisponível`. A estimativa compacta da lista da fila
continua sendo recalculada ao abrir ou navegar na lista.

## 1. Aplicar o patch na VPS

Envie `patch-musica-0.3.97-incremental.zip` e seu `.sha256` para o diretório
pessoal da VPS. Os comandos abaixo pressupõem o bot em `/home/ubuntu/bot`.
Encerre a reprodução antes de reiniciar os serviços.

Confira o pacote:

```bash
cd "$HOME"
sha256sum -c patch-musica-0.3.97-incremental.sha256
unzip -l patch-musica-0.3.97-incremental.zip
```

Faça backup dos arquivos de produção existentes antes de substituí-los:

```bash
music_stop_backup="$HOME/backup-musica-antes-0.3.97-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$music_stop_backup" -C /home/ubuntu/bot \
  cogs/musica/nucleo/estado.py \
  cogs/musica/reproducao/estimativa_fila.py \
  cogs/musica/reproducao/controle_remoto.py \
  cogs/musica/comandos/base.py \
  cogs/musica/comandos/controle.py \
  cogs/musica/comandos/tocar.py \
  cogs/musica/interface/componentes.py \
  cogs/musica/legado/roteador_audio.py \
  cogs/musica/runtime_telefone/agente/reproducao.py \
  cogs/musica/runtime_telefone/agente/servidor.py
printf '%s\n' "$music_stop_backup"
```

Guarde o caminho impresso. O helper novo
`cogs/musica/reproducao/atividade.py` não possui versão anterior neste backup.

Aplique mesclando as pastas:

```bash
music_stop_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.97.XXXXXX)
unzip -q "$HOME/patch-musica-0.3.97-incremental.zip" -d "$music_stop_patch_dir"
cp -a "$music_stop_patch_dir/tts-bot-main/." /home/ubuntu/bot/
```

Se instalou manualmente, ou se o updater não publicou o novo runtime, publique
com o Python do bot. Depois reinicie o serviço do bot:

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
sudo journalctl -u tts-bot.service -n 40 --no-pager
```

O resultado de `is-active` deve ser **`active`**. O comando acima reinicia o
serviço do bot, não a máquina da VPS.

## 2. Atualizar o telefone

No **Termux**, com o transporte conectado e sem música tocando:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado no status: **`version: 0.3.97`**, **`available: true`**,
**`discord_ready: true`** e nenhuma dependência faltando. O bootstrap ainda
anuncia Worker **1.11.43**, mas o hash do runtime deve ser novo.

Se `file_version` já estiver em 0.3.97 e o processo continuar anunciando 0.3.96,
reinicie o Music Agent uma vez e consulte novamente:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

O auxiliar de diagnóstico de saída já instalado continua servindo; não é
necessário trocar tokens, `.env`, SSH, músicas arquivadas ou configurações de
áudio. A proteção autoritativa de STOP vazio só estará completa quando o
Music Agent também estiver em **0.3.97**.

## 3. Testar no Discord

Faça os testes com um volume confortável. Comece por uma sessão sem música.

1. Envie `_stop` duas vezes. Não deve aparecer resposta nem painel novo.
   Repita sem estar em um canal de voz: não deve aparecer aviso de voz para
   esse comando vazio.
2. Com nenhuma música ativa, teste `_stop` enquanto somente uma fala TTS é
   reproduzida. A fala e a conexão dela devem ser preservadas. O histórico e
   os efeitos não devem ser limpos por esse STOP ignorado.
3. Inicie uma música curta, sem repetição e sem outras faixas na fila. No
   menu da mensagem do painel, copie o link da mensagem antes de ela acabar.
4. Aguarde o fim natural. A mensagem deve virar somente a linha
   `Parece que acabou as músicas` com o emoji pedido, em texto menor. Cartão,
   capa, barra e controles devem desaparecer.
5. Copie o link da mensagem depois do fim. O último número do link, que é o
   ID da mensagem, deve ser o mesmo. Não deve haver outro painel ou mensagem
   de encerramento publicada pelo bot.
6. Espere mais de um minuto e confira novamente. O timer de controles e a
   atualização do monitor não devem fazer o cartão voltar. Confira também
   após o tempo normal de desconexão por inatividade que você já utiliza.
7. Inicie outra música. O novo fluxo deve mostrar seu player normal; um
   encerramento atrasado da sessão anterior não pode substituir esse player.
8. Durante uma música ativa, execute `_stop`. Ele deve parar e limpar a fila
   normalmente. Repita imediatamente: a segunda parada não deve responder ou
   criar painel. Faça também duas chamadas rápidas para conferir a proteção
   contra paradas duplicadas.
9. Teste STOP com música pausada e durante a preparação de uma faixa. A ação
   deve continuar cancelando trabalho musical real. Nenhuma música antiga
   deve começar depois de a preparação ter sido cancelada.
10. Em uma playlist virtual, aguarde uma troca enquanto ainda existem faixas
    pendentes. A prévia materializada vazia não deve ser tratada como fim da
    fila; o texto de encerramento só cabe quando realmente não há trabalho.
11. Em uma sessão curta separada, apague a mensagem do painel antes do fim.
    Quando a música acabar, o bot não deve postar uma mensagem substituta.
12. Caso ainda exista um botão de STOP de um painel antigo, clique nele com
    a sessão vazia. O clique deve ser confirmado silenciosamente, sem aviso
    de painel expirado, mensagem de sucesso ou novo painel.

Para validar o novo texto de espera, mantenha uma música tocando e adicione
outra. Confira a última linha:

```text
-# ⏳ Vai começar em: 3 minutos e 7 segundos
```

O número depende da fila atual. Confira também `1 minuto e 1 segundo`, tempo
abaixo de um minuto e uma espera longa. A duração da faixa recém-adicionada
não entra na espera dela. A confirmação continua com o valor original mesmo
se depois houver skip, pausa, reorganização ou conclusão do título. Ao abrir
a lista completa da fila, as estimativas devem usar o estado do momento.

Uma falha real de permissão ao editar mensagens pode impedir a transformação
do painel. Nesse caso, confira o log da VPS: a tentativa não pode resultar em
uma repostagem. A atualização não concede permissões novas ao bot.

## 4. Consultar o serviço e registrar problemas

Na VPS:

```bash
sudo systemctl is-active tts-bot.service
sudo journalctl -u tts-bot.service --since "15 minutes ago" --no-pager
```

No telefone, consulte o status instalado:

```bash
python ~/testar-music-agent.py status
```

Se precisar relatar um problema, envie o status e descreva se havia música,
pausa, fila pendente ou apenas TTS. Para o painel, envie os links de antes e
depois para conferir a identidade da mensagem. Não envie tokens ou arquivos
de configuração privados.

## 5. Testes locais

Na VPS, depois de aplicar os arquivos, execute com o Python do bot:

```bash
cd /home/ubuntu/bot
/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python -m unittest \
  tests.test_music_activity \
  tests.test_music_idle_stop_worker \
  tests.test_music_idle_stop_controller \
  tests.test_music_terminal_panel \
  tests.test_music_finished_view \
  tests.test_music_play_request_activity \
  tests.test_music_queue_wait_estimate \
  tests.test_music_queue_wait_ui \
  tests.test_music_queue_wait_messages -v
```

Os testes incluídos verificam o texto exato do painel e sua composição, o
caminho somente de edição, os casos de atividade musical, STOP ignorado,
concorrência e o novo formato de espera. As fronteiras de Discord e transporte
são adaptadas; o comando não faz testes de voz contra o serviço real. O
relatório de validação do patch informa o resultado da bateria executada.
Os testes locais não substituem a conferência visual no Discord nem medem
qualidade perceptiva do áudio no telefone.

## 6. Reverter para 0.3.96

Encerre a reprodução. Na VPS, substitua o nome abaixo pelo backup real criado
antes da instalação e restaure:

```bash
tar -xzf "$HOME/backup-musica-antes-0.3.97-AAAAMMDDTHHMMSS.tar.gz" -C /home/ubuntu/bot
```

Publique novamente o runtime com o comando `after-update --force-agent` da
seção 1 e reinicie `tts-bot.service`. No Termux, execute o bootstrap e inicie
o agente novamente conforme a seção 2. Confirme **Music Agent 0.3.96**.

O helper novo, os testes e este guia podem permanecer no disco: os arquivos
de produção restaurados não usam a nova lógica. A reversão não precisa
restaurar músicas, cache de áudio ou preferências persistidas. As confirmações
e mensagens já publicadas no Discord não são reescritas pela reversão.
