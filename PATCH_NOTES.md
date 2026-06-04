# Patch: core-linux embedded runner assets v1 + preflight cleanup

## Resumo

- APK 0.5.65 / versionCode 80.
- Junta cleanup do runner preflight v2 com scaffold/detector de assets embutidos.
- Mantém tudo em modo seguro: sem iniciar Bedrock, Box64, runner real ou shell livre.

## Incluído

- Corrige texto legado `preflight v1` para `preflight v2` nos bloqueios atuais.
- Sanitiza resultados antigos de runner para não expor EULA em status/painel/histórico regravado.
- Remove EULA de bloqueios visíveis do preflight/Bedrock service; confirmação fica interna para etapa futura de start real.
- Adiciona scaffold de manifest dos assets nativos esperados:
  - `libcoreworker_runner.so` / `libcoreworker_executor.so`
  - `libcoreworker_proot.so` / `libproot.so`
  - `libcoreworker_busybox.so` / `libbusybox.so`
  - `libcoreworker_box64.so` / `libbox64.so`
- Detector agora considera o executor JNI carregado como runner embutido quando o Android não expõe arquivo em `nativeLibraryDir`.
- Mantém componentes ausentes como pendência real; não cria `.so` placeholder.

## Mantido bloqueado

- Bedrock start real.
- Box64 start.
- Runner real.
- Shell livre.
- Comando remoto arbitrário.
- Execução de binários baixados/importados.

## Fora do escopo

- UI/layout.
- Updater.
- CallKeeper.
- Música/player.
- TTS runtime.
