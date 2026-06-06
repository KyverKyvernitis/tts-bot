# Patch V15 — Box64 version smoke controlado

## Objetivo

Preparar o primeiro smoke real do Box64 sem iniciar Bedrock e sem abrir shell livre.

## Segurança mantida

- Não inicia Bedrock.
- Não inicia servidor persistente.
- Não aceita comando remoto arbitrário.
- Não executa binário x86_64 do usuário.
- Não baixa nada em runtime.
- Só permite `box64 --version` e `box64 --help` via allowlist fixa.

## Mudanças principais

- APK `0.5.93` / `108`.
- Novo stage: `core-linux-box64-version-smoke-v15`.
- Novo job seguro: `apk_core_linux_box64_smoke_test`.
- Extrai `assets/core-linux/bin/box64` para `files/core-linux/bin/box64`.
- Aplica `chmod` controlado no binário extraído.
- Revalida `sha256`, tamanho, ELF64 e AArch64 após extração.
- Antes de executar, verifica runtime glibc no rootfs:
  - `/lib/ld-linux-aarch64.so.1`
  - `libc.so.6`
  - `libm.so.6`
  - `libresolv.so.2`
- Se o runtime glibc estiver ausente, retorna `box64_smoke_blocked_missing_glibc_runtime`.

## Resultado esperado em rootfs mínimo

Se o rootfs ainda for o rootfs de teste sem glibc, o resultado correto é bloqueio limpo:

```text
state=box64_smoke_blocked_missing_glibc_runtime
```

## Resultado esperado em rootfs Linux arm64 com glibc

```text
state=box64_smoke_ok
box64 --version ok
box64 --help ok
```
