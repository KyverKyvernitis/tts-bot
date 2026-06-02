# patch-core-linux-heartbeat-source-unification-20260602

Base: `repo-20260602-184425.zip`

## Objetivo

Corrigir o estado cego do Core Linux quando o heartbeat mais recente vem do `CoreWorkerRuntimeService`/foreground service. O Core Linux v1 já passava no smoke test sem Termux, mas o foreground service publicava `supported_tasks`, `supportedTasks`, `capabilities` e `coreLinux*` vazios, fazendo a VPS/painel enxergarem o runtime como incompleto.

## Mudanças

- Bump do APK para `0.5.57` / `versionCode 72`.
- `CoreWorkerRuntimeService` agora monta heartbeat rico com:
  - `supported_tasks`, `supportedTasks` e `app_jobs`;
  - `capabilities`;
  - `runtime` com estado do foreground service;
  - `coreLinux` lido dos snapshots persistidos em `files/core-linux/runtime`;
  - `nativeRuntime` lido do snapshot do executor nativo.
- `CoreWorkerRuntimeService` agora limita heartbeats concorrentes e aplica debounce leve para reduzir rajadas.
- `webserver.py` agora tem fallback canônico para APK Core Linux v1 quando um heartbeat chega incompleto.
- `webserver.py` preserva/mescla último estado Core Linux válido no heartbeat mais recente.
- `/core-worker/app/runtime-summary` agora pode enriquecer o resumo usando o último resultado Core Linux válido dos jobs internos, evitando `Core Linux vazio` quando o heartbeat foreground-service for incompleto.
- `MainActivity.safeStartupTask` parou de logar `start/ok` de todas as tarefas rápidas; agora loga falhas, tarefas lentas e alguns fluxos úteis, reduzindo spam de `safeStartupTask`.

## Mantido bloqueado

- Bedrock real não inicia.
- Box64 real não inicia.
- Shell livre remoto continua bloqueado.
- Termux continua apenas como fallback legado; não foi removido neste patch.
- CallKeeper não foi tocado.

## Validação local

- `python3 -m py_compile webserver.py utility/commands/workers.py`
- Checagem simples de balanceamento de chaves/parênteses nos arquivos Java alterados.

O build Android real deve continuar sendo feito pelo phone worker/builder.
