# Patch Core Linux V14.2.1 — Box64 como asset controlado

- Move Box64 de `jniLibs` para `assets/core-linux/bin/box64`.
- Mantém auditoria SHA256/tamanho/ELF64/AArch64.
- Exclui `libcoreworker_box64.so`/`libbox64.so` do empacotamento nativo caso uma base antiga ainda tenha esses arquivos.
- Ajusta o preflight para auditar o asset, não `nativeLibraryDir`.
- Mantém bloqueado: execução de Box64, `box64 --version`, Bedrock, shell livre e comando arbitrário.
