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
PHONE_WORKER_HOST=100.81.92.35
PHONE_WORKER_PORT=8766
PHONE_WORKER_TOKEN=troque_essa_chave
PHONE_WORKER_SSH_USER=u0_a412
PHONE_WORKER_SSH_PORT=8022
PHONE_WORKER_START_COMMAND=/data/data/com.termux/files/home/start-phone-worker.sh
```

O timer da VPS chama `scripts/phone-worker-watch.sh` para manter o worker acordado quando possível.
