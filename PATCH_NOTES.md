# Patch: core-linux-embedded-binaries-intake-v1

Base: `repo-20260604-185848.zip`

## Inclui

- APK `0.5.66` / `versionCode 81`.
- Capacidade nova: `core-linux-embedded-binaries-intake-v1`.
- Pipeline local de intake para binários reais arm64-v8a, sem download e sem execução.
- Gradle task `verifyCoreLinuxEmbeddedBinaries`:
  - valida arquivos reais quando existirem;
  - rejeita placeholder/arquivo pequeno;
  - valida ELF64 AArch64 mínimo;
  - não exige presença por padrão;
  - pode exigir todos com `CORE_WORKER_REQUIRE_EMBEDDED_BINARIES=true`.
- Script `scripts/core-linux-embedded-binaries-intake.py` para copiar binários reais fornecidos manualmente para nomes oficiais.
- Manifesto `assets/core-linux/embedded-binaries-manifest.json`.
- Preflight separa:
  - executor JNI do APK;
  - core-runner asset real;
  - proot;
  - busybox;
  - box64.

## Continua bloqueado

- Bedrock start real.
- Box64 start.
- Runner real.
- Shell livre.
- Comando remoto arbitrário.
- Download automático de binários no APK.

## Não tocado

- UI/layout.
- updater.
- CallKeeper.
- música/player.
- TTS runtime.
