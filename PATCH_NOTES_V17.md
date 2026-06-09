# Patch V17 — Core Linux rootfs ARM64/glibc staging import

- APK `0.6.1` / `116`.
- Formaliza o estágio `core-linux-rootfs-staging-import-v17` para importação assistida de rootfs Linux ARM64 com glibc.
- Mantém o import pelo seletor do APK: o usuário escolhe o arquivo localmente; a VPS não envia rootfs e não há download automático.
- A importação continua em staging: calcula SHA-256, extrai no staging, valida layout/glibc e só promove para rootfs ativo se passar.
- Estados de sucesso agora apontam para `rootfs_glibc_ready_for_box64`, deixando claro que o próximo passo permitido é preparar o smoke do Box64, não Bedrock.
- Adiciona telemetria/stage V17 em progresso, falhas, abort, validate e manifesto do rootfs.
- Expõe jobs manuais seguros de status/validate/abort do import rootfs pelo endpoint local `/core-worker/app/jobs/enqueue`:
  - `apk_core_linux_rootfs_import_status`
  - `apk_core_linux_rootfs_import_validate`
  - `apk_core_linux_rootfs_import_abort`
  - `apk_core_linux_rootfs_real_status`
- Os jobs V17 não carregam arquivo rootfs, não executam binários importados, não iniciam Box64/Bedrock e não abrem shell livre.
- `.tar`/`.tar.gz`/`.tgz` continuam aceitos; `.tar.xz`/`.tar.zst` são detectados e bloqueados com mensagem clara até existir decompressor auditado.
- Não inclui binários, rootfs, Box64 ou Bedrock.
