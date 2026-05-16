# Phone Worker

Worker opcional para usar o celular como ajudante da VPS em tarefas que não são críticas.

Ele **não substitui a VPS**. Se o celular cair, a VPS continua funcionando e usa fallback local.

## v1.6.6 — auto-update por jobs da VPS

A versão `1.6.6` foi preparada para o fluxo automático pós-update da VPS:

- quando o updater detectar mudanças em `deploy/termux/phone-worker/`, ele agenda jobs `worker_update` para agents online compatíveis depois que o bot reiniciar e passar no healthcheck;
- o worker valida os arquivos recebidos, confere SHA-256, aplica apenas alvos permitidos e reinicia de forma adiada/segura;
- quando o updater detectar mudanças no APK, a VPS agenda um job `apk_build_debug` para workers com perfil/capability `apk-builder`;
- builds de APK rodam em diretório temporário no celular builder e publicam o resultado na VPS, sem sujar o repositório principal.

## O que ele expõe

- `GET /health` e `GET /status`: saúde do worker.
- `GET /local/status`: status básico para o APK, aceitando apenas localhost.
- `POST /local/profile`: atualiza o perfil/roles/capabilities deste próprio worker pelo APK, aceitando apenas localhost.
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

As rotas remotas (`/health`, `/status` e `/task`) usam token. As rotas `/local/*` são exclusivas de localhost para integração com o APK e não retornam tokens. Para rotas remotas, use:

```txt
Authorization: Bearer <PHONE_WORKER_TOKEN>
```

ou:

```txt
X-Phone-Worker-Token: <PHONE_WORKER_TOKEN>
```


## Ponte local com o APK Core Worker

O APK usa apenas rotas locais, sempre em `127.0.0.1`, para não transformar o app em painel avançado:

```txt
GET  http://127.0.0.1:8766/local/status
POST http://127.0.0.1:8766/local/profile
POST http://127.0.0.1:8766/local/pair
POST http://127.0.0.1:8766/local/heartbeat
```

Essas rotas:

- só aceitam chamadas vindas de localhost/`127.x.x.x`/`::1`;
- não exigem o `PHONE_WORKER_TOKEN`, porque não devem sair do próprio celular;
- não retornam tokens;
- não executam shell livre;
- não expõem fila completa nem controle pesado;
- só mostram status básico e permitem trocar o perfil do próprio worker (`leve`, `midia`, `completo`, `bedrock`).

Quando o perfil é atualizado, o worker salva `CORE_WORKER_PROFILE`, `CORE_WORKER_ROLES` e `CORE_WORKER_CAPABILITIES` no `~/.phone-worker.env` e tenta mandar um heartbeat para a VPS se o registry já estiver configurado.

Desde a versão `1.6.0`, o APK também pode pedir pareamento local por `POST /local/pair`. Essa rota recebe `vps_url`, `code`, `name` e `profile`, chama o pareamento real da VPS a partir do próprio Termux worker e salva `CORE_WORKER_ID`/`CORE_WORKER_TOKEN` apenas no `~/.phone-worker.env`. O token não volta para o APK. Assim o APK não cria um registro `apk-*` duplicado no registry.

`POST /local/heartbeat` apenas pede para o Termux worker enviar um heartbeat imediato para a VPS. O APK não envia heartbeat próprio.

## Instalação no Termux

Copie esta pasta para o celular e rode:

```bash
cd ~/phone-worker-install
bash install.sh
nano ~/.phone-worker.env
~/phone-worker/start-phone-worker.sh
```

Para manter em watchdog local:

```bash
tmux new-session -d -s phone-worker-watch '~/phone-worker/watch-phone-worker.sh'
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
PHONE_WORKER_START_COMMAND=/data/data/com.termux/files/home/phone-worker/start-phone-worker.sh
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
~/phone-worker/pair-phone-worker.sh CORE-XXXX http://IP_TAILSCALE_DA_VPS:10000
```

Também é possível fazer direto pelo Python:

```bash
cd ~/phone-worker
python phone_worker.py --pair CORE-XXXX --vps-url http://IP_TAILSCALE_DA_VPS:10000
```

O script chama `POST /core-worker/pair`, salva `CORE_WORKER_ID`, `CORE_WORKER_TOKEN`, `CORE_WORKER_VPS_URL`, ativa heartbeat/jobs em `~/.phone-worker.env` e nunca imprime o token. Reinicie o worker depois do pareamento:

