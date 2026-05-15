# Phone Worker

Worker opcional para usar o celular como ajudante da VPS em tarefas que não são críticas.

Ele **não substitui a VPS**. Se o celular cair, a VPS continua funcionando e usa fallback local.

## O que ele expõe

- `GET /health` e `GET /status`: saúde do worker.
- `POST /task`: tarefas opcionais diretas usadas por partes antigas da VPS.
- polling seguro no registry da VPS:
  - `POST /core-worker/jobs/poll` na VPS para buscar job pendente;
  - `POST /core-worker/jobs/result` na VPS para devolver resultado.
- `POST /task`: tarefas opcionais:
  - `ping`
  - `sha256`
  - `zip`
  - `text_stats`
  - `log_extract`
  - `log_summary` para resumir logs, contar erros e agrupar mensagens repetidas
  - `ffprobe_media` quando `ffprobe` estiver disponível junto do ffmpeg
  - `ffmpeg_convert` quando `ffmpeg` estiver instalado no Termux.

Todas as rotas usam token via:

```txt
Authorization: Bearer <PHONE_WORKER_TOKEN>
```

ou:

```txt
X-Phone-Worker-Token: <PHONE_WORKER_TOKEN>
```

## Instalação no Termux

Copie esta pasta para o celular e rode:

```bash
cd ~/phone-worker-install
bash install.sh
nano ~/.phone-worker.env
~/start-phone-worker.sh
```

Para manter em watchdog local:

```bash
tmux new-session -d -s phone-worker-watch '~/watch-phone-worker.sh'
```

## Variáveis da VPS

No `.env` da VPS:

```env
PHONE_WORKER_ENABLED=true
PHONE_WORKER_HOST=100.x.y.z
PHONE_WORKER_PORT=8766
PHONE_WORKER_TOKEN=troque_essa_chave
PHONE_WORKER_SSH_USER=u0_a000
PHONE_WORKER_SSH_PORT=8022
PHONE_WORKER_START_COMMAND=/data/data/com.termux/files/home/start-phone-worker.sh
```

O timer da VPS chama `scripts/phone-worker-watch.sh` para manter o worker acordado quando possível.

Variáveis opcionais usadas pelos diagnósticos do bot:

```env
PHONE_WORKER_QUICK_STATUS_ENABLED=true
PHONE_WORKER_QUICK_STATUS_TIMEOUT_SECONDS=1.2
PHONE_WORKER_LOG_SUMMARY_TIMEOUT_SECONDS=7
PHONE_WORKER_LOG_SUMMARY_MAX_INPUT_MB=8
PHONE_WORKER_LOG_SUMMARY_MAX_RECENT=12
PHONE_WORKER_LOG_SUMMARY_MAX_TOP=12
```
## Uso fora do `/vps`

Além dos diagnósticos, a VPS pode usar o phone-worker para preparar áudio curto de TTS antes do Lavalink tocar. Isso é opcional e tem fallback local automático.

```env
MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED=true
MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS=3.5
MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB=8
```

Se o celular estiver offline, lento ou falhar no `ffmpeg`, a VPS converte localmente e o TTS continua funcionando.


## Tarefas extras v5

O worker também pode ajudar fora do `/vps` em tarefas auxiliares da VPS:

- `zip_validate`: valida ZIPs de update antes de aplicar, detectando caminhos inseguros, symlinks, arquivos grandes, services/scripts e resumo por extensão.
- `maintenance_plan`: analisa uma lista de arquivos enviada pela VPS e devolve candidatos de limpeza/maiores arquivos sem apagar nada sozinho.
- `log_summary`: resume logs grandes para auto-update, rollback futuro e diagnósticos internos.

Essas tarefas são sempre opcionais. Se o celular estiver offline, a VPS continua usando o caminho local normal.

## Pareamento no Core Worker registry

Depois do painel `workers`, o phone-worker pode entrar no registry multi-worker da VPS e deixar de aparecer apenas como worker direto/legacy.

Fluxo recomendado:

