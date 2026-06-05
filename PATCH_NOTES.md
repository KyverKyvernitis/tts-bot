# Patch V9 — Core Linux BusyBox dependency chain

Este patch fecha a cadeia base que faltava para substituir o Termux no APK, ainda sem liberar execução real.

## Importado/ajustado

- BusyBox `1.37.0-3` do pacote Termux aarch64.
- `libbusybox.so` do mesmo pacote.
- `libandroid-selinux.so` do pacote Termux aarch64.
- `libpcre2-8.so` do pacote Termux aarch64.
- PRoot já importado no V8 recebeu correção extra de RUNPATH para `$ORIGIN`.
- BusyBox recebeu correção de `NEEDED libbusybox.so.1.37.0 -> libbusybox.so`.
- BusyBox/libbusybox/libandroid-selinux/libpcre2 receberam RUNPATH `$ORIGIN` para resolver dependências no `nativeLibraryDir` do APK.

## Segurança/gate

- APK sobe para `0.5.79` / `versionCode 94`.
- Preflight sobe para `core-linux-runner-preflight-v9`.
- Intake sobe para `core-linux-embedded-binaries-intake-v9`.
- `baseToolsReady` agora exige:
  - runner;
  - PRoot;
  - loader do PRoot;
  - `libtalloc.so` real;
  - BusyBox;
  - `libbusybox.so`;
  - `libandroid-selinux.so`;
  - `libpcre2-8.so`.
- Runner real continua bloqueado até rootfs real + smoke allowlist no APK instalado.

## Validação feita

```bash
python3 -m py_compile webserver.py scripts/core-linux-embedded-binaries-build-pipeline.py scripts/core-linux-embedded-binaries-intake.py
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify --strict --metadata-file scripts/core-linux-embedded-binaries-metadata.v9.json
readelf -d android/core-worker-app/app/src/main/jniLibs/arm64-v8a/libcoreworker_proot.so
readelf -d android/core-worker-app/app/src/main/jniLibs/arm64-v8a/libcoreworker_busybox.so
readelf -d android/core-worker-app/app/src/main/jniLibs/arm64-v8a/libbusybox.so
readelf -d android/core-worker-app/app/src/main/jniLibs/arm64-v8a/libandroid-selinux.so
readelf -d android/core-worker-app/app/src/main/jniLibs/arm64-v8a/libpcre2-8.so
```
