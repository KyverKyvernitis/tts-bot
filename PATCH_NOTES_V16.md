# Patch V16 — Rootfs glibc intake/preflight

Objetivo: preparar a etapa de rootfs Linux ARM64 com glibc para o próximo smoke real do Box64, sem executar Box64, sem iniciar Bedrock e sem abrir shell livre.

## Mudanças

- APK `0.5.98` / `113`.
- Novo stage seguro: `core-linux-rootfs-glibc-intake-preflight-v16`.
- Novo job manual seguro: `apk_core_linux_rootfs_glibc_preflight`.
- `CoreLinuxRootfsImportManager` passa a validar runtime glibc ARM64:
  - `/lib/ld-linux-aarch64.so.1`
  - `libc.so.6`
  - `libm.so.6`
  - `libresolv.so.2`
- `validateActive` só marca rootfs pronto para Box64 quando a glibc está presente.
- O preflight V16 não executa binários do rootfs, não toca Box64 e não inicia Bedrock.

## Segurança

- Sem shell livre.
- Sem comando arbitrário vindo da VPS.
- Sem download automático.
- Sem executar binários importados.
- Sem Box64 `--version` ainda.
- Sem Bedrock.

## Próximo passo esperado

Depois de importar um rootfs Linux ARM64 com glibc e validar o V16, o próximo patch pode liberar um smoke Box64 controlado (`box64 --version` / `box64 --help`).
