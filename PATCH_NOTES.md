# Patch: worker-panel-apk-publish-recovery-v1

Base: `repo-20260604-223619.zip`

## Corrige

- `_worker` / `_workers` voltam a abrir o painel:
  - restaura `_core_worker_push_status_text()`;
  - mantém o resumo de Push sem quebrar o layout quando não existe token FCM.
- Publicação do APK gerado pelo worker builder:
  - phone worker sobe para `1.10.30`;
  - `apk_build_debug` agora procura APK órfão em `core-worker-apk-builds/build-*` quando o Gradle já terminou com `BUILD SUCCESSFUL`, mas o processo caiu antes de copiar/publicar;
  - se encontrar `app-debug.apk` válido, promove para `artifacts/latest-artifact.json` e publica sem rebuild;
  - `apk_publish_last` também tenta essa recuperação antes de dizer que não existe artifact;
  - publicação passa a usar o APK persistido em `artifacts/`, não o arquivo temporário do workdir;
  - falha de upload/publicação não marca o build Gradle como perdido: deixa `publish_pending=True` para republicar depois.
- Automação da VPS:
  - ignora build APK `running` obviamente velho para não travar por horas;
  - limpa leases expirados antes de decidir se já existe job ativo;
  - quando um APK já foi compilado mas não publicado, enfileira `apk_publish_last` antes de rebuildar.

## Não muda

- Não builda APK na VPS.
- Não muda o APK Android/versionCode nesta correção.
- Não toca CallKeeper.
- Não libera Bedrock real, shell livre ou execução arbitrária.