1. No Discord, use `workers` na guild privada.
2. Clique em **Parear worker** para gerar um código temporário.
3. No Termux atualizado, rode:

```bash
~/pair-phone-worker.sh CORE-XXXX http://IP_TAILSCALE_DA_VPS:8766
```

Também é possível fazer direto pelo Python:

```bash
cd ~/phone-worker
python phone_worker.py --pair CORE-XXXX --vps-url http://IP_TAILSCALE_DA_VPS:8766
```

O script chama `POST /core-worker/pair`, salva `CORE_WORKER_ID`, `CORE_WORKER_TOKEN`, `CORE_WORKER_VPS_URL`, ativa heartbeat/jobs em `~/.phone-worker.env` e nunca imprime o token. Reinicie o worker depois do pareamento:

```bash
~/start-phone-worker.sh
```

Teste manual sem iniciar servidor novo:

```bash
cd ~/phone-worker
python phone_worker.py --heartbeat-once
python phone_worker.py --jobs-once
```

O heartbeat envia status, bateria/rede quando o Termux:API estiver disponível, ffmpeg/ffprobe, disco, saúde básica e um resumo do Tailscale quando a CLI existir. Com jobs habilitados, o worker consulta a VPS por polling e executa somente jobs whitelisted (`ping`, `status`, `diagnostic_basic`, `worker_self_check`, `worker_logs`, `network_probe`, `tailscale_status`, `service_status`, `service_start`, `service_stop`, `service_restart`, `ffmpeg_check`, `ffprobe_check`, `worker_update`, `zip_validate`, `log_summary`, `text_stats`, `maintenance_plan`). Não existe execução de shell livre pelo registry. O token fica só no `~/.phone-worker.env`; o registry da VPS guarda apenas hash.

## Controle seguro de serviços

O painel `workers` agora consegue criar jobs para serviços whitelisted do celular:

- `phone-worker`: status, start, stop e restart do agente atual. Para `stop`/`restart`, o worker responde primeiro à VPS e só depois agenda a ação para não deixar o job preso.
- `phone-worker-watch`: start, stop, restart e status do watchdog em `tmux`.
- `tailscale`: diagnóstico/status apenas. Se você usa o app oficial do Tailscale no Android, start/stop continuam sendo feitos pelo próprio app/VPN do Android; o worker só testa conectividade e mostra se a VPS é alcançável.

Ações no painel privado `workers`:

- **Saúde**: cria `worker_self_check`.
- **Logs**: cria `worker_logs`.
- **Tailscale**: cria `tailscale_status`.
- **Status serviços**: cria `service_status`.
- **Iniciar/Parar watchdog**: controla a sessão `phone-worker-watch`.
- **Reiniciar/Parar worker**: controla o `phone-worker` atual com ação deferida.


## Auto-update seguro do phone-worker

O painel privado `workers` agora tem **Atualizar worker**. Esse botão executa `scripts/sync-phone-worker.sh` na VPS para copiar a versão atual desta pasta para o Termux via SSH e reiniciar o worker. Ele não copia `~/.phone-worker.env` e não envia tokens para o GitHub.

Workers pareados no registry que já declaram suporte a `worker_update` também podem receber o job **Atualizar agent**. Esse job só grava arquivos whitelisted:

- `phone_worker.py` em `~/phone-worker/`;
- `install.sh`, `README.md` e `phone-worker.env.example` em `~/phone-worker/`;
- `start-phone-worker.sh` e `watch-phone-worker.sh` no `$HOME`.

O update confere `sha256`, faz backup `.bak` quando possível e, por padrão, reinicia o phone-worker só depois de responder o resultado para a VPS.

Variáveis locais opcionais:

```env
PHONE_WORKER_SELF_UPDATE_ENABLED=true
PHONE_WORKER_UPDATE_RESTART=true
PHONE_WORKER_UPDATE_MAX_FILE_BYTES=524288
PHONE_WORKER_UPDATE_MAX_TOTAL_BYTES=1048576
```
