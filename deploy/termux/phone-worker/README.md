## Patch 85.11: Android TTS nativo no APK

A versão `1.10.27` do phone worker e o APK `0.5.54` mantêm a ponte ATTS rápida, adiciona catálogo de vozes Android para o painel do bot e registra métricas finas por etapa no caminho ATTS. O serviço persistente do APK mantém uma instância `TextToSpeech` aquecida e expõe somente em `127.0.0.1:8877` as rotas locais `GET /native-tts/status`, `POST /native-tts/synthesize` e `POST /native-tts/synthesize.raw`. O Termux/phone_worker usa a rota binária raw para evitar JSON/base64 gigante no caminho rápido, anuncia a engine `android_native` quando o APK estiver pronto e usa cache normal do worker depois da primeira síntese e retorna tempos de cache/Android/worker para diagnóstico de latência.

O prefixo experimental `%texto` continua existindo, mas agora usa `android_native` por padrão via `TTS_PIPER_EXPERIMENT_ENGINE=android_native`. Piper não foi removido: ele fica como fallback legado se a variável for alterada para `piper` ou se uma chamada antiga ainda pedir essa engine. A ordem automática do TTS Agent passa a preferir `android_native`, depois `gcloud`, `edge`, `gtts` e, por último, `piper` quando houver modelo local.

Variáveis opcionais do worker: `PHONE_WORKER_ANDROID_TTS_ENABLED=true`, `PHONE_WORKER_ANDROID_TTS_URL=http://127.0.0.1:8877`, `PHONE_WORKER_ANDROID_TTS_STATUS_TIMEOUT_SECONDS=0.45`, `PHONE_WORKER_ANDROID_TTS_STATUS_CACHE_SECONDS=5` e `PHONE_WORKER_ANDROID_TTS_SYNTH_TIMEOUT_MS=4500`.

## Patch 85.10: Google Cloud OGG_OPUS + TTS direto controlado

A versão `1.10.21` mantém a VPS como cérebro do bot, mas melhora o caminho de voz: Google Cloud TTS na VPS passa a preferir `OGG_OPUS` para reduzir trabalho de reencode quando o destino é voz/Opus, com fallback automático para `MP3` se o formato não estiver disponível. O playback local da VPS tenta usar `FFmpegOpusAudio` para arquivos OGG/Opus antes de cair para PCM.

No TTS direto worker→Discord, o Music Agent sobe para `0.3.25` e passa a aceitar áudio prebuilt em OGG/Opus por `audio_b64`, usando a rota Opus quando possível. Para Google Cloud, a VPS pode pré-gerar OGG_OPUS e enviar ao worker, evitando que o worker caia para gTTS só porque não tem Google Cloud local. A transferência de posse continua controlada: o worker só toca voz quando a VPS libera a sessão, e o fallback VPS permanece obrigatório se algo falhar.

## Patch 85.9: TTS direto worker → Discord voice

A versão `1.10.20` ativa o primeiro caminho direto controlado de TTS pelo worker: quando a rota worker está saudável e não há música local ativa competindo, a VPS continua como cérebro/comandos/UI, mas libera a posse da voz e envia o pedido `voice_agent_play_tts` para o worker. O worker usa o Music Agent como plano de voz, conecta/toca o TTS direto no Discord e reporta o resultado de volta. Se falhar, a VPS usa o fallback normal.

Novas peças: task `voice_agent_play_tts` no phone worker e ação `voice_tts` no Music Agent (`0.3.24`). O caminho antigo de síntese worker→VPS→Discord continua como fallback; o worker não registra comandos, não mexe no banco e não vira o cérebro do bot.

## Patch 85.8: transferência controlada de posse da voz

A versão `1.10.19` mantém a arquitetura segura: a VPS segue como cérebro do bot e o worker controla apenas o plano de áudio/voz quando estiver autorizado. O Worker Voice Agent agora registra uma etapa intermediária de transferência (`voice_agent_prepare_transfer`, `voice_agent_begin_transfer`, `voice_agent_release_transfer` e `voice_agent_transfer_status`). O handoff continua chegando com dono `vps`; o worker só poderá usar a conexão de voz quando a VPS conceder explicitamente a posse para `worker`. O probe de conexão não ignora mais o dono atual da voz, mesmo em modo manual, evitando competição com a conexão `discord.py` da VPS.