```bash
~/phone-worker/start-phone-worker.sh
```

Teste manual sem iniciar servidor novo:

```bash
cd ~/phone-worker
python phone_worker.py --heartbeat-once
python phone_worker.py --jobs-once
```

O heartbeat envia status, bateria real via Termux:API quando disponível, ping TCP até a VPS, rede, ffmpeg/ffprobe, disco, saúde básica e um resumo do Tailscale quando a CLI existir. Com jobs habilitados, o worker consulta a VPS por polling e executa somente jobs whitelisted (`ping`, `status`, `diagnostic_basic`, `worker_self_check`, `worker_logs`, `network_probe`, `tailscale_status`, `service_status`, `service_start`, `service_stop`, `service_restart`, `ffmpeg_check`, `ffprobe_check`, `worker_update`, `boot_status`, `boot_repair`, `zip_validate`, `log_summary`, `text_stats`, `maintenance_plan`). Não existe execução de shell livre pelo registry. O token fica só no `~/.phone-worker.env`; o registry da VPS guarda apenas hash.


## Supervisor local e anti-duplicação

O `start-phone-worker.sh` agora atua como supervisor local:

- usa lock para evitar duas inicializações ao mesmo tempo;
- mata processos antigos/duplicados de `phone_worker.py` antes de iniciar;
- grava PID em `~/phone-worker/phone-worker.pid`;
- grava status curto em `~/phone-worker/phone-worker.status`;
- rotaciona logs quando passam de `PHONE_WORKER_LOG_MAX_BYTES`;
- inicia com `nohup` sem depender de `tmux`;
- o `watch-phone-worker.sh` só chama o supervisor e aplica backoff quando houver falha.

Variáveis úteis no `~/.phone-worker.env`:

```env
PHONE_WORKER_LOG_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker.log
PHONE_WORKER_PID_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker.pid
PHONE_WORKER_STATUS_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker.status
PHONE_WORKER_LOG_MAX_BYTES=1048576
PHONE_WORKER_START_KILL_DUPLICATES=true
PHONE_WORKER_WATCH_MAX_BACKOFF_SECONDS=300
```

No painel `workers`, a ação **Status serviços** mostra PID, duplicados, runtime e logs. Se aparecer `runtime atenção`, use **Manutenção → Reiniciar worker** ou **Manutenção → Reparar scripts**.

## Boot automático pós-reboot

O `install.sh`, o `bootstrap-phone-worker.sh`, o sync da VPS e a ação **Reparar boot automático** criam/reparam:

```bash
~/.termux/boot/10-core-worker
```

Esse script é lido pelo app **Termux:Boot** quando o Android inicia. Ele espera alguns segundos, segura wake-lock quando possível e chama `~/phone-worker/start-phone-worker.sh`.

Depois de instalar/reparar, abra o app **Termux:Boot** uma vez e, no Android/MIUI, libere inicialização automática e bateria sem restrição para:

- Termux
- Termux:Boot
- Termux:API
- Tailscale

O painel `workers` mostra `boot ok`, `boot faltando` ou `boot incompleto`. Se aparecer faltando/incompleto, use **Manutenção → Reparar boot automático**.

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
- `start-phone-worker.sh`, `watch-phone-worker.sh` e `pair-phone-worker.sh` em `~/phone-worker` e também no `$HOME` por compatibilidade.

O update confere `sha256`, faz backup `.bak` quando possível e, por padrão, reinicia o phone-worker só depois de responder o resultado para a VPS.

Variáveis locais opcionais:

```env
PHONE_WORKER_SELF_UPDATE_ENABLED=true
PHONE_WORKER_UPDATE_RESTART=true
PHONE_WORKER_UPDATE_MAX_FILE_BYTES=524288
PHONE_WORKER_UPDATE_MAX_TOTAL_BYTES=1048576
```


## Atualização do APK Core Worker

A atualização do APK ficou simples e centrada no app:

```text
VPS publica /core-worker/app/latest.json
APK consulta a VPS quando abre ou quando o usuário pede verificação
APK mostra notificação local se existir versão nova
APK mostra o botão Atualizar no topo apenas quando houver update
usuário toca em Atualizar
APK baixa, valida SHA-256 quando informado e abre o instalador do Android
```

