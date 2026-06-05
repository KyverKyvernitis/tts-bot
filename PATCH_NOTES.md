# Patch: core-worker-build-tracking-runtime-summary-base-tools-v1

Base: `repo-20260604-213007.zip`

## Inclui

- APK `0.5.72` / `versionCode 87`.
- Phone worker `1.10.28`.
- `runtime-summary` agora enriquece o heartbeat compacto com o último preflight Core Linux válido.
- `coreLinux.runnerPreflight.embedded` e `coreLinux.embedded` passam a expor, de forma estável:
  - `executor`, `runner`, `proot`, `busybox`, `box64`;
  - `present`, `embeddedInApk`, `allowedForFutureExecution`, `canExecute`, `placeholder`, `size`, `sha256`, `detectedBy`.
- Corrige o caso onde o painel/comando lia `runner_* = null` mesmo depois do preflight real confirmar `runner` embutido no APK.
- Corrige falso estado de build falho quando já existe `latest.json` publicado para a mesma versão/source.
- Automação de APK ignora falha antiga se houver build/publicação mais nova bem-sucedida para a mesma versão/source.
- Builder do phone worker passa a salvar no `.apk.json` e `latest-artifact.json`:
  - `gradle_log_path`, `gradle_log_exists`, `gradle_log_bytes`;
  - `build_successful`, `build_result`, `phoneWorkerVersion`.
- Limpeza segura de artifacts antigos no builder (`PHONE_WORKER_APK_BUILD_KEEP_ARTIFACTS`, padrão 12), sem tocar no último APK nem no `latest-artifact.json`.
- Reintroduz o asset `assets/core-linux/embedded-binaries-source-plan.json` no source do APK.
- Pipeline de binários sobe para source plan `v5` e adiciona comandos seguros:
  - `audit-base-tools`: valida apenas `proot` + `busybox` em dry-run;
  - `stage-base-tools`: importa apenas `proot` + `busybox` auditados; `box64` continua para etapa separada.

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
