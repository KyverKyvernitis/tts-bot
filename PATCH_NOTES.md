# patch-core-linux-rootfs-import-v1-20260603

## Objetivo

Adicionar a primeira etapa segura de importação de rootfs real no APK Core Worker, sem remover Termux ainda e sem iniciar Bedrock/Box64/shell livre.

## Entregue

- APK `0.5.60` / `versionCode 75`.
- Novo `CoreLinuxRootfsImportManager.java`.
- Importação de rootfs pelo Storage Access Framework (`ACTION_OPEN_DOCUMENT`).
- Formatos aceitos no v1: `.tar`, `.tar.gz`, `.tgz`.
- SHA-256 calculado por streaming do arquivo escolhido.
- SHA-256 esperado opcional no modal; se informado e não bater, a importação falha.
- Extração sempre em staging (`core-linux/staging/rootfs-import-next`).
- Promoção só depois da validação.
- Rootfs anterior preservado como rollback interno durante promoção.
- Validação mínima de rootfs real:
  - `.core-worker-rootfs-ready`
  - `.core-worker-rootfs-manifest.json`
  - `etc/os-release`
  - `bin` ou `usr/bin`
  - `tmp`
  - `home/core`
  - `var/log`
  - `opt/core-worker/rootfs-policy.json`
- Manifesto real: `kind=core-worker-rootfs-real`.
- Estado persistido em:
  - `core-linux/runtime/rootfs-import-state.json`
  - `core-linux/runtime/rootfs-state.json`
- Heartbeat/snapshot passam a expor:
  - `rootfsValidationLevel`
  - `rootfsDistributionReady`
  - `rootfsImportState`
  - `rootfsImportSummary`
- Botão no APK:
  - `Importar rootfs real`
  - `Status rootfs real`
- Jobs leves adicionados:
  - `apk_core_linux_rootfs_import_status`
  - `apk_core_linux_rootfs_import_validate`
  - `apk_core_linux_rootfs_import_abort`
  - `apk_core_linux_rootfs_real_status`
- Painel workers/VPS reconhece os novos jobs/capability.

## Segurança mantida

- Não executa binários do rootfs importado.
- Não inicia Bedrock.
- Não inicia Box64.
- Não abre shell livre.
- Não aceita comando remoto arbitrário.
- Não baixa rootfs automaticamente.
- Não toca updater.
- Não toca CallKeeper.
- Não toca música/TTS runtime.

## Limitações conhecidas do v1

- `.tar.xz` ainda não é suportado.
- Hardlinks no tar são bloqueados.
- Symlinks absolutos são bloqueados por segurança.
- PAX/long path avançado pode exigir refinamento em patch futuro se o rootfs escolhido usar metadados complexos.
- Esta etapa só importa/valida/promove. Runner real fica para patch futuro.
