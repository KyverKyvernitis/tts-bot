# Self-builder APK — fronteiras Python antes/depois da rodada 31

Entrada: checkpoint da rodada 30, 603 arquivos. O facade
`android/core-worker-app/app/src/main/python/coreworker/apk_self_builder.py`
tinha 1.958 linhas e concentrava toolchain, preflight, source, processo/recovery,
build, publicação e cleanup. A API Java→Chaquopy pública continua sendo:
`preflight`, `reconcile_interrupted_build`, `finalize_build_attempt` e `run`.

## Corte da rodada 31

| Fronteira | Dono após o corte | Estado preservado no facade |
| --- | --- | --- |
| Toolchain | `coreworker/builder/toolchain.py` | constantes/schema e wrappers compatíveis |
| Preflight/readiness | `coreworker/builder/preflight.py` | API pública `preflight` e bindings vivos |
| Resource preflight | `coreworker/builder/preflight.py` | limites/políticas continuam no facade e são passados por chamada |
| Source/process/recovery/build/publish | facade | sem mudança nesta rodada |

`toolchain.py` valida manifesto, fingerprint, ambiente e smoke. `preflight.py`
monta readiness público e preflight de recursos. Ambos são sem estado mutável de
runtime e não iniciam subprocesso, rede, thread ou IO pesado no import. Os wrappers
do facade passam callbacks atuais (`_safe_json_load`, `_atomic_json`, `_short`,
clock e helpers), preservando monkeypatch/bindings usados pelos testes e evitando
segunda cópia de estado.

## Bugs caracterizados antes da correção

Dois manifests corrompidos derrubavam o preflight em vez de serem classificados
como toolchain inválido:

1. `versions.jdkMajor="not-an-int"` escapava com `ValueError`;
2. `validation.requiredSmokeChecks` contendo item não-string escapava com
   `TypeError` ao construir `set`.

Os cenários foram adicionados primeiro e reproduziram 2 failures/1 pass. Depois
da correção, ambos retornam JSON `ready=false`, `toolchain.ok=false`, mantendo o
schema v1 compatível: o conjunto exato de `requiredSmokeChecks` continua exigido
somente para v2, como na entrada.

## Limites preservados

Nenhuma política de bateria, temperatura, memória, retenção, tamanho de source,
APK, timeout, hash, schema, Gradle/SDK/Chaquopy ou publicação foi redesenhada.
`native_dir` permanece na assinatura pública de `preflight`. O builder Termux é
outra implementação e não foi unificado com o Chaquopy. Process ownership,
recovery, cancelamento, source e publish continuam para rodadas seguintes.

## Rodada 32 — source e privados/hidratação

| Fronteira | Dono após o corte | Estado preservado no facade |
| --- | --- | --- |
| Download/source ZIP | `coreworker/builder/source.py` | limites, origem e wrappers/bindings vivos |
| Extração ZIP/projeto | `coreworker/builder/source.py` | `MAX_SOURCE_*` e `_is_inside` passados na chamada |
| Private files | `coreworker/builder/private_files.py` | payload e resultado continuam no fluxo `_build` |
| Runtime hydration | `coreworker/builder/private_files.py` | allowlist fixa; hash helper vivo fornecido pelo facade |
| Processo/recovery/build/publish | facade | sem mudança na rodada 32 |

Seis bugs foram reproduzidos antes da correção: escrita parcial antes de detectar
membro ZIP inseguro, colisão de membros normalizados, private file gravado antes de
validar keystore, newline em properties, symlink de repro-assets e stale file com
mesmo tamanho. O ZIP agora é totalmente pré-validado; privados são validados antes
da primeira escrita; hidratação não segue symlinks e compara conteúdo quando o
tamanho coincide.

O facade passa a ter 1.341 linhas. Os módulos novos têm 208 (`source.py`) e 159
(`private_files.py`) linhas. Não foram movidos ownership Gradle, recovery,
publicação nem cleanup. APIs públicas Java→Chaquopy continuam literais.

## Rodada 33 — ownership, processo Gradle e recovery

| Fronteira | Dono após o corte | Estado preservado no facade |
| --- | --- | --- |
| PID/start ticks/PGID | `coreworker/builder/process.py` | wrappers com bindings vivos |
| Lock/cancelamento/owner | `coreworker/builder/process.py` | payload/job/attempt seguem no fluxo `_build` |
| Gradle subprocess | `coreworker/builder/process.py` | toolchain/resources/payload passados por chamada |
| Reconcile/finalize | `coreworker/builder/process.py` | assinaturas Java→Chaquopy permanecem no facade |
| Build/publish/cleanup | facade | próximo corte; sem mudança estrutural nesta rodada |

Seis falhas foram reproduzidas antes da correção, com um sétimo cenário de lock
stale já verde. O corte fecha a janela de lock ownerless fresco, remove lock criado
pela própria tentativa quando publicar owner falha, torna metadados numéricos
corrompidos conservadores e impede `work == work_root` de virar `rmtree` global.
Lock ownerless antigo continua recuperável após grace; owner existente ilegível
não é roubado. PID/start ticks, PGID/cwd/cmdline, job/attempt, TERM/KILL, outbox e
cancelamento permanecem no mesmo contrato.

O facade passa de 1.341 para 945 linhas. `builder/process.py` tem 657 linhas e não
possui estado global mutável. Build orchestration, validação/publish e cleanup geral
continuam no facade; são o próximo corte antes de MainActivity/UI e discovery.

## Rodada 34 — build, artifact/publish e cleanup

| Fronteira | Dono após o corte | Estado preservado no facade |
| --- | --- | --- |
| Build orchestration | `coreworker/builder/build.py` | API `run`, políticas, callbacks e bindings vivos |
| APK validate/promote | `coreworker/builder/artifact.py` | identidade/hash/constants passados por chamada |
| Multipart/republicação | `coreworker/builder/artifact.py` | token/worker/origem e cancellation continuam por chamada |
| Retenção/cleanup | `coreworker/builder/artifact.py` | limites 3 APKs/8 logs continuam definidos no facade |
| Handoff/finalize | `builder/process.py` + facade público | outbox Java continua antes de `finalize_build_attempt` |

Cinco bugs foram reproduzidos antes da correção, com um sexto cenário já verde:
work privado pré-Gradle sem owner, metadata de republicação sem SHA, conexão
multipart não fechada em erro, nome final parcial após cópia interrompida e
`notificationId` não sanitizado no nome de colisão. Após a correção os seis passam.
Work/log são publicados no owner antes do primeiro private input; promoção do APK
é temporária+hash+replace; publish exige hash durável e conexão fecha em `finally`.

O facade passa de 945 para 597 linhas. `artifact.py` tem 389 linhas e `build.py`
291, ambos sem estado mutável de runtime nem trabalho no import. As quatro APIs
Java→Chaquopy permanecem literais. Com esse corte, toolchain, preflight, source,
private inputs, process/recovery, build e artifact/publish/cleanup estão separados;
o próximo domínio é MainActivity/UI.
