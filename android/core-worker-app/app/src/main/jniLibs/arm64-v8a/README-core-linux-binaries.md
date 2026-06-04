# Core Linux embedded binaries

Esta pasta é o único local oficial para binários nativos `arm64-v8a` do Core Linux no APK privado.

Arquivos esperados:

- `libcoreworker_runner.so` — runner próprio, seguro e allowlist-only.
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
python3 scripts/core-linux-embedded-binaries-build-pipeline.py build-runner --stage
python3 scripts/core-linux-embedded-binaries-build-pipeline.py stage --input-dir /caminho/dos/binarios
python3 scripts/core-linux-embedded-binaries-build-pipeline.py verify
```
