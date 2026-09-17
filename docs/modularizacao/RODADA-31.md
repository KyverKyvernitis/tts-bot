# Rodada 31 — self-builder Chaquopy: toolchain e preflight

Entrada: checkpoint da rodada 30, 603 arquivos. Foram relidos o handoff portátil,
`docs/02-contratos-invariantes.md`, `docs/03-mapa-e-ordem-do-trabalho.md` e
`docs/12-self-builder-chaquopy.md` da referência da rodada 28. A fonte editada é
somente o checkpoint 30; patches antigos não foram reaplicados.

## Caracterização

A baseline dos cinco arquivos de testes já ligados ao self-builder passou com
151 testes. Um teste novo preserva literalmente as quatro assinaturas públicas
Chaquopy. Dois casos novos de manifesto corrompido foram escritos antes da
correção e produziram 2 failures/1 pass: conversão de `jdkMajor` inválido escapava
com `ValueError` e `requiredSmokeChecks` não-string escapava com `TypeError`.

## Extração

Foi criado `coreworker/builder/` com `__init__.py`, `toolchain.py` e
`preflight.py`. O facade continua físico no caminho antigo e mantém wrappers com
os mesmos nomes privados usados pelos testes/integrações. Toolchain recebe
bindings de JSON/paths/hash; smoke recebe fingerprint, ambiente, runner, clock e
persistência por chamada. Preflight recebe resolve/smoke/persistência por chamada.
Resource preflight também recebe os helpers vivos do facade.

O facade caiu de 1.958 para 1.579 linhas. `toolchain.py` tem 318 linhas e
`preflight.py` 220. Não há estado global de runtime nos módulos novos. Source,
processos Gradle, locks/ownership, recovery, build, publish e cleanup não foram
movidos nesta rodada.

A validação v2 agora trata tipos corruptos como checks inválidos em vez de
exceção. Uma regressão intermediária tornou `requiredSmokeChecks` obrigatório no
schema v1; o teste existente de launcher compacto detectou a quebra. A condição
foi restaurada: v1 mantém o contrato anterior e v2 exige exatamente os cinco
smokes. Dois testes textuais antigos foram ajustados para inspecionar facade +
módulos extraídos, mantendo as mesmas asserções de segurança.

## Gates executados na fonte

- Caracterização nova antes de corrigir: 1 passed, 2 failed.
- Novo teste após correção: 3 passed.
- Self-builder/recovery/Termux/cleanup + teste novo: 154 passed.
- Família `test_core_worker*.py` + identidade + teste novo: 218 passed,
  85 skipped. Os skips são os gates Java sem JARs disponíveis neste host.
- Cobertura ampla, em três lotes e excluindo apenas os três coletores de chatbot
  bloqueados por `discord` e telemetria isolada: 244 passed/86 skipped; 340 passed;
  562 passed/9 subtests passed.
- Suíte global sem exclusões: mesmos 3 erros de coleta da rodada 30
  (`test_chatbot_imagegen.py`, `test_chatbot_reply_routing.py`,
  `test_chatbot_v2.py`) por `ModuleNotFoundError: discord`, mais 1 skip.
- `test_phone_worker_telemetry.py`: 57 passed/54 teardown errors na rodada 31 e
  exatamente 57/54 no checkpoint 30; fixture antigo de `Path.glob` não aceita os
  kwargs de Python 3.13. Não houve relaxamento.
- `compileall` do pacote `coreworker` e do teste novo: exit 0.

`ruff` continua ausente. Java 21 existe, mas os JARs de gate não estão no pacote;
não registrar lint ou Java como aprovados. Nenhum Gradle/APK real, rede externa,
publicação, instalação em aparelho ou job remoto foi executado.

## Fechamento e próximo corte

O patch preliminar 30→31 foi aplicado com `git apply --check` e `git apply` sobre
uma extração limpa do checkpoint 30. A reconstrução produziu 609 arquivos, todos
idênticos à fonte em caminhos, bytes e modos. `compileall` passou; o gate dirigido
repetiu 154 passes e a família Core Worker repetiu 218 passes/85 skips. O patch
final deve repetir essa equivalência depois deste registro; hashes dos artefatos
ficam no relato externo para evitar autorreferência.

Após fechar o artefato, continuar o self-builder pela ordem do handoff: source +
arquivos privados/hidratação; depois ownership/processo Gradle + recovery;
build/publish/cleanup. MainActivity/UI vem depois do self-builder.
