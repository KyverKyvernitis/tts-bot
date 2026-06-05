# Patch V10 — Core Linux BusyBox wrapper gate + libtalloc RUNPATH

Este patch corrige o bloqueio que impedia o APK `0.5.79/94` de buildar no phone worker.

## Correções principais

- APK sobe para `0.5.80` / `versionCode 95`.
- `verifyCoreLinuxEmbeddedBinaries` passa a entender que:
  - `libcoreworker_busybox.so` é um wrapper ELF pequeno do BusyBox Termux;
  - `libbusybox.so` é o payload real que precisa carregar o peso/tamanho forte;
  - `libandroid-selinux.so` e `libpcre2-8.so` são dependências obrigatórias quando BusyBox está presente.
- `libtalloc.so` e `libcoreworker_libtalloc.so` tiveram RUNPATH higienizado de:
  - `/data/data/com.termux/files/usr/lib`
  - para `$ORIGIN`
- Metadata V10 registra os novos hashes e a regra de wrapper pequeno.
- A metadata V9 também foi atualizada como compatível para não quebrar comandos antigos de validação.

## Segurança mantida

- Nenhum binário externo é executado durante build/intake.
- Runner real segue bloqueado.
- Box64 e Bedrock continuam fora desta etapa.
- PRoot/BusyBox ainda precisam passar pelo smoke real no APK instalado antes de reduzir Termux.

## Validação recomendada

```bash
python3 -m py_compile webserver.py scripts/core-linux-embedded-binaries-build-pipeline.py scripts/core-linux-embedded-binaries-intake.py
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify --strict --metadata-file scripts/core-linux-embedded-binaries-metadata.v10.json

for f in \
  libcoreworker_proot.so \
  libcoreworker_busybox.so \
  libbusybox.so \
  libandroid-selinux.so \
  libpcre2-8.so \
  libtalloc.so \
  libcoreworker_libtalloc.so
 do
  echo "----- $f -----"
  readelf -d android/core-worker-app/app/src/main/jniLibs/arm64-v8a/$f | grep -E 'NEEDED|RUNPATH|RPATH' || true
 done
```

Resultado esperado: `libtalloc.so` e `libcoreworker_libtalloc.so` também devem mostrar `RUNPATH [$ORIGIN]`.
