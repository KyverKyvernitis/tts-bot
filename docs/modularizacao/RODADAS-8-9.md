# Continuação — configuração e distribuição modular

Entrada: commit `a60709d66bb523214d1635cf6d448b031230cc6f`, SHA-256 do ZIP de código
`6c4c5f379ec171e864532af1b9f3d5b1504b41ea7f5dbf97553267626931fec0`.
Os 554 arquivos foram conferidos por hash e modo antes das edições; Git estava
limpo. A skill de retomada não estava disponível na sessão e o próximo passo dos
documentos foi seguido. O ambiente Python foi verificado novamente. Os dois JARs
Android locais estavam truncados; foram restaurados a partir do artefato Maven
fixado e seus hashes foram conferidos antes do gate Java.

## Rodada 8 — configuração

Código: `phone_worker_runtime/config.py` e `__init__.py`; wrappers/loader e leitores
em `phone_worker.py`. O módulo novo tem validação, formatter e composição de
linhas puros. IO/env continuam no facade. O loader resolve a cópia ao lado do
entrypoint, usa lock, não importa novamente o runtime e permite retry quando os
arquivos do segundo estágio chegam. O parser inicial continua disponível no
entrypoint isolado. `render_env_lines` recebe o formatter atual, preservando
substituições posteriores usadas por consumidores/testes.

Os testes revelaram que a formatação antiga expandia `$`, executava substituição
por crases e permitia que `;` criasse outra atribuição ao carregar o arquivo em
Bash. Também retirava aspas literais no leitor Python. A correção mantém o texto
literal para esses casos e preserva leitura de JSON legado. O teste usa somente
probes locais controlados; nenhum comando de produção ou remoto é executado.

Evidências: `rodada8-config-entrada` teve um erro de fixture (ensure_ascii no
formatter substituído) e cinco falhas reais. Depois de corrigir a fixture,
`rodada8-config-vermelho`: 5 failed / 13 passed. Após a implementação,
`rodada8-config`: 43 passed (configuração/fronteiras/boot mínimo). A cobertura de
configuração cresceu depois para 21 casos. Próximo passo da rodada: distribuir
os módulos e testar o pacote completo no bootstrap antigo.

## Rodada 9 — release local e compatibilidade

Código: listas de arquivos no publisher e no runtime; validação de paths do
publisher; limites 64 membros, 8 MiB comprimidos e 32 MiB expandidos. O manifesto
interno conta no orçamento. O publisher recusa links e ancestrais simbólicos,
nomes NUL e colisão com seu manifesto. Em falha de tamanho/escrita de ZIP, remove
temporário e mantém a release/manifesto anteriores. O algoritmo de hash não mudou.

`tests/fixtures/phone_worker_bootstrap_1_0_0.py` é a cópia exata da base original,
guardada por SHA-256 no teste. O publisher real monta uma fixture completa em
`tmp_path`; o extrator antigo valida hashes, conjunto, Python/shell e modos.
O ZIP tem 25 arquivos mais um manifesto (26 membros). O entrypoint extraído
importa e executa `--help` em subprocesso sem site-packages. Editar somente o
módulo novo muda o hash reportado e o ZIP; módulos ausentes impedem preparação.

Evidências: `rodada9-release-entrada`: 5 failed / 4 passed (links e limites não
eram recusados). Após a correção, `rodada9-release`: 70 passed na seleção de
configuração, release, fronteiras, boot mínimo e transporte TTS. Duas regressões
adicionais de manifesto reservado/NUL falharam antes da correção;
`rodada9-nomes`: os 11 testes de release passaram. Os comandos completos e hashes
de cada fotografia estão nos JSONs homônimos do pacote, sem reescrever os logs.

## Fechamento e limites

O gate final é `rodada9-suite-final`; lint e compileall são registrados à parte.
O relatório externo associa o resultado ao inventário da entrega. Antes desse
gate, atualizar o inventário AST para não entregar offsets antigos. Java de
produção, `WorkerHandler`, transporte TTS, Music Agent, self-builder e bootstrap
de produção permaneceram iguais ao checkpoint de entrada desta continuação.

Não houve Gradle/APK, execução em aparelho nem medição de latência real. A fonte
Coptic ausente e o skip de `/proc` já estavam classificados no checkpoint anterior.
O teste de extração original não substitui instalação, validação de processo ou
rollback em aparelho. Discovery dinâmico continua pendente; nesta primeira etapa
há listas explícitas concordantes e verificação de todos os arquivos selecionados.

Próximo passo: testes de bateria/sysfs com permissões/campos parciais/temperatura,
seguidos da primeira extração de telemetry com probes explícitos e fallback de
startup. Preservar VPS de 1 GB sem worker e manter probes fora do áudio. Publicação,
instalação em aparelho e envio de jobs remotos permanecem fora da retomada.
