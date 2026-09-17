# Rodada 34 — self-builder Chaquopy: build, artifact/publish e cleanup

Entrada: checkpoint da rodada 33, 616 arquivos e 11.773.687 bytes. O último corte
Python do self-builder separa build orchestration e a fronteira de artifact/
publicação/retenção sem alterar as quatro APIs Java→Chaquopy em
`coreworker.apk_self_builder`. `process.py` continua dono de ownership/recovery e,
principalmente, `finalize_build_attempt` continua sendo a única liberação de
work/lock depois da barreira de resultado durável no serviço Java.

## Caracterização antes da correção

Seis cenários foram executados antes de editar produção: 5 falharam e 1 já passava.

1. build encerrado antes de iniciar Gradle podia escrever arquivos privados no
   workdir sem publicar `work` no owner; após o handoff durável o finalize liberava
   o lock, mas não conhecia o diretório privado para removê-lo;
2. `apk_publish_last` aceitava `latest-artifact.json` sem SHA-256 persistido,
   permitindo republicar um APK válido sem provar que ainda era o artifact
   associado ao metadata durável;
3. exceção durante `connection.send()` deixava a conexão multipart aberta;
4. `shutil.copy2` interrompido podia deixar bytes parciais diretamente no nome
   final `.apk` dentro de `artifacts/`;
5. o sufixo de colisão concatenava `notificationId` sem sanitização; barras e
   segmentos `..` quebravam o destino e podiam sair da pasta `artifacts/`;
6. finalize de um owner pré-Gradle com work já conhecido era seguro na entrada;
   esse contrato verde foi mantido.

Os testes usam somente diretórios temporários, APK/identidade e conexões HTTP fake.
Nenhum Gradle/APK real, download remoto, publicação na VPS, instalação ou job
remoto foi executado.

## Correções e extração

`coreworker/builder/build.py` concentra a orquestração sem estado global. Logo após
criar o workdir, antes do primeiro download/private input, publica no owner `work`,
`log`, estágio e executor Gradle zero. Assim qualquer saída pré-Gradle preserva o
mesmo handoff `pythonFinishedAt/result_handoff_pending`, mas
`finalize_build_attempt` consegue remover o workdir privado depois que Java já
persistiu/fsyncou a outbox.

`coreworker/builder/artifact.py` concentra validação do APK, promoção, multipart,
republicação e retenção. Promoção usa arquivo privado temporário, confirma o hash
validado, faz `os.replace` e fsync best-effort do diretório; erro de cópia remove o
temporário e nunca expõe nome final parcial. O sufixo de colisão passa por
`_safe_filename` e um segundo conflito recebe prefixo do SHA. Republicação exige
SHA-256 persistido de 64 hex e igualdade com o APK revalidado. Multipart fecha a
conexão em `finally`, inclusive em falha/cancelamento.

Cleanup foi movido para a mesma unidade de artifact, mantendo retenção de 3 APKs,
8 logs, latest/current, active work/log, cancellation markers por 24 h e sem tocar
no toolchain. Workdir symlink é ignorado antes de tree sizing/rmtree. O facade
mantém wrappers/bindings vivos (`shutil`, HTTP classes, clocks, hashes, callbacks),
portanto monkeypatches e a API externa continuam no mesmo ponto.

O facade cai de 945 para 597 linhas. `builder/artifact.py` tem 389 linhas e
`builder/build.py` 291. A barreira de outbox não foi movida nem antecipada:
`_build` apenas marca `result_handoff_pending`; a liberação continua em
`finalize_build_attempt` após o serviço Java tornar o resultado durável.

## Gates na fonte

- Caracterização na entrada: 5 failed, 1 passed; após correção: 6 passed.
- Seleção relacionada self-builder/identity/UI: 58 passed.
- Família `test_core_worker*.py` + `test_apk_self_builder*.py`: 231 passed,
  85 skipped. São exatamente +6 passes contra a rodada 33; os 85 skips continuam
  sendo os gates Java sem JARs deste host.
- Suíte global sem exclusões: os mesmos 3 erros de coleta por `discord` ausente
  (`test_chatbot_imagegen.py`, `test_chatbot_reply_routing.py`, `test_chatbot_v2.py`)
  e 1 skip, iguais ao checkpoint 33.
- Cobertura executável, excluindo somente esses três coletores e telemetria:
  317 passed/9 skipped/9 subtests; 292 passed; 333 passed; 223 passed/77 skipped.
- `test_phone_worker_telemetry.py`: 57 passed/54 teardown errors na rodada 34 e
  exatamente 57/54 na rodada 33 limpa, pelo fixture `Path.glob` no Python 3.13.
- `compileall` de `coreworker` e do teste novo: exit 0.

`ruff` não está instalado. Java 21 está presente, mas os JARs do gate Java não
estão no checkpoint; nenhum desses gates é marcado como aprovado. Os testes desta
rodada não medem desempenho Gradle/aparelho ou latência real.

## Fechamento por reconstrução

O patch preliminar 33→34 contém exatamente nove caminhos de escopo, passa
`git diff --check` e `git apply --check` e, aplicado sobre uma extração nova do
checkpoint 33, produz 620/620 arquivos idênticos à fonte em caminhos, bytes e
modos (11.804.286 bytes). `compileall` passa e o gate relacionado repete 58 passes.

Na primeira execução da família Core Worker na reconstrução, o teste real de `/proc`
`test_exact_gradle_process_group_is_stopped_before_requeue` retornou identidade não
verificada uma vez. Como fonte e reconstrução eram byte-idênticas, o cenário foi
repetido isoladamente nos dois estados e passou em ambos; a repetição completa na
reconstrução fechou em 231 passes/85 skips. Não houve alteração de teste/código para
forçar esse resultado. O patch final é regenerado depois deste registro e repetido
antes do empacotamento. Hashes ficam no relato externo para evitar autorreferência.

## Próximo corte

O self-builder Python fica estruturalmente fechado neste checkpoint. O próximo
bloco é MainActivity/UI: primeiro inventariar estado/renderização/eventos e
caracterizar lifecycle/reentrada sem alterar protocolos do worker. Depois seguir
discovery/release/plataforma e repetir gates Java/Gradle/aparelho no ambiente
completo antes da entrega final.