Também foram adicionadas métricas para retries do TTS Agent quando o worker retorna ocupado/fila cheia, reduzindo quedas desnecessárias para fallback local/VPS em falhas transitórias.

## Patch 85.7: ownership de voz e probe seguro

A versão `1.10.18` mantém o Worker Voice Agent, o registro de sessão e o handoff temporário, mas corrige o dry-run para não competir com a conexão de voz real da VPS. O handoff agora carrega o dono atual da voz (`vps` por padrão) e o worker passa a tratar esse estado como `voice_handoff_received_waiting_transfer`: os dados temporários foram recebidos, mas a conexão direta só deve abrir quando houver transferência explícita de posse da voz para o worker.

`voice_agent_probe_connection` continua existindo para a etapa futura/manual de diagnóstico, mas não deve abrir Voice WebSocket/UDP automaticamente enquanto a VPS ainda está conectada/tocando/reconectando. A flag local nova `PHONE_WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED=false` documenta esse comportamento seguro. Na VPS, `WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED=false` mantém o probe automático desligado, sem reverter a arquitetura.

## Patch 85.6: conexão de voz dry-run do Worker Voice Agent

A versão `1.10.17` adiciona o primeiro teste controlado de conexão de voz do Worker Voice Agent. Depois que a VPS registra a sessão lógica e o handoff temporário, o worker pode iniciar `voice_agent_probe_connection`, abrir o Voice WebSocket do Discord com o `session_id`/endpoint/token temporário, esperar o `READY`, tentar o UDP discovery e fechar a conexão em seguida. Essa etapa **não toca áudio**, não recebe `DISCORD_TOKEN` geral e não transforma o worker no cérebro do bot; ela só valida que o plano de áudio do worker consegue iniciar a conexão de voz quando online.

Novas tasks diretas: `voice_agent_probe_connection`, `voice_agent_connection_status` e `voice_agent_clear_connection`. O painel `/vps` passa a exibir a linha de conexão dry-run com estado, etapa, WS ready, UDP e latência. A conexão é curta e fecha após o probe, preparando o próximo patch para manter a sessão viva e, depois disso, tocar TTS direto worker → Discord.

## Patch 85.5: handoff dry-run do Worker Voice Agent

A versão `1.10.16` adiciona o handoff temporário de voz em modo dry-run. A VPS continua como cérebro do bot, mas quando uma sessão de voz está ativa ela também envia ao worker os dados temporários necessários para a futura conexão de voz (`session_id`, endpoint e token temporário de voz). Esse handoff fica **somente em memória**, expira rápido e não é salvo no `voice-agent-state.json`. O `DISCORD_TOKEN` geral do bot continua sem ser enviado ao worker.

Novas tasks diretas: `voice_agent_register_handoff`, `voice_agent_clear_handoff` e `voice_agent_handoff_status`. O painel `/vps` agora mostra sessão lógica e handoff separadamente, com presença de session/endpoint/token temporário, sem expor valores crus. A transmissão direta worker → Discord ainda fica desligada por padrão; esta etapa só prova que o worker recebeu o material de voz certo para o próximo patch abrir Voice WebSocket/UDP de forma controlada.

## Patch 85.4: registro de sessão do Worker Voice Agent

A versão `1.10.15` passa a aceitar as tasks `voice_agent_register_session`, `voice_agent_clear_session` e `voice_agent_guild_status`. A VPS continua como cérebro do bot e apenas registra no worker, quando ele está saudável, qual guild/canal de voz está em uso. Esse registro é uma etapa segura antes do TTS direto worker → Discord: ele não envia `DISCORD_TOKEN` e não persiste o voice token bruto.

O `voice_agent` agora mostra `shared_session_ready`, quantidade de sessões, guilds ativas e uma prévia da última sessão registrada. Enquanto `PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED=false`, isso serve só para estado/handoff e painel; a reprodução continua no caminho seguro atual.

