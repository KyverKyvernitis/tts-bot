# Patch: updater-worker-apk-publish-hardening-v2

Base: `repo-20260604-231706.zip`

## Corrige

- Updater não cai mais com código `141` durante a verificação de alterações locais:
  - `trim_alert_text` agora lê o pipe corretamente usando `python -c`;
  - `collect_local_tracked_changes` não depende mais de pipeline frágil com `git status`;
  - `collect_local_tracked_files` não usa `head` em pipeline com `pipefail`.
- `core-linux-embedded-binaries-build-pipeline.py verify` fica read-only:
  - o comando de diagnóstico não regrava `embedded-binaries-source-plan.json`;
  - evita sujar arquivos rastreados e bloquear o próximo update.
- Painel `_worker` fica mais resistente:
  - status de notificação/push não derruba a renderização do painel se algum resumo auxiliar falhar.
- Publicação do APK fica mais confiável:
  - Core Worker APK sobe para `0.5.74` / `versionCode 89`;
  - phone worker sobe para `1.10.31`;
  - `apk_build_debug` valida se o APK foi persistido em `artifacts/` logo após o Gradle;
  - falha de upload/publicação não perde o build: deixa artifact salvo e `publish_pending` para republicar;
  - `apk_publish_last` tenta republicar com erro controlado, sem estourar o job por timeout/rede;
  - recuperação de APK órfão agora procura mais workdirs antigos;
  - automação da VPS detecta build APK `running` antigo e enfileira `apk_publish_last` antes de rebuildar.

## Não muda

- Não builda APK na VPS.
- Não toca CallKeeper.
- Não libera Bedrock real, shell livre ou execução arbitrária.
