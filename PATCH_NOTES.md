# patch-core-linux-runner-preflight-v1-20260604

Base: `repo-20260604-100017.zip`

## Objetivo

Adicionar o primeiro preflight seguro do runner Core Linux depois do rootfs real validado.

Este patch apenas detecta requisitos e registra estado. Ele não inicia Bedrock, não inicia Box64, não executa runner real, não abre shell livre e não aceita comando remoto arbitrário.

## Alterações

- APK bump para `0.5.63` / `versionCode 78`.
- Novo `CoreLinuxRunnerPreflightManager.java`.
- Novos jobs leves:
  - `apk_core_linux_runner_status`
  - `apk_core_linux_runner_preflight`
  - `apk_core_linux_runner_requirements`
- Nova capability pública:
  - `core-linux-runner-preflight-v1`
- Heartbeat/Core Linux snapshot passa a expor:
  - `runnerPreflightState`
  - `runnerPreflightSummary`
  - `runnerReady`
  - `runnerBlocked`
  - `runnerExecutionAllowed`
  - `runnerRequirementsReady`
- Preflight detecta, sem executar:
  - rootfs real validado;
  - executor nativo do APK;
  - proot embutido;
  - busybox embutido;
  - Box64 embutido;
  - bedrock_server presente;
  - EULA aceita;
  - server.properties presente;
  - candidato Box64 em diretório gravável bloqueado pelo Android 10+.
- Estado persistido em:
  - `files/core-linux/runtime/runner-preflight-state.json`
  - `files/core-linux/runtime/runner-state.json`
  - `files/core-linux/logs/runner-preflight.log`
- VPS/painel reconhecem os novos jobs/capabilities.

## Segurança mantida

- Bedrock start real: bloqueado.
- Box64 start: bloqueado.
- Runner real: bloqueado.
- Shell livre: bloqueado.
- Comando remoto arbitrário: bloqueado.
- Binários importados/baixados no app home: não executados.

## Fora do patch

- Não toca updater.
- Não toca CallKeeper.
- Não mexe na UI do APK.
- Não mexe em player de música.
- Não mexe em TTS runtime.
- Não remove Termux ainda.