# Phone Worker Termux



## Patch 85.10: Google Cloud TTS nativo no worker turbo

A versão `1.10.22` integra Google Cloud TTS diretamente no `TTS Agent` do worker turbo confiável. Quando `google-cloud-texttospeech` está instalado e existe uma service account em runtime, o worker anuncia `gcloud` como engine pronta, usa `OGG_OPUS` por padrão e mostra no health se a biblioteca, credencial e encoding estão prontos. A ordem automática de engine passa a preferir `gcloud`, depois `piper`, `edge` e `gtts`.

A chave deve ficar apenas em `~/phone-worker/secrets/google-tts-service-account.json` ou caminho privado equivalente apontado por `PHONE_WORKER_GOOGLE_APPLICATION_CREDENTIALS`/`GOOGLE_APPLICATION_CREDENTIALS`. Não coloque o JSON no repositório, patch zip, logs ou GitHub. O painel/health mostra apenas presença da credencial, nunca o conteúdo.

No Termux, o caminho testado para dependências é: primeiro `pkg install python-grpcio python-cryptography protobuf libprotobuf`, depois `pip install google-cloud-texttospeech`. O start script passa a respeitar essa ordem quando Google Cloud TTS estiver habilitado ou a credencial existir, evitando compilar `grpcio`/`cryptography` pelo pip.

## Patch 85.3: base do Worker Voice Agent

A versão `1.10.14` também passa a expor `voice_agent` em `/health`, `/status` e `/local/status`, além da task direta `voice_agent_status`. Essa etapa **não** faz o worker controlar o bot inteiro e ainda não ativa TTS direto worker → Discord por padrão. Ela cria a base de telemetria/contrato para o próximo caminho: VPS continua sendo o cérebro do bot, enquanto o worker turbo vira o plano de voz/áudio compartilhado para Música + TTS quando estiver saudável.

O `voice_agent` junta o estado do `music_agent` e do `tts_agent`, mostra se a sessão compartilhada está preparada, se o ducking Música/TTS está possível, quais pendências ainda existem e se o TTS direto está apenas em preparação ou pronto. Variáveis locais do worker: `PHONE_WORKER_VOICE_AGENT_ENABLED`, `PHONE_WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED`, `PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED` e `PHONE_WORKER_VOICE_AGENT_DIRECT_MUSIC_ENABLED`. Na VPS existem flags espelhadas de roadmap (`WORKER_VOICE_AGENT_*`) para o painel/planejamento, mas a transmissão direta de TTS pelo worker continuará desativada até a etapa própria de voz.

## Patch 85.2: TTS Agent / rota worker

A versão `1.10.14` adiciona as tasks diretas `tts_agent_status` e `tts_agent_synthesize` e expõe `tts_agent` em `/health`, `/status` e `/local/status`. Quando o worker turbo está online, saudável e com alguma engine TTS pronta, a VPS pode trocar o modo do TTS para `worker` sem testar o celular a cada frase. Se o health falhar, se o agent ficar velho ou se a síntese falhar repetidamente, a VPS volta para o modo `vps` por cooldown e tenta recuperar pelo health loop.

O TTS Agent reutiliza as engines já conhecidas do worker: `piper` quando há modelo/config local, `edge` quando `edge-tts` está instalado, `gtts` quando disponível e `gcloud` apenas se `PHONE_WORKER_TTS_AGENT_GCLOUD_ENABLED=true`. O Piper continua usando o cache grande local do worker; a VPS ainda mantém seu cache próprio e consulta o cache remoto como segunda camada.

Textos longos passam a ser divididos em partes menores pela VPS. Isso permite começar a reprodução pelo primeiro bloco e usar o prefetch já existente para preparar o próximo bloco enquanto o áudio atual toca, sem esperar sintetizar a mensagem inteira antes de falar.

