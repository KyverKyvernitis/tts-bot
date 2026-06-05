# Patch V11 — PRoot loader32 optional gate

Objetivo: corrigir o build do APK depois do V10. O V10 corrigiu o BusyBox wrapper, mas o Gradle passou a reprovar `libcoreworker_proot_loader32.so` porque ele é ELF32/ARM. Esse loader é multiarch futuro e não deve bloquear o APK ARM64 atual.

## O que muda

- APK sobe para `0.5.81` / `versionCode 96`.
- `verifyCoreLinuxEmbeddedBinaries` passa a aceitar `proot_loader32` como opcional ARM32.
- O gate continua rígido para a cadeia ARM64 necessária agora:
  - `libcoreworker_proot.so`
  - `libcoreworker_proot_loader.so`
  - `libtalloc.so`
  - `libcoreworker_busybox.so`
  - `libbusybox.so`
  - `libandroid-selinux.so`
  - `libpcre2-8.so`
- Stages expostos:
  - `core-linux-runner-preflight-v11`
  - `core-linux-embedded-binaries-intake-v11`
- Adiciona `scripts/core-linux-embedded-binaries-metadata.v11.json`.
- Documenta que `loader32` não é requisito do smoke ARM64 atual.

## Segurança

- Não baixa nada.
- Não executa PRoot/BusyBox.
- Não libera runner real.
- Não libera Bedrock/Box64.
- Apenas corrige validação/diagnóstico para permitir o build do APK ARM64.

## Validação feita

```bash
python3 -m py_compile webserver.py scripts/core-linux-embedded-binaries-build-pipeline.py scripts/core-linux-embedded-binaries-intake.py
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify --strict --metadata-file scripts/core-linux-embedded-binaries-metadata.v11.json
```

Resultado: runner, proot, busybox wrapper, libbusybox, libandroid-selinux, libpcre2-8 e libtalloc validaram. `proot_loader32` foi tratado como opcional/multiarch no gate Gradle.
