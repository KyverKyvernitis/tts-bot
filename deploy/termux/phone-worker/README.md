# Phone Worker

Worker opcional para usar o celular como ajudante da VPS em tarefas que não são críticas.

Ele **não substitui a VPS**. Se o celular cair, a VPS continua funcionando e usa fallback local.

## O que ele expõe

- `GET /health` e `GET /status`: saúde do worker.
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