Variáveis principais da VPS: `TTS_WORKER_AGENT_ENABLED`, `TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS`, `TTS_WORKER_AGENT_STALE_SECONDS`, `TTS_WORKER_AGENT_FAILURE_THRESHOLD`, `TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS`, `TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS`, `TTS_WORKER_AGENT_PREFERRED_ENGINE`, `TTS_LONG_TEXT_CHUNK_ENABLED`, `TTS_LONG_TEXT_CHUNK_MAX_CHARS` e `TTS_LONG_TEXT_CHUNK_MAX_PARTS`. No worker, use `PHONE_WORKER_TTS_AGENT_ENABLED`, `PHONE_WORKER_TTS_AGENT_ENGINE`, `PHONE_WORKER_TTS_AGENT_CONCURRENCY`, `PHONE_WORKER_TTS_AGENT_TIMEOUT_SECONDS` e `PHONE_WORKER_TTS_AGENT_MAX_TEXT_LENGTH`.


## Patch 85.1: Piper Turbo Cache

A versão `1.9.0` mantém a task direta `tts_synthesize_piper`, restrita ao perfil `turbo` com `tts-synth`, mas muda o prefixo experimental do bot para `%texto`. O Piper agora tem cache extra grande no worker turbo e cache separado/maior na VPS: a primeira síntese pode continuar lenta, mas repetições devem responder pelo caminho de cache.

O benchmark `.teste` agora mede Piper em duas fases: cache miss/geração e cache hit. O resumo deve diferenciar “Piper funcional, mas lento ao gerar” de “Piper cacheado é rápido”.

Piper/modelos continuam locais no celular: configure `PHONE_WORKER_PIPER_COMMAND`, `PHONE_WORKER_PIPER_MODEL` e, se necessário, `PHONE_WORKER_PIPER_CONFIG` no `~/.phone-worker.env`. Não coloque `.onnx`, `.json` de modelo, service account ou segredos no repositório. No perfil `turbo`, o `start-phone-worker.sh` pode instalar dependências faltantes de forma segura (`PHONE_WORKER_TURBO_DEPS_INSTALL=auto`) e baixar o modelo padrão se `PHONE_WORKER_PIPER_MODEL_AUTO_DOWNLOAD=true`. Para cache local grande, ajuste `PHONE_WORKER_PIPER_CACHE_MAX_MB`, `PHONE_WORKER_PIPER_CACHE_MAX_FILES`, `PHONE_WORKER_TTS_CACHE_MAX_MB` e `PHONE_WORKER_TTS_CACHE_MAX_FILES`. A VPS pode consultar esse cache TTS genérico do worker como segunda camada, sem depender dele para funcionar. A VPS também mantém um índice negativo curto de miss/erro para não consultar o celular repetidamente por chaves que acabaram de falhar; ajuste `TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS`, `TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS` e `TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES` no ambiente da VPS se precisar.

## Patch 84.7: benchmark TTS turbo

A versão `1.8.9` adiciona a task direta `tts_synthesize_benchmark`, restrita ao perfil `turbo` com as capacidades `tts-synth` e `tts-benchmark`. Ela sintetiza áudio curto por `edge`, `gtts` ou `gcloud` e devolve o MP3 para a VPS medir o tempo total real do worker, incluindo ida, síntese, retorno e validação. O worker não vira dependência do TTS: se estiver offline, lento, sem dependência ou sem credencial Google local, a VPS continua sintetizando sozinha e o relatório informa a falha curta.

## Patch 84.6: resultados antigos, republicação e allowlist multi-worker

A versão `1.8.8` fecha a limpeza de resultados pendentes antigos: quando a VPS responde que um job já não existe, o agent arquiva localmente o resultado e para de reenviar em loop. O registry também passa a aceitar `apk_publish_last` como tipo de job válido, permitindo republicar o último APK salvo em `~/core-worker-apk-builds/artifacts/` sem recompilar. Esse ajuste mantém o fluxo multi-worker: resultados ficam ligados ao worker/job quando possível, e resultados órfãos são registrados como descartados em vez de travarem heartbeat/poll.

# Phone Worker

## Patch 84.5: bootstrap direto e republicação sem rebuild

