# Patch: Core Linux runner assets preflight v2

## Objetivo
Evolui o preflight do runner sem iniciar Bedrock, sem abrir shell livre e sem executar binários importados. A EULA deixa de aparecer como pendência/status visível para o usuário e fica fora do fluxo de runner/assets.

## Mudanças
- APK 0.5.64 / versionCode 79.
- Runner preflight passa para `core-linux-runner-preflight-v2`.
- Preflight persiste estado em `runner-preflight-state.json` / `runner-state.json`.
- Background heartbeat/foreground service agora reporta capability e jobs do runner preflight.
- Background snapshot também carrega estado do runner e rootfs real validado.
- Detector de assets nativos embutidos no APK:
  - `libcoreworker_runner.so` / `libcoreworker_executor.so`
  - `libcoreworker_proot.so` / `libproot.so`
  - `libcoreworker_busybox.so` / `libbusybox.so`
  - `libcoreworker_box64.so` / `libbox64.so`
- Asset snapshot mostra nomes esperados, ABI, tamanho, hash quando aplicável e bloqueio em diretório gravável.
- EULA removida das pendências/checks/missing/nextActions do runner preflight.
- EULA removida dos jobs/labels visíveis da VPS/painel workers.
- Bedrock continua detectando apenas arquivos técnicos sem iniciar nada: `bedrock_server` e `server.properties`.

## Mantido bloqueado
- Bedrock start real.
- Box64 start.
- Runner real.
- Shell livre.
- Comando remoto arbitrário.
- Execução de binários importados/baixados.

## Fora de escopo
- Updater/ZIP/GitHub/rollback/redo.
- CallKeeper.
- Player de música.
- TTS runtime.
- Redesign de UI.
