# Core Linux embedded binaries

Esta pasta é o único local oficial para binários nativos `arm64-v8a` do Core Linux no APK privado.

Arquivos esperados:

- `libcoreworker_runner.so` — runner próprio, seguro e allowlist-only. Já pode ser gerado pelo pipeline local e embutido no APK.
- `libcoreworker_proot.so` — PRoot arm64 validado.
- `libcoreworker_libtalloc.so` — dependência auditada do PRoot quando o build não for estático/self-contained.
- `libcoreworker_busybox.so` — BusyBox arm64 validado.
- `libcoreworker_box64.so` — Box64 arm64 validado, somente depois da base PRoot + BusyBox.

Regras:

- não usar placeholder;
- não baixar em runtime;
- não executar neste estágio;
- não embutir `bedrock_server` no APK;
- não rodar build inseguro dentro do prefixo vivo do Termux/worker;
- validar com `scripts/core-linux-embedded-binaries-intake.py` antes de buildar;
- preparar/buildar com `scripts/core-linux-embedded-binaries-build-pipeline.py`.

Comandos úteis:

```bash
python3 scripts/core-linux-embedded-binaries-build-pipeline.py plan
# gera e embute o runner próprio, sem baixar terceiros
python3 scripts/core-linux-embedded-binaries-build-pipeline.py build-runner --stage
python3 scripts/core-linux-embedded-binaries-build-pipeline.py metadata-template > /tmp/core-linux-binaries-metadata.json
# audita só a base PRoot + BusyBox; libtalloc é aceito junto quando o PRoot for dinâmico
python3 scripts/core-linux-embedded-binaries-build-pipeline.py audit-base-tools --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
# copia só se os metadados externos estiverem aprovados e coerentes
python3 scripts/core-linux-embedded-binaries-build-pipeline.py stage-base-tools --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify --metadata-file /tmp/core-linux-binaries-metadata.json
```


## Extração pelo Android

O APK privado força `android:extractNativeLibs="true"` e `packaging.jniLibs.useLegacyPackaging=true`. Isso é obrigatório porque `proot`, `busybox`, `libtalloc` e `box64` só podem virar candidatos de execução futura quando aparecerem como arquivos reais em `nativeLibraryDir`. Se o APK só mostrar uma `ZipEntry` em `lib/arm64-v8a/*.so`, o preflight v7 detecta o asset para diagnóstico, mas mantém `allowedForFutureExecution=false`.

## Política de assets externos

`proot`, `libtalloc`, `busybox` e `box64` só devem entrar no APK depois de build/import auditado com metadata de origem, licença, versão/commit/hash e receita de build. O intake rejeita stage real desses assets sem `licenseStatus` aprovado (`verified-audited`, `source-built` ou `redistributable-verified`).

Para componentes GPL, o metadata também precisa declarar `sourceCompliance.completeCorrespondingSourceReady=true`, `licenseTextIncluded=true` e URL/caminho do source correspondente. Isso evita empacotar binário GPL sem rastro de source/licença.

## V8 — PRoot Termux deb intake parcial

Arquivos importados de `proot_5.1.107.76_aarch64.deb` e `libtalloc_2.4.3_aarch64.deb`:

- `libcoreworker_proot.so`: executável PRoot arm64 empacotado como native lib.
- `libcoreworker_proot_loader.so`: loader arm64 exigido pelo PRoot.
- `libcoreworker_proot_loader32.so`: loader 32-bit preservado para diagnóstico/futuro.
- `libcoreworker_libtalloc.so`: libtalloc auditada para validação do intake.
- `libtalloc.so`: duplicata com nome resolvível pelo linker depois do patch `NEEDED libtalloc.so.2 -> libtalloc.so` no PRoot.

BusyBox do pacote `busybox_1.37.0-3_aarch64.deb` não foi embutido neste patch porque o launcher depende de `libbusybox.so.1.37.0`, e essa biblioteca depende de `libandroid-selinux.so`, que não veio no ZIP de binários. O gate continua bloqueando BusyBox até essa dependência ser fornecida/auditada ou até usarmos um BusyBox static/self-contained.
