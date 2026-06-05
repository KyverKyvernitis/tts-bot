# Patch: core-worker-base-tools-audit-v1

Base: `repo-20260604-215859.zip`

## Inclui

- APK `0.5.73` / `versionCode 88`.
- Phone worker `1.10.29`.
- Source plan sobe para `core-worker-embedded-binaries-source-plan-v6`.
- Pipeline sobe para `core-linux-embedded-binaries-build-pipeline-v4`.
- Intake local sobe para `core-worker-embedded-binaries-local-v4`.
- Preflight do runner sobe para `core-linux-runner-preflight-v5`.
- Registro auditável das fontes pesquisadas:
  - `proot` Termux `5.1.107.76`, `GPL-2.0`, SHA-256 do source oficial do recipe Termux.
  - `libtalloc` `2.4.3`, `GPL-3.0`, SHA-256 do source oficial do recipe Termux.
  - `busybox` Termux `1.37.0-r3`, `GPL-2.0`, SHA-256 do source oficial do recipe Termux.
- Novo alvo opcional `libcoreworker_libtalloc.so` para permitir PRoot dinâmico auditado sem esconder dependência.
- `audit-base-tools` e `stage-base-tools` agora tratam `proot + busybox` como obrigatórios e `libtalloc` como dependência auditada quando necessária.
- Intake agora valida:
  - ELF arm64 e tamanho mínimo;
  - metadata de origem/licença;
  - `sourceSha256` esperado do recipe;
  - `binarySha256` quando informado;
  - compliance de GPL (`completeCorrespondingSourceReady`, `licenseTextIncluded`, `sourceUrl`);
  - `linkMode` (`static`, `self-contained` ou `dynamic-with-bundled-dependencies`);
  - rejeição de build inseguro em prefixo vivo do worker quando declarado.
- Preflight/runtime-summary passam a expor:
  - `libtallocEmbedded`;
  - `prootNeedsLibtalloc`;
  - `prootDependencyReady`;
  - `baseToolsReady`;
  - asset `embedded.libtalloc`.
- README dos binários nativos atualizado com a política de GPL/source e dependências.

## Continua bloqueado

- Bedrock start real.
- Box64 start.
- Shell livre.
- Comando remoto arbitrário.
- Download automático de binários no APK.
- Execução de binários importados do app-home.

## Não tocado

- Updater.
- UI do app.
- CallKeeper.
- Música/player.
- TTS runtime.
- Build na VPS.
