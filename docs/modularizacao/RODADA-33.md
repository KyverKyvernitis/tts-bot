# Rodada 33 — self-builder Chaquopy: ownership, processo Gradle e recovery

Entrada: checkpoint da rodada 32, 613 arquivos e 11.751.169 bytes. O corte segue
`INVENTARIO-SELF-BUILDER.md`: ownership/processo Gradle e recovery foram tratados
como uma única fronteira. Build orchestration, validação/publicação do APK e
cleanup geral permanecem no facade para a rodada seguinte. As quatro APIs públicas
Java→Chaquopy continuam literais em `coreworker.apk_self_builder`.

## Caracterização antes da correção

Sete cenários foram adicionados sem editar produção. A entrada resultou em
6 failures/1 pass:

1. uma segunda tentativa podia remover e tomar um `.apk-build-active` recém-criado
   na janela entre `mkdir(lock)` e a publicação atômica de `owner.json`;
2. falha em `_atomic_json(owner.json)` deixava o diretório de lock ownerless;
3. `attempt` corrompido em owner escapava com `ValueError` por `reconcile`;
4. o mesmo `attempt` corrompido escapava por `finalize_build_attempt`;
5. `pythonFinishedAt` não numérico escapava durante reconciliação;
6. `work` igual ao próprio `apk-self-builder/work` era aceito e podia apagar todas
   as tentativas ao finalizar um único job;
7. lock ownerless realmente antigo continuava recuperável; esse caso já passava e
   foi mantido como contrato de liveness.

Os testes usam somente diretórios temporários e subprocessos locais já existentes
na suíte. Nenhum Gradle/APK real, publicação, instalação ou job remoto foi usado.

## Correções e extração

Foi criado `coreworker/builder/process.py`, sem estado mutável de runtime e sem IO,
thread, processo ou rede no import. O facade mantém wrappers para preservar os
bindings/monkeypatches históricos. O módulo concentra:

- PID/start ticks, PGID, inspeção de membros e identidade de grupo órfão;
- sinalização TERM/KILL e coleta do grupo criado com `start_new_session=True`;
- marcador de cancelamento por attempt;
- aquisição/publicação do lock e validação de owner;
- `reconcile_interrupted_build` e `finalize_build_attempt`;
- execução do Gradle, owner metadata, timeout e cancelamento cooperativo.

O lock ownerless recebe grace de 30 s apenas quando `owner.json` ainda não existe,
fechando a janela de publicação sem transformar lock antigo em permanente. Um
`owner.json` existente porém ilegível não é removido por suposição. Se esta própria
tentativa cria o lock e falha ao publicar seu owner, somente esse lock recém-criado
é removido.

Campos numéricos de ownership passam a ser parseados conservadoramente; valor
malformado retorna identidade não verificada em vez de escapar pela API pública.
`work` precisa ser descendente estrito de `apk-self-builder/work`: o próprio root
é recusado antes de qualquer `rmtree`. Job/attempt, start ticks, PGID, cwd/cmdline,
barreira da outbox, estágio de cancelamento e ordem TERM→grace→KILL permanecem.

O facade caiu de 1.341 para 945 linhas; `builder/process.py` tem 657 linhas. O teste
textual de auto-enrollment foi atualizado para auditar facade + módulo extraído,
sem relaxar as asserções de que apenas parent hint e source fingerprint entram nos
parâmetros Gradle e nenhum token secreto vira `buildConfigField`.

## Gates na fonte

- Caracterização na entrada: 6 failed, 1 passed.
- Cenários novos após correção/extração: 7 passed.
- Regressões focadas de process/recovery: 76 passed.
- Família `test_core_worker*.py` + testes de modularização/source/process:
  225 passed, 85 skipped. O delta contra a rodada 32 é exatamente +7 passes; os
  85 skips continuam sendo os gates Java sem JARs deste host.
- Suíte global sem exclusões: mesmos 3 erros de coleta por `discord` ausente
  (`test_chatbot_imagegen.py`, `test_chatbot_reply_routing.py`, `test_chatbot_v2.py`)
  e 1 skip, exatamente iguais ao checkpoint 32 limpo.
- Cobertura ampla executável, excluindo somente esses três coletores e telemetria:
  278 passed; 344 passed; 223 passed/77 skipped; 314 passed/9 skipped/9 subtests.
- `test_phone_worker_telemetry.py`: 57 passed/54 teardown errors na rodada 33 e
  exatamente 57/54 no checkpoint 32, pelo mesmo fixture `Path.glob` no Python 3.13.
- `compileall` do pacote `coreworker` e do teste novo: exit 0.

`ruff` não está instalado. Java 21 existe, mas os JARs do gate obrigatório não
estão no checkpoint; não registrar lint/Java como aprovados. Nenhum resultado acima
mede desempenho Gradle/aparelho nem latência real.

## Fechamento por reconstrução

O patch preliminar 32→33 contém exatamente oito caminhos de escopo, passou
`git diff --check` e `git apply --check` e foi aplicado sobre uma nova extração do
checkpoint 32 original, sem caches. A reconstrução produziu 616 arquivos e
11.773.422 bytes, todos idênticos à fonte congelada em caminhos, bytes e modos.
`compileall` passou; process/recovery repetiu 76 passes e a família Core Worker
repetiu 225 passes/85 skips. O patch final é regenerado depois deste registro e
repete a reconstrução antes do empacotamento. Hashes ficam no relato externo para
evitar autorreferência neste arquivo.

## Próximo corte

Separar build orchestration + validação/publicação + cleanup mantendo a barreira
resultado durável→`finalize_build_attempt`, identidade/versionCode/versionName,
retenção, hashes, rollback e cancelamento antes/durante publish. Depois seguir
MainActivity/UI e, por fim, discovery/release/plataforma.
