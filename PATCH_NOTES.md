# Patch: core-linux-smoke-gate-v2

Base: `repo-20260605-133937(2).zip`

## Corrige / melhora

- Fecha o falso positivo do `apk_core_linux_runtime_smoke_test`:
  - antes ele podia preparar/validar scaffold e retornar OK sem PRoot/BusyBox/rootfs real;
  - agora ele vira um **smoke gate**: só fica OK quando a base real para substituir Termux estiver pronta.
- O smoke gate passa a exigir:
  - executor JNI allowlist pronto;
  - rootfs real validado;
  - `runnerBaseRequirementsReady`/`termuxReductionReady`;
  - runner + PRoot + BusyBox auditados pelo preflight.
- Remove `apk_core_linux_runtime_smoke_test` da fila automática da VPS enquanto essa etapa ainda depende de binários externos.
  - o job continua disponível manualmente;
  - evita painel “com erro” por uma etapa que ainda não deve rodar toda hora.
- Atualiza capacidades visíveis para `core-linux-runner-preflight-v6` e `core-linux-embedded-binaries-intake-v6`.
- Atualiza o APK para `0.5.76` (`versionCode 91`) para forçar publicação do próximo APK.

## Próximo passo depois deste patch

- Trazer os binários auditados `libcoreworker_proot.so` + `libcoreworker_busybox.so` e, se o PRoot não for estático, `libcoreworker_libtalloc.so`.
- Rodar:

```bash
python3 scripts/core-linux-embedded-binaries-build-pipeline.py audit-base-tools --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
python3 scripts/core-linux-embedded-binaries-build-pipeline.py stage-base-tools --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify --metadata-file /tmp/core-linux-binaries-metadata.json --strict
```

## Não muda

- Não toca CallKeeper.
- Não builda APK na VPS.
- Não adiciona placeholders de PRoot/BusyBox.
- Não inicia Bedrock, não abre shell livre e não executa comando remoto arbitrário.
