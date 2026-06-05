# Patch — Core Linux native extraction gate v7

Objetivo: preparar o APK para a etapa real de substituição do Termux sem marcar PRoot/BusyBox como executáveis só por estarem dentro do APK.

## O que muda

- Sobe o APK para `0.5.77` / `versionCode 92`.
- Força extração de native libs no APK:
  - `android:extractNativeLibs="true"` no `AndroidManifest.xml`.
  - `packaging.jniLibs.useLegacyPackaging=true` no Gradle.
- Atualiza o preflight do Core Linux para v7.
- O preflight agora diferencia:
  - asset físico em `nativeLibraryDir` = candidato futuro válido;
  - `ZipEntry` dentro do APK = apenas diagnóstico, não caminho executável.
- PRoot/BusyBox/libtalloc/Box64 só ficam `allowedForFutureExecution=true` quando:
  - estão extraídos como native lib física;
  - têm metadata externa aprovada;
  - não estão em diretório gravável bloqueado pelo Android 10+.
- Atualiza manifestos/capacidades para `core-linux-runner-preflight-v7` e `core-linux-embedded-binaries-intake-v7`.
- Adiciona o source plan `embedded-binaries-source-plan.json` aos assets do APK.

## Próximo passo depois deste patch

Importar os binários reais auditados:

```bash
python3 scripts/core-linux-embedded-binaries-build-pipeline.py metadata-template > /tmp/core-linux-binaries-metadata.json
python3 scripts/core-linux-embedded-binaries-build-pipeline.py audit-base-tools --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
python3 scripts/core-linux-embedded-binaries-build-pipeline.py stage-base-tools --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
```

Ainda não inclui PRoot/BusyBox reais, porque eles não foram enviados na base e não devem ser inventados/baixados pelo patch.