A versão `1.8.7` reforça o fluxo multi-worker direto: a VPS pode recuperar um phone-worker direto confiável por host configurado quando o token antigo ainda está salvo no Termux, evitando o ciclo de `worker não encontrado` em heartbeat/poll/result/publish. O builder também passa a registrar metadados do APK persistente e expõe `apk_publish_last`, permitindo republicar o último APK gerado sem recompilar quando só a publicação falhou.

## Patch 84.4: publicação do APK e multi-worker direto

A versão `1.8.6` fecha o ciclo pós-build do APK: o APK gerado é copiado para `~/core-worker-apk-builds/artifacts/` antes de qualquer limpeza do workspace, o resultado separa build/artefato/publicação, e a VPS pode registrar automaticamente um phone-worker direto confiável quando ele usa o token local configurado. Isso evita o erro repetido `worker não encontrado` para heartbeat/poll/result/publish do builder direto e prepara o fluxo para vários workers com identidades estáveis.

O build Android também evita tentar stripar `.so` com o `llvm-strip` x86_64 do NDK dentro do Termux, mantendo as bibliotecas nativas prebuilt empacotadas sem ruído.

## Patch 84.3: hotfix de atualização do agent e painel

A versão `1.8.5` reforça o fluxo de atualização do phone-worker e evita que o painel/automação continuem usando um agent antigo depois de patches do APK. O painel da VPS agora mantém ações essenciais visíveis para o phone-worker direto, esconde o botão de acordar quando já existe worker online e deixa o build manual disponível mesmo quando ainda não há celular pareado pelo APK.

## Patch 84.2: build APK com executor nativo prebuilt

A versão `1.8.4` ajusta o builder Android para o Patch 84.2:

- o executor nativo mínimo do APK é empacotado por `app/src/main/jniLibs/arm64-v8a/libcoreworker_executor.so`;
- o phone worker não tenta mais tratar a existência de `src/main/cpp/CMakeLists.txt` como obrigação de CMake/NDK quando `externalNativeBuild` não está ativo;
- falhas de Gradle passam a resumir melhor a causa principal no resultado do job;
- o enfileiramento automático do mesmo APK/source recebe cooldown para evitar loop de builds repetidos.

Isso evita o erro do CMake do Android SDK dentro do Termux/Android ARM64 (`Syntax error: ")" unexpected`) sem voltar a buildar APK na VPS Oracle.

## v1.8.3 — hotfix build APK sem loop

A versão `1.8.3` estabiliza o builder depois da entrada do NDK/CMake: agora o `apk_build_debug` usa lock local/cross-process para impedir dois Gradle ao mesmo tempo, grava o log persistente em `~/core-worker-apk-builds/logs/`, devolve `gradle_log_tail` no resultado do job e mantém metadados de versão/source mesmo quando o build falha. Isso evita retry automático cego e deixa o painel mostrar o erro real antes de tentar outro build.

## v1.8.2 — build nativo APK/NDK

A versão `1.8.2` adiciona diagnóstico explícito de NDK/CMake para o build do Core Worker quando o app passa a usar `externalNativeBuild`. O worker continua sendo o único ambiente que compila APK; a VPS apenas orquestra, publica e notifica.

## v1.8.1 — assinatura compatível do APK

A versão `1.8.1` alinha o agent ao Patch 57. O phone worker continua compilando o APK, mas agora recebe uma keystore compatível pelo payload autenticado do job e assina o APK no workspace temporário. Isso evita o erro do Android de conflito com pacote existente ao atualizar por cima.

Regras de segurança:

- a keystore não vem no ZIP público;
- a keystore não vai para GitHub;
- a keystore é apagada junto com o workspace temporário depois do build;
- o phone worker não recebe a service account do Firebase;
- se a keystore não vier no payload, o build falha em vez de gerar APK com assinatura incompatível.

Worker opcional para usar o celular como ajudante da VPS em tarefas que não são críticas.

Ele **não substitui a VPS**. Se o celular cair, a VPS continua funcionando e usa fallback local.

