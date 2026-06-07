# Patch V15.2 — Box64 smoke sem OOM

Objetivo: corrigir o OOM do V15.1 no job `apk_core_linux_box64_smoke_test`.

Mudanças principais:
- APK 0.5.95 / 110.
- Stage `core-linux-box64-version-smoke-v15.2`.
- O smoke checa o runtime glibc do rootfs antes de tocar o asset Box64 pesado.
- Se glibc ainda estiver ausente, retorna `box64_smoke_blocked_missing_glibc_runtime` sem extrair/copiar o Box64.
- Quando glibc estiver presente, a extração do Box64 usa `openFd`/streaming e buffer fixo.
- Gradle marca `box64` como `noCompress` para evitar alocação grande do AssetManager.
- Continua sem Bedrock, sem shell livre, sem comando arbitrário e sem binário x86_64 do usuário.

Este patch não inclui binários pesados.
