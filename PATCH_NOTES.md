# Patch: core-linux-base-tools-phase

Base: `repo-20260605-005354.zip`

## Corrige / melhora

- Separa o preflight do Core Linux em duas fases reais:
  - **fase atual:** rootfs real + executor + runner + PRoot + BusyBox;
  - **fase futura:** Box64 + Bedrock + `server.properties`.
- O painel/runtime não vai mais tratar Box64/Bedrock como pendência da fase base para substituir o Termux.
- Quando PRoot + BusyBox estiverem auditados e embutidos, o estado passa a indicar claramente:
  - `runnerBaseRequirementsReady=true`;
  - `termuxReductionReady=true`;
  - próximo passo: smoke test real allowlist (`APK → runner → proot → rootfs`).
- Mantém o bloqueio de segurança:
  - sem shell livre;
  - sem comando remoto arbitrário;
  - sem iniciar Bedrock;
  - sem executar Box64/proot/busybox durante o preflight.
- O `runtime-summary` e o painel `_worker` passam a expor melhor:
  - `baseToolsReady`;
  - `runnerBaseRequirementsReady`;
  - `termuxReductionReady`;
  - `bedrockRequirementsReady`;
  - `currentMissing` e `futureMissing` separados.
- Atualiza o APK para `0.5.75` (`versionCode 90`) para forçar build/publicação do próximo APK.

## Próximo passo depois deste patch

- Importar PRoot + BusyBox auditados com:
  - `scripts/core-linux-embedded-binaries-build-pipeline.py audit-base-tools ...`
  - depois `stage-base-tools ...`
- Só depois liberar o smoke test real allowlist do rootfs.

## Não muda

- Não toca CallKeeper.
- Não builda APK na VPS.
- Não adiciona binários externos falsos/placeholders.
- Não inicia Bedrock e não libera runner real ainda.