## v1.7.8 — autostart ao abrir Termux e sshd auto-heal

A versão `1.7.8` alinha o agent ao Patch 47. Além do Termux:Boot, o update/boot_repair agora instala um bloco gerenciado em `~/.bashrc` e `~/.profile` para disparar o watchdog silenciosamente quando o Termux é aberto. Isso corrige o caso em que o usuário abre o Termux e cai só na tela inicial sem `watch-phone-worker.sh`, `phone_worker.py` ou `sshd` rodando.

O watchdog também tenta iniciar `sshd` automaticamente quando o binário existe e a porta configurada não está ouvindo. O heartbeat passa a reportar `shell_autostart` para o painel/APK diferenciar: VPN ok, Termux aberto, mas canal local parado.

## v1.7.7 — diagnóstico de wake e canal SSH/HTTP real

A versão `1.7.7` alinha o agent ao Patch 46. O heartbeat agora informa melhor o canal de wake: watchdog local, `sshd`, porta configurada e resumo do SSH no Termux. A VPS passa a diferenciar `porta worker fechada`, `SSHD parado`, `sem rota`, `timeout`, `token errado` e `SSH/auth falhou`, em vez de mostrar apenas “SSH falhou”.

O botão **Acordar phone-worker** usa o watchdog oficial quando consegue entrar por SSH e registra probes HTTP/SSH redigidos no painel. Se o Android matar Termux/SSHD, o painel deve explicar que o canal remoto está indisponível e que o watchdog/local/APK precisam manter o worker vivo.

## v1.7.6 — comunicação confiável e auto-update garantido

A versão `1.7.6` alinha o agent ao Patch 45. O worker continua usando watchdog local, mas a VPS agora também reavalia mismatch de versão em cada heartbeat/poll e agenda `worker_update` mesmo se a pendência antiga tiver sumido. O agent mantém resultado pendente em disco e reenvia quando a rota/VPN voltar, evitando jobs invisíveis.

O `worker_update` aplica os arquivos whitelisted em `~/phone-worker`, repara o boot para apontar ao watchdog, persiste resultados pendentes em disco e reinicia mesmo se a rota para a VPS cair antes da confirmação. Ao reconectar, o worker reenvia o resultado pendente e o painel consegue mostrar versão atual vs. versão esperada. Diretórios duplicados como `~/phone-worker-install` são reportados com caminho exato; se estiverem inativos, não bloqueiam o estado principal do worker.

## v1.7.3 — pipeline automático, rede e boot mais confiáveis

A versão `1.7.3` mantém o agent alinhado ao Patch 42: o worker informa estado de rede/rota até a VPS, detecta instalações duplicadas no Termux, usa boot oficial em `~/phone-worker` e envia metadados de build/notificação para a VPS validar APK, `latest.json` e entrega de atualização no app.

Na VPS, o painel/loop de Core Workers usa `scripts/phone-worker-watch.sh` com confirmação real: código 0 do script não é tratado como “acordou”; o painel só mostra sucesso quando o worker volta a responder pelo registry/health.

## v1.7.1 — resultados úteis e limpeza de jobs

A versão `1.7.1` melhora os resultados enviados ao painel `workers`: `maintenance_plan` agora devolve resumo, bytes recuperáveis estimados e sugestões seguras; `boot_status` detalha script, permissão, conteúdo e Termux:Boot; a matriz de jobs continua alinhada aos perfis `builder`/`turbo`; e o agent mantém estado local do último job para reenviar resultado se a VPS oscilar, evitando jobs presos como `running`.

