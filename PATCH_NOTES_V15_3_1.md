# Patch V15.3.1 — Box64 glibc hard guard sem helper pesado

## Objetivo

Corrigir o OOM persistente do `apk_core_linux_box64_smoke_test` no APK 0.5.96 / 111.

Os testes V15.1, V15.2 e V15.3 ainda caíam em `OutOfMemoryError` tentando alocar cerca de 301 MB antes de produzir `stage/state` útil. O diagnóstico mostrou que ainda havia caminho passando por helpers genéricos de Box64/runtime.

## Mudanças

- APK sobe para `versionName 0.5.97` e `versionCode 112`.
- Stage novo: `core-linux-box64-glibc-preflight-v15.3.1`.
- O handler `apk_core_linux_box64_smoke_test` em `MainActivity` agora tem hard guard local:
  - não chama `CoreLinuxRuntimeManager.box64VersionSmokeTest`;
  - não chama `coreLinuxNativeExecutorSnapshot`;
  - não chama `runtimeSnapshot`;
  - não chama `box64IntakePreflight`;
  - não abre asset `core-linux/bin/box64`;
  - não extrai Box64;
  - não calcula SHA256 do Box64.
- A etapa faz somente `File.exists()` em caminhos pequenos do rootfs:
  - `/lib/ld-linux-aarch64.so.1`
  - `libc.so.6`
  - `libm.so.6`
  - `libresolv.so.2`
- O JSON de retorno é curto e explícito.

## Resultado esperado

No rootfs atual, sem glibc arm64 completo, o esperado é:

```text
stage=core-linux-box64-glibc-preflight-v15.3.1
state=box64_smoke_blocked_missing_glibc_runtime
sem OutOfMemoryError
```

## Ainda bloqueado

- Box64 não é executado.
- Bedrock não é iniciado.
- Shell livre continua bloqueado.
- Comando arbitrário continua bloqueado.
- Binário x86_64 do usuário continua bloqueado.
