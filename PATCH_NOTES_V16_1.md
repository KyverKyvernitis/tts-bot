# Patch V16.1 — Rootfs glibc preflight telemetry

Objetivo: corrigir a telemetria do preflight `apk_core_linux_rootfs_glibc_preflight` antes de avançar para importação real de rootfs com glibc.

## Alterações

- APK `0.5.99` / `114`.
- Stage seguro: `core-linux-rootfs-glibc-intake-preflight-v16.1`.
- O resultado do job agora expõe o payload dedicado esperado pelos monitores:
  - `coreLinuxRootfsGlibcPreflight.stage`
  - `coreLinuxRootfsGlibcPreflight.state`
  - `coreLinuxRootfsGlibcPreflight.glibcRuntime`
  - `coreLinuxRootfsGlibcPreflight.missing`
  - `coreLinuxRootfsGlibcPreflight.checks`
- O handler também replica `stage`, `state`, `glibcRuntime`, `missing`, `checks`, `validationLevel` e `readyForBox64Smoke` no topo do resultado para evitar registros espelhados com `stage=None`/`state=None`.

## Segurança

- Não executa Box64.
- Não abre asset Box64.
- Não importa rootfs.
- Não executa binários do rootfs.
- Não inicia Bedrock.
- Não abre shell livre.

## Resultado esperado

Antes de importar rootfs com glibc:

```text
stage=core-linux-rootfs-glibc-intake-preflight-v16.1
state=rootfs_glibc_preflight_missing_runtime
```

Depois de importar rootfs Linux ARM64 com glibc, o alvo passa a ser:

```text
state=rootfs_glibc_ready_for_box64
readyForBox64Smoke=true
```
