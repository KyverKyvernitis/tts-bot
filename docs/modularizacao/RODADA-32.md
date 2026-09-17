# Rodada 32 — self-builder Chaquopy: source, ZIP, privados e hidratação

Entrada: checkpoint da rodada 31, 609 arquivos. O corte segue o inventário e a
ordem já registrados: source/download/ZIP e arquivos privados/hidratação. A API
Java→Chaquopy pública continua em `coreworker.apk_self_builder`; nenhuma mudança
foi feita em ownership de processo Gradle, recovery, build, publish ou cleanup.

## Caracterização antes da correção

O baseline dos dez arquivos ligados ao self-builder passou com 197 testes. Seis
cenários novos foram adicionados sem alterar produção e falharam os seis:

1. um membro inseguro no fim do ZIP deixava membros anteriores já extraídos;
2. `app\\build.gradle` e `app/build.gradle` podiam colidir após normalização e o
   segundo membro sobrescrevia o primeiro;
3. falha tardia de hash da keystore deixava `google-services.json` já escrito;
4. newline em alias/senha podia injetar outra linha no properties de assinatura;
5. `repro-assets` seguia symlink de arquivo e podia copiar conteúdo de fora;
6. hidratação comparava apenas tamanho e mantinha arquivo antigo quando o conteúdo
   mudava sem mudar o número de bytes.

Todos usam arquivos/ZIPs locais controlados. Não houve download real, Gradle,
assinatura, instalação, publicação nem job remoto.

## Extração e correções

Foram criados `coreworker/builder/source.py` e
`coreworker/builder/private_files.py`. Ambos são sem estado mutável de runtime e
não iniciam rede, subprocesso, thread ou IO no import. O facade mantém wrappers e
passa bindings vivos onde os testes/integrações já monkeypatchavam IO.

`source.py` concentra download autenticado, validação/extração ZIP e descoberta do
projeto. Retry continua limitado por `SOURCE_DOWNLOAD_ATTEMPTS=3`; hash mismatch
continua determinístico e sem retry. O ZIP agora é validado por inteiro antes da
primeira escrita e recusa colisões de nome depois de normalizar `\\` para `/`, além
das travas anteriores de traversal, symlink, contagem e bytes expandidos.

`private_files.py` valida google-services, keystore e propriedades de assinatura
antes da primeira gravação privada. CR/LF/NUL em valores de properties são
recusados. Os três arquivos privados recebem modo 0600. Hidratação ignora symlinks
em native/repro assets e, quando tamanho é igual, compara conteúdo por SHA-256 para
não conservar bytes antigos. A remoção dos assets de toolchain legado continua.

O facade caiu de 1.579 para 1.341 linhas. `source.py` tem 208 linhas e
`private_files.py` 159. Limites de source, quantidade de membros, expansão, hashes,
mesma origem, nomes de arquivos, package `dev.core.worker`, allowlist de libs,
formato do resultado e comportamento do build não foram redesenhados.

## Gates na fonte

- Caracterização nova na entrada: 6 failed.
- Novos cenários após corrigir/extrair: 6 passed.
- Self-builder e regressões relacionadas: 203 passed.
- Família `test_core_worker*.py` mais os testes de modularização/source: 218 passed,
  85 skipped; os skips são os gates Java sem os JARs exigidos neste host.
- Suíte global sem exclusões: os mesmos 3 erros de coleta por ausência de `discord`
  (`test_chatbot_imagegen.py`, `test_chatbot_reply_routing.py`, `test_chatbot_v2.py`)
  e 1 skip de coleta.
- Cobertura ampla executável, excluindo somente esses três coletores e telemetria
  isolada: 333 passed + 9 subtests; 103 passed/76 skipped; 258 passed;
  183 passed/10 skipped; 162 passed; 113 passed.
- `test_phone_worker_telemetry.py`: 57 passed/54 teardown errors na rodada 32 e
  exatamente 57/54 no checkpoint 31, pelo mesmo monkeypatch de `Path.glob`
  incompatível com Python 3.13.
- `compileall` do pacote `coreworker` e do teste novo: exit 0.

`ruff` não está instalado. Java está presente, mas os JARs do gate obrigatório não
estão no pacote; não registrar lint/Java como aprovados.

## Fechamento por reconstrução

Um patch preliminar 31→32 foi gerado com os nove caminhos de escopo, validado por
`git diff --check`, aceito por `git apply --check` e aplicado sobre uma extração
limpa do checkpoint 31. A reconstrução produziu 613 arquivos, todos idênticos à
fonte congelada em caminhos, bytes e modos. `compileall` passou; o gate relacionado
repetiu 203 passes e a família Core Worker repetiu 218 passes/85 skips. O patch
final deve repetir a mesma equivalência depois deste registro; hashes dos artefatos
ficam no relato externo para evitar autorreferência.

## Próximo corte

Mover ownership/processo Gradle + recovery como um contrato único, preservando
PID/start ticks/PGID/job/attempt, lock, sinais, cancelamento e reconciliação. Depois
fechar build/publish/cleanup. MainActivity/UI e discovery/release continuam depois
do self-builder.
