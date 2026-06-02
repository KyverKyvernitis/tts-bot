# patch-vps-core-worker-backpressure-json-io-20260602

Base: `repo-20260602-191433.zip`

## Objetivo

Reduzir travamentos da VPS durante rajadas do APK/Core Worker antes de avançar para rootfs real. O Core Linux Runtime v1 já estava funcional; este patch estabiliza o caminho de telemetria/jobs para evitar `event loop atrasado`, `heartbeat blocked` e filas Waitress causadas por I/O JSON síncrono repetitivo.

## Alterações principais

- `webserver.py`
  - adiciona cache local por mtime/tamanho para JSONs de telemetria do APK;
  - grava JSON compacto, sem `indent/sort_keys` e sem `fsync` obrigatório;
  - mantém `fsync` opt-in via `CORE_WORKER_JSON_FSYNC=1`;
  - reduz limites padrão de histórico:
    - heartbeats: 60 eventos;
    - notifications: 60 eventos;
    - jobs results: 120 resultados;
    - pending jobs: 80;
  - reduz `CORE_WORKER_APP_JOB_MAX_DELIVER` padrão de 6 para 2;
  - adiciona throttle em `/core-worker/app/jobs/fetch` para evitar rajadas vazias;
  - evita consultar o histórico de jobs em todo heartbeat quando o APK já envia estado Core Linux completo;
  - evita armazenar heartbeats repetidos do mesmo source em janela curta;
  - expõe `source` no runtime-summary;
  - normaliza `jobsRuntime` legado com `bedrock-installe` para texto neutro `apk-native-runtime` no summary.

- `CoreWorkerRuntimeService.java`
  - heartbeat foreground passa de 25s mínimo para 60s;
  - tick do serviço persistente passa de 60s para 120s;
  - start repetido do foreground service não fura mais o debounce, exceto ação manual.

- `MainActivity.java`
  - debounce de `onResume` passa de 15s para 60s;
  - fetch de jobs internos não manual recebe debounce local de 25s;
  - APK trata resposta `throttled` da VPS sem considerar falha.

- APK bump: `0.5.58` / `versionCode 73`.

## Segurança/escopo

- Não toca CallKeeper.
- Não inicia Bedrock real.
- Não adiciona rootfs real ainda.
- Não libera shell livre.
- Não remove Termux de uma vez; Termux continua fallback legado enquanto a migração prossegue.

## Validação local

- `python3 -m py_compile webserver.py utility/commands/workers.py`
- checagem simples de balanceamento de chaves/parênteses Java em `MainActivity.java` e `CoreWorkerRuntimeService.java`.

Build Android real não foi executado aqui porque continua sendo feito pelo phone worker/builder.
