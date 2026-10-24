# Music Agent 0.3.91 — ativação e indicador de Opus direto

Patch incremental sobre a **0.3.90 já instalada** nesta conversa. Contém
somente arquivos alterados ou novos, nas pastas originais `tts-bot-main/`.
O Phone Worker permanece **1.11.43**.

## Correções

- O HTTP 400 ao alterar saída vinha de uma chamada a `get_state`, inexistente
  no Music Agent. O comando agora usa o registro real de estado. Isso corrige
  `direct`, `profile`, `protection` e `reset`. A autenticação estava funcionando.
- O painel mostra **Opus direto** somente quando há confirmação de cópia na
  faixa atual, na mesma sessão/player/fonte/geração. Selecionar `auto` não basta.
- Volume, TTS, pausa, troca de faixa, reconexão ou outro fallback removem o
  indicador. A revisão do estado e a assinatura do painel acompanham essa
  mudança sem gerar edição por contadores de pacotes.
- O auxiliar distingue erro de comando (400) de autenticação (401).

O codec e o bitrate da fonte continuam aparecendo como antes. O patch não
altera codec de saída, seleção da fonte, volume ou limites do canal.

## Sobre o status enviado

A versão 0.3.90 e o hash instalados estavam corretos. `settings: {}` e
`revision: 0` confirmam que a tentativa falhou antes de salvar o modo.

Mesmo após corrigir a ativação, aquela faixa tem fonte de **131 kbps** em
canal limitado a **96 kbps**, além de volume diferente de 100%. Nessas
condições ela continua em PCM e o rótulo correto permanece **Opus**.
O perfil 256 não ultrapassa o teto de 96 do canal. Para cópia, a fonte e os
pacotes precisam caber na capacidade disponível e o volume precisa ser
100%, sem efeitos/TTS. O padrão de 55% permanece preservado.

## 1. Instalar na VPS

Pare a música e aplique o ZIP pelo updater habitual. Mescle o conteúdo de
`tts-bot-main/` em `/home/ubuntu/bot`. É necessário atualizar a VPS e o runtime
do telefone, porque o painel roda na VPS.

Para instalação manual, coloque o ZIP em
`~/patch-musica-0.3.91-incremental.zip` e execute na **VPS**:

```bash
unzip -l ~/patch-musica-0.3.91-incremental.zip

opus_panel_backup="$HOME/backup-musica-antes-0.3.91-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$opus_panel_backup" -C /home/ubuntu/bot \
  cogs/musica/runtime_telefone/agente/estado.py \
  cogs/musica/runtime_telefone/agente/servidor.py \
  cogs/musica/nucleo/modelos.py \
  cogs/musica/agente_telefone/conversao.py \
  cogs/musica/agente_telefone/monitor.py \
  cogs/musica/interface/componentes.py \
  cogs/musica/reproducao/sincronizacao.py

opus_panel_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.91.XXXXXX)
unzip -q ~/patch-musica-0.3.91-incremental.zip -d "$opus_panel_patch_dir"
cp -a "$opus_panel_patch_dir/tts-bot-main/." /home/ubuntu/bot/
```

Guarde o caminho do backup. O patch não inclui `.env`, chaves, tokens ou dados
do arquivo. Se instalou manualmente, ou se o updater não publicou o runtime:

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

No **Termux**, com o transporte conectado e a publicação concluída:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado: Music Agent **0.3.91**, disponível e conectado ao Discord.
Se o arquivo foi atualizado mas o processo ainda anunciar 0.3.90, com a
música parada reinicie uma vez:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Não é necessário reconfigurar SSH, firewall ou armazenamento do Termux.

O auxiliar v2 já consegue ativar o modo após corrigir o servidor. Para receber
as mensagens de erro corrigidas, baixe `testar-saida-musica-v3.py` em Downloads:

```bash
cp "$HOME/storage/downloads/testar-saida-musica-v3.py" "$HOME/testar-saida-musica.py"
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode auto
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Esperado: comando com `ok: true`, `settings.opus_direct: auto` e revisão
atualizada. A ativação é por servidor e em memória, até o agente reiniciar.

## 3. Confirmar o rótulo

No Discord, mantenha o volume habitual se estiver apenas verificando a
correção do comando. Nesse caso pode continuar aparecendo **Opus**.

Para testar cópia, use uma faixa Opus compatível que caiba no limite do
canal. Anote o volume anterior, use `_volume 100`, desligue efeitos e inicie
a faixa novamente desde o começo. Ajuste o volume de escuta do dispositivo
conforme necessário; o volume do bot não é elevado automaticamente.

Consulte no Termux:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Se `audio_output.passthrough.copy_active` for `true`, os pacotes estiverem
sendo enviados e a música estiver tocando, o painel mostrará **Opus direto**
no próximo ciclo normal de atualização. Se ficar falso, consulte `reason`
e `reasons`. Não é necessário reenviar o painel a cada pacote.

Enquanto a cópia estiver ativa, envie um TTS ou use `_volume 90`. Espere o
próximo ciclo do painel: deve voltar para **Opus**. A faixa permanece em PCM
até terminar, como na 0.3.90. Voltar a 100% não reativa cópia no meio dela;
uma próxima faixa compatível pode copiar novamente.

O bitrate exibido continua descrevendo a fonte. Por exemplo,
`Opus · 131 kbps` não significa que o canal de 96 esteja transmitindo a 131.

Para desligar o teste:

```bash
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode off
```

Restaure o volume anterior no Discord (`_volume 55` se era o padrão).

## Diagnóstico se persistir

O auxiliar novo não sugere trocar o token para um HTTP 400. Consulte somente
as linhas de erro do comando, sem imprimir arquivos de ambiente:

```bash
grep -F 'command_error' ~/phone-worker/music_agent.log | tail -n 10
```

Para registrar o teste:

```bash
mkdir -p ~/music-start-tests
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/opus-painel-0.3.91.json
cp ~/music-start-tests/opus-painel-0.3.91.json ~/storage/downloads/
```

## Validação e reversão

Os testes cobrem o handler HTTP real com payload do auxiliar, sem injetar
um método de produção inexistente, além da cadeia estado do runtime →
sincronização da VPS → texto do painel. Verificam cópia real, fallback,
identidade da faixa, cache/revisão, ausência de evidência e reconexão.
Os testes nativos de FFmpeg/libopus da 0.3.90 também foram mantidos.

A validação local usa fronteiras Discord simuladas; não representa uma
sessão Discord real. As limitações de transientes entre encoders e trim do
Opus direto documentadas na 0.3.90 continuam válidas.

Para reverter o código, pare a música, restaure os sete arquivos do backup
na VPS, publique o runtime anterior, reinicie o serviço e atualize o telefone
com o bootstrap. Isso restaura a 0.3.90, incluindo o defeito de ativação que
este patch corrige, sem desfazer o patch SSH 1.11.43.