O phone-worker não baixa nem instala o APK pelo painel `workers`. O painel continua focado em controlar/monitorar workers; o app cuida da própria atualização. No Android comum, a instalação ainda precisa da confirmação do usuário.

## Onboarding rápido de novo celular

Este fluxo é temporário enquanto o APK **Core Worker** não existe. No APK, o pareamento, token, start do agent, heartbeat, boot automático e seleção de perfil serão feitos automaticamente por botão/QR.

Para adicionar um segundo celular hoje:

1. Conecte o celular no Tailscale.
2. Copie/instale esta pasta como `~/phone-worker`.
3. No painel privado `workers`, use **Parear novo worker**.
4. O painel já gera um comando pronto com código temporário e URL real da VPS. Copie esse comando no Termux do novo celular.

Exemplo do comando gerado:

```bash
cd ~/phone-worker && bash ./bootstrap-phone-worker.sh CORE-XXXX http://100.x.x.x:10000 "Core Worker 2" midia
```

O bootstrap instala/repara dependências básicas, pareia, salva `~/.phone-worker.env`, inicia o worker e tenta um heartbeat. Se o worker já estiver instalado, o painel também mostra o comando curto com `pair-phone-worker.sh`.

Perfis aceitos pelo `pair-phone-worker.sh` e pelo bootstrap:

- `leve`: `phone-worker, diagnostics, log-summary`
- `midia`: `phone-worker, diagnostics, log-summary, zip-validate, ffmpeg, ffprobe, tts-convert`
- `completo`: `phone-worker, diagnostics, log-summary, maintenance-plan, zip-validate, ffmpeg, ffprobe, tts-convert`
- `bedrock`: reservado para o futuro worker Bedrock (`bedrock`, `bedrock-logs`, `bedrock-backup`), não assume Java.

Também é possível passar uma lista customizada no lugar do perfil:

```bash
~/phone-worker/pair-phone-worker.sh CORE-XXXX http://100.x.x.x:10000 "Worker Logs" "phone-worker,diagnostics,log-summary"
```

Com 2 ou mais workers online, o painel `workers` libera **Melhor worker disponível** e **Teste failover**. Jobs sem alvo fixo são entregues para qualquer worker compatível; se um job em execução perder lease, ele volta para a fila e outro worker compatível pode assumir.


## Builder de APK em worker

O worker pode ser marcado como `builder`/`apk-builder` para compilar o APK Core Worker fora da VPS. Isso é pensado para aliviar a VPS Oracle de 1 GB RAM.

Fluxo:

```text
painel workers -> job apk_build_debug
VPS cria source-core-worker-app.zip leve
worker builder baixa o source zip
worker roda gradle assembleDebug
worker envia o APK + sha256 para /core-worker/app/publish
VPS publica latest.json
APK mostra Atualizar no topo quando abrir/verificar
```

Segurança:

- não existe shell livre;
- só job whitelisted `apk_build_debug`;
- só worker com role/capability `apk-builder` recebe esse job;
- o endpoint de publicação exige token do worker;
- a VPS só aceita publicação de worker autenticado e com `apk-builder`.

Requisitos no celular builder:

- Java/Gradle/Android SDK command-line tools instalados no Termux ou ambiente compatível;
- espaço livre suficiente;
- perfil `builder` aplicado no APK ou funções `apk-builder` adicionadas pelo painel.

Variáveis úteis:

```env
PHONE_WORKER_APK_BUILD_ENABLED=true
PHONE_WORKER_APK_BUILD_TIMEOUT_SECONDS=3600
PHONE_WORKER_APK_BUILD_DIR=/data/data/com.termux/files/home/core-worker-apk-builds
```


## Builder de APK

Na versão `1.6.5`, o worker pode continuar usando o perfil `builder`/`apk-builder` para compilar o Core Worker fora da VPS. O worker compila e envia o APK para a VPS, mas a VPS deve re-assinar/publicar o APK com uma chave fixa local. A chave privada não deve ficar no Termux nem no GitHub.

O builder em Termux tende a funcionar melhor com Android SDK 34 e `aapt2` do próprio Termux. Se o build falhar com `aapt2` ou `android.jar`, prepare o ambiente com SDK 34 e mantenha `android.aapt2FromMavenOverride` apontando para `/data/data/com.termux/files/usr/bin/aapt2`.
