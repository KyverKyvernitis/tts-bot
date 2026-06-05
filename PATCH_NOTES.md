# Patch V8 — Core Linux Termux .deb intake parcial

Este patch importa os binários reais enviados em `binarios.zip` para o APK, mas mantém o gate seguro.

## Importado

- PRoot `5.1.107.76` arm64 do pacote Termux.
- Loader arm64 do PRoot.
- Loader32 do PRoot preservado para diagnóstico/futuro.
- libtalloc `2.4.3` arm64.
- Duplicata `libtalloc.so` para resolução do `NEEDED` do PRoot patchado.
- Metadata V8 com SHA256, origem, versão, licença e compliance GPL.
- Textos GPL-2.0/GPL-3.0 em assets do APK.

## Bloqueado de propósito

BusyBox `1.37.0-3` não foi embutido porque o pacote Termux recebido é dinâmico:

- `/usr/bin/busybox` depende de `libbusybox.so.1.37.0`;
- `libbusybox.so.1.37.0` depende de `libandroid-selinux.so`;
- `libandroid-selinux.so` não veio em `binarios.zip`.

O preflight V8 agora exige também o loader do PRoot. O runner real continua bloqueado até BusyBox + dependências + rootfs passarem no smoke allowlist.

## Validação feita

```bash
python3 -m py_compile webserver.py scripts/core-linux-embedded-binaries-build-pipeline.py scripts/core-linux-embedded-binaries-intake.py
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify --strict --metadata-file scripts/core-linux-embedded-binaries-metadata.v8.json
readelf -d android/core-worker-app/app/src/main/jniLibs/arm64-v8a/libcoreworker_proot.so
```

Resultado importante: o PRoot foi ajustado para depender de `libtalloc.so` em vez de `libtalloc.so.2`, e o BusyBox ficou bloqueado por falta de `libandroid-selinux.so`.
