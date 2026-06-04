# Core Linux embedded binaries

Coloque aqui apenas binários reais `arm64-v8a` aprovados para o APK privado:

- `libcoreworker_runner.so`
- `libcoreworker_proot.so`
- `libcoreworker_busybox.so`
- `libcoreworker_box64.so`

Regras:

- não usar placeholder;
- não baixar em runtime;
- não executar neste estágio;
- não embutir `bedrock_server` no APK;
- validar com `scripts/core-linux-embedded-binaries-intake.py` antes de buildar.
