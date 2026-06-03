# Patch: core-worker autowake APK online final

Base: `repo-20260603-130337.zip`

## Objetivo

Parar o auto-wake legado do Termux/phone-worker quando o APK/Core Linux interno já está online e pronto.

## Alterações

- `utility/commands/workers.py`
  - `_core_worker_app_runtime_record()` passa a preferir `data/core_worker_app_runtime_snapshot.json` antes do heartbeat grande.
  - `_core_worker_any_apk_runtime_online()` agora lê o snapshot compacto e usa o heartbeat só como fallback.
  - A checagem de APK online valida idade, versão mínima, origem APK, `coreLinuxPrepared`, capabilities e estado `runtime_v1_ready`.
  - O loop `_core_worker_auto_wake_loop()` faz um caminho rápido antes de coletar snapshot pesado.
  - Quando o APK/Core Linux está online, o loop pula o wake e só registra um log resumido em intervalo controlado.

## Variáveis opcionais

- `CORE_WORKER_AUTO_WAKE_APK_ONLINE_MAX_AGE_SECONDS` padrão: `300`
- `CORE_WORKER_AUTO_WAKE_APK_MIN_VERSION_CODE` padrão: `74`
- `CORE_WORKER_AUTO_WAKE_APK_SKIP_LOG_INTERVAL_SECONDS` padrão: `900`

## Segurança/escopo

- Não altera Core Linux funcional.
- Não altera APK.
- Não altera CallKeeper.
- Não altera música/TTS.
- Não inicia rootfs real, Box64 ou Bedrock.
