# Patch: VPS worker automation / zip_validate lag fix

Base: `repo-20260603-123310.zip`

Objetivo: reduzir as fontes de lag que sobraram depois do Core Linux Runtime v1 estar pronto, sem mexer em CallKeeper, música, TTS, Bedrock real ou rootfs real.

## Mudanças

- APK: bump para `0.5.59` / `versionCode 74`.
- `webserver.py`:
  - adiciona snapshot compacto `data/core_worker_app_runtime_snapshot.json`;
  - passa a persistir heartbeats do APK em formato compacto, sem guardar runtime/status inteiro em `latestByInstallId/latestByWorkerId/events`;
  - mantém `supported_tasks`, `capabilities`, `coreLinuxState`, `coreLinuxSummary` e campos usados pelo painel/runtime-summary;
  - `/core-worker/app/runtime-summary` passa a preferir o snapshot compacto quando nenhum worker/install específico é pedido;
  - logs repetidos de `core-worker automation skipped` agora têm janela maior de silenciamento;
  - `process-pending` não é spawnado de novo se não há pendência explícita e o último scan terminou dentro do cooldown persistido.
- `bot.py`:
  - `zip_validate` via phone-worker passa a falhar rápido quando o phone-worker está indisponível;
  - adiciona cooldown por task para não repetir timeout a cada ZIP;
  - timeout padrão de `zip_validate` cai para 1.5s, com limite máximo de 5s.
- `utility/commands/workers.py`:
  - auto-wake legado agora usa intervalo padrão maior;
  - auto-wake pula tentativa de acordar Termux quando o APK interno/Core Linux já está online e recente.
- `CoreWorkerRuntimeService.java`:
  - reduz frequência do heartbeat/tick do foreground service.

## Validação local

- `python3 -m py_compile webserver.py bot.py utility/commands/workers.py`
- checagem simples de balanceamento de `{}` e `()` em `CoreWorkerRuntimeService.java` e `MainActivity.java`

## Fora do escopo

- Não altera CallKeeper.
- Não inicia Bedrock real.
- Não implementa rootfs real ainda.
- Não altera o smoke test Core Linux já aprovado.
