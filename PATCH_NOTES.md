# Patch: Core Linux reporting + startup stability

Base: repo-20260602-182047.zip

## Objetivo
Fechar a camada de contrato/relatório após o smoke test do Core Linux Runtime v1 passar sem Termux.

## Mudanças principais
- Bump APK para 0.5.56 / versionCode 71.
- APK passa a enviar `supported_tasks`, `supportedTasks`, `app_jobs` e `capabilities` também no heartbeat direto `/core-worker/app/heartbeat`.
- Heartbeat em background (`CoreWorkerUpdateJobService`) também declara as tarefas seguras reais do APK, em vez de mandar lista vazia.
- VPS passa a persistir capacidades, tarefas suportadas, `runtime`, `coreLinux` e `nativeRuntime` no arquivo `data/core_worker_app_heartbeats.json`.
- `/core-worker/app/runtime-summary` agora, quando chamado sem worker/install explícito, pega o heartbeat mais recente em vez de retornar `unknown`.
- `/core-worker/app/jobs/fetch` aceita `supportedJobs`, `supported_tasks`, `supportedTasks` e `app_jobs`.
- Painel workers passa a reconhecer `supportedTasks`/`appJobs` além de `supported_tasks`.
- Startup/resume do APK recebeu debounce de 15s para evitar rajadas de `safeStartupTask` e requests simultâneos.
- Heartbeat interno, heartbeat nativo e fetch de jobs agora têm guarda contra execução concorrente.

## Segurança preservada
- Não inicia Bedrock real.
- Não libera shell livre.
- Não baixa rootfs automaticamente.
- Não remove Termux ainda; Termux segue como fallback legado.
- Não toca CallKeeper.

## Validação feita aqui
- `python3 -m py_compile webserver.py utility/commands/workers.py`
- Conferência de balanceamento de chaves Java em `MainActivity.java` e `CoreWorkerUpdateJobService.java`.

## Observação
Não rodei build Android real nesta sandbox porque a base não inclui `gradlew`; o build deve continuar sendo feito pelo phone worker/builder.
