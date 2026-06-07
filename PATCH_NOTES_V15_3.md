# Patch V15.3 — Box64 glibc preflight isolado

- APK `0.5.96` / `111`.
- Substitui o caminho do job `apk_core_linux_box64_smoke_test` por uma fase leve: `core-linux-box64-glibc-preflight-v15.3`.
- A fase V15.3 **não abre** `assets/core-linux/bin/box64`, não extrai Box64, não calcula SHA do Box64 e não executa `box64 --version`.
- Verifica apenas arquivos pequenos do rootfs necessários ao runtime glibc arm64:
  - `/lib/ld-linux-aarch64.so.1`
  - `libc.so.6`
  - `libm.so.6`
  - `libresolv.so.2`
- Resultado esperado no rootfs mínimo atual: `box64_smoke_blocked_missing_glibc_runtime`, sem `OutOfMemoryError`.
- Bedrock, shell livre, comando arbitrário e binário x86_64 de usuário continuam bloqueados.