O painel Discord deve editar uma única mensagem ephemeral por fluxo e usar **Ver último resultado** para mostrar dados completos, não apenas tipo/status.

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
nohup bash ~/phone-worker/watch-phone-worker.sh >> ~/phone-worker/phone-worker-watch.log 2>&1 &
```

O `install.sh`, `bootstrap-phone-worker.sh` e o job `boot_repair` criam `~/.termux/boot/10-core-worker` apontando para esse watchdog. Não edite scripts manualmente; aplique sempre patches pelo fluxo da VPS/GitHub.

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

O timer da VPS chama `scripts/phone-worker-watch.sh` para manter o worker acordado quando possível. O bot também tem um loop de auto-wake seguro para workers offline com responsabilidades importantes. Por padrão ele tenta a cada 60 segundos e não para por causa de falha anterior.

```env
CORE_WORKER_AUTO_WAKE_ENABLED=true
CORE_WORKER_AUTO_WAKE_INTERVAL_SECONDS=60
CORE_WORKER_WAKE_CONFIRM_SECONDS=8
# O botão manual ignora cooldown; o timer/loop automático respeita este valor.
PHONE_WORKER_KICK_COOLDOWN_SECONDS=60
```

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

Além dos diagnósticos, a VPS pode usar o phone-worker para preparar áudio curto de TTS e alimentar o fluxo direto do Music Agent/FFmpeg. Lavalink/NodeLink não fazem mais parte do worker. Isso é opcional e tem fallback local automático.

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

O script chama `POST /core-worker/pair`, salva `CORE_WORKER_ID`, `CORE_WORKER_TOKEN`, `CORE_WORKER_VPS_URL`, ativa heartbeat/jobs em `~/.phone-worker.env` e nunca imprime o token. Depois do pareamento, inicie o watchdog oficial:

```bash
nohup bash ~/phone-worker/watch-phone-worker.sh >> ~/phone-worker/phone-worker-watch.log 2>&1 &
```

Teste manual sem iniciar servidor novo:

```bash
cd ~/phone-worker
python phone_worker.py --heartbeat-once
python phone_worker.py --jobs-once
```

O heartbeat envia status, bateria real via Termux:API quando disponível, ping TCP até a VPS, rede, ffmpeg/ffprobe, disco, saúde básica e um resumo do Tailscale quando a CLI existir. Com jobs habilitados, o worker consulta a VPS por polling e executa somente jobs whitelisted (`ping`, `status`, `diagnostic_basic`, `worker_self_check`, `worker_logs`, `network_probe`, `tailscale_status`, `service_status`, `service_start`, `service_stop`, `service_restart`, `ffmpeg_check`, `ffprobe_check`, `worker_update`, `boot_status`, `boot_repair`, `zip_validate`, `zip_audit`, `log_summary`, `log_digest`, `text_stats`, `maintenance_plan`, `vps_assist_probe`, `hash_batch`, `endpoint_probe`, `media_probe`, `audio_convert`, `apk_build_debug`). Não existe execução de shell livre pelo registry. O token fica só no `~/.phone-worker.env`; o registry da VPS guarda apenas hash.


## Supervisor local e anti-duplicação

O `watch-phone-worker.sh` é o supervisor persistente local e chama `start-phone-worker.sh` para manter o agent vivo:

- usa lock para evitar duas inicializações ao mesmo tempo;
- mata processos antigos/duplicados de `phone_worker.py` antes de iniciar;
- grava PID em `~/phone-worker/phone-worker.pid`;
- grava status curto em `~/phone-worker/phone-worker.status`;
- rotaciona logs quando passam de `PHONE_WORKER_LOG_MAX_BYTES`;
- `start-phone-worker.sh` inicia com `nohup` sem depender de `tmux`;
- se o arquivo `phone_worker.py` no disco estiver em versão mais nova que o processo vivo, o start força restart para aplicar o update;
- o watchdog segura wake-lock, tem lock/pid próprio e tenta novamente a cada intervalo configurado, mesmo quando houver falha.

Variáveis úteis no `~/.phone-worker.env`:

```env
PHONE_WORKER_LOG_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker.log
PHONE_WORKER_PID_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker.pid
PHONE_WORKER_STATUS_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker.status
PHONE_WORKER_LOG_MAX_BYTES=1048576
PHONE_WORKER_START_KILL_DUPLICATES=true
PHONE_WORKER_WATCH_LOCK_DIR=/data/data/com.termux/files/home/phone-worker/.phone-worker-watch.lock
PHONE_WORKER_WATCH_PID_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker-watch.pid
PHONE_WORKER_PENDING_RESULTS_FILE=/data/data/com.termux/files/home/phone-worker/phone-worker-pending-results.json
PHONE_WORKER_WATCH_MAX_BACKOFF_SECONDS=60
```

No painel `workers`, a ação **Status serviços** mostra PID, duplicados, runtime e logs. Se aparecer `runtime atenção`, use **Manutenção → Reiniciar worker** ou **Manutenção → Reparar scripts**.

## Boot automático pós-reboot

O `install.sh`, o `bootstrap-phone-worker.sh`, o sync da VPS e a ação **Reparar boot automático** criam/reparam:

```bash
~/.termux/boot/10-core-worker
```

Esse script é lido pelo app **Termux:Boot** quando o Android inicia. Ele espera alguns segundos, segura wake-lock quando possível e chama `~/phone-worker/watch-phone-worker.sh`. Boot que chama apenas `start-phone-worker.sh` é considerado incompleto.

Depois de instalar/reparar, abra o app **Termux:Boot** uma vez e, no Android/MIUI, libere inicialização automática e bateria sem restrição para:

- Termux
- Termux:Boot
- Termux:API
- Tailscale

O painel `workers` mostra `boot ok`, `boot faltando` ou `boot incompleto`. Se aparecer faltando/incompleto, use **Manutenção → Reparar boot automático**.

## Controle seguro de serviços

O painel `workers` agora consegue criar jobs para serviços whitelisted do celular:

- `phone-worker`: status, start, stop e restart do agente atual. Para `stop`/`restart`, o worker responde primeiro à VPS e só depois agenda a ação para não deixar o job preso.
- `phone-worker-watch`: start, stop, restart e status do watchdog local persistente, usando pid/lock próprio e `tmux` apenas como compatibilidade quando existir.
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
- `builder`: `phone-worker, diagnostics, log-summary, maintenance-plan, apk-builder, zip-validate, vps-assist, cache-worker`
- `turbo`: mídia + manutenção + APK builder + assistência máxima para acelerar a VPS
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

Na versão `1.8.0`, o worker pode continuar usando o perfil `builder`/`apk-builder` para compilar o Core Worker fora da VPS. A VPS envia o `google-services.json` local apenas no payload autenticado do job; ele é gravado no workspace temporário de build e não vai para GitHub nem para o ZIP público. O worker compila e envia o APK debug já assinado pelo Gradle para a VPS, que só valida e publica. A service account do Firebase fica somente na VPS e nunca deve ir para o Termux.

O builder em Termux tende a funcionar melhor com Android SDK 34 e `aapt2` do próprio Termux. Se o build falhar com `aapt2` ou `android.jar`, prepare o ambiente com SDK 34 e mantenha `android.aapt2FromMavenOverride` apontando para `/data/data/com.termux/files/usr/bin/aapt2`.

## Worker Assist / aceleração da VPS

A partir do phone-worker 1.6.7/1.7.x, o celular pode ajudar a VPS de forma oportunista, sem virar dependência obrigatória do bot.

Novas capacidades seguras:

- `vps-assist`: permite que a VPS envie tarefas auxiliares quando o worker estiver online.
- `hash-worker`: cálculo de hashes em lote.
- `endpoint-probe`: teste de endpoints da VPS visto do celular.
- `media-probe`: análise de mídia com `ffprobe` quando disponível.
- `audio-convert`: conversão curta com `ffmpeg` quando disponível.
- `cache-worker`: preparação/validação de pacotes e caches.

Novos jobs permitidos:

- `vps_assist_probe`
- `hash_batch`
- `endpoint_probe`
- `media_probe`
- `audio_convert`
- `log_digest`
- `zip_audit`
- `maintenance_plan`
- `apk_build_debug` quando o worker tem `apk-builder`

A VPS continua sendo o cérebro. Se nenhum worker estiver online, a VPS deve usar fallback local. Não existe shell livre nem execução arbitrária; apenas jobs whitelist.

Perfil novo opcional:

- `turbo`: modo forte para celular confiável, combinando mídia, builder e auxílio à VPS.

