# Core Linux embedded binaries

Esta pasta é o único local oficial para binários nativos `arm64-v8a` do Core Linux no APK privado.

Arquivos esperados:

- `libcoreworker_runner.so` — runner próprio, seguro e allowlist-only. Já pode ser gerado pelo pipeline local e embutido no APK.
- `libcoreworker_proot.so` — PRoot arm64 validado.
- `libcoreworker_busybox.so` — BusyBox arm64 validado.
- `libcoreworker_box64.so` — Box64 arm64 validado.

Regras:

- não usar placeholder;
- não baixar em runtime;
- não executar neste estágio;
- não embutir `bedrock_server` no APK;
- validar com `scripts/core-linux-embedded-binaries-intake.py` antes de buildar;
- preparar/buildar com `scripts/core-linux-embedded-binaries-build-pipeline.py`.

Comandos úteis:

```bash
python3 scripts/core-linux-embedded-binaries-build-pipeline.py plan
# gera e embute o runner próprio, sem baixar terceiros
python3 scripts/core-linux-embedded-binaries-build-pipeline.py build-runner --stage
python3 scripts/core-linux-embedded-binaries-build-pipeline.py metadata-template > /tmp/core-linux-binaries-metadata.json
# audita sem copiar
python3 scripts/core-linux-embedded-binaries-build-pipeline.py audit-input --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
# só copia se os metadados externos estiverem aprovados
python3 scripts/core-linux-embedded-binaries-build-pipeline.py stage --input-dir /caminho/dos/binarios --metadata-file /tmp/core-linux-binaries-metadata.json
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify --metadata-file /tmp/core-linux-binaries-metadata.json
```


## Política de assets externos

`proot`, `busybox` e `box64` só devem entrar no APK depois de build/import auditado com metadata de origem, licença, versão/commit/hash e receita de build. O intake rejeita stage real desses assets sem `licenseStatus` aprovado (`verified-audited`, `source-built` ou `redistributable-verified`).
