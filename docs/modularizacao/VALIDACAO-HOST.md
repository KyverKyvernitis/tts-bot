# Validação de desenvolvimento

O pacote de continuidade inclui registros por rodada, comandos completos,
hashes dos fontes testados e logs em `evidencias/retomada-20260910/`. Esses
registros ficam fora da release de dispositivo. O estado inicial testado foi
`estado-em-trabalho.zip` com SHA-256
`ec6af6d03155026c98a6d01da7cb0b211db436badc240376f27abf035ccc856f`.
As rodadas 8–9 partem do checkpoint `a60709d`, com registro em
`RODADAS-8-9.md` e evidências novas separadas no pacote de continuidade.
As rodadas 10–13 partem de `4dd8e15`, registradas em `RODADAS-10-13.md`;
os logs novos ficam em `evidencias/rodadas-10-13/` no pacote externo.
As rodadas 14–16 partem de `e86e4d2`, registradas em `RODADAS-14-16.md`;
os gates finais e a reconstrução limpa pelo patch ficam em
`evidencias/rodadas-14-16/` no pacote externo.
As rodadas 17–20 partem de `cb70bb2`, registradas em `RODADAS-17-20.md`;
gates gerais, compatibilidade e reconstrução ficam em `evidencias/rodadas-17-20/`.
As rodadas 21–22 partem de `87e5752`, registradas em `RODADAS-21-22.md`;
inventário TTS, contratos, gates e reconstrução ficam em `evidencias/rodadas-21-22/`.
As rodadas 23–24 partem de `f763aa6`, registradas em `RODADAS-23-24.md`;
cache/Android, comparação AST e reconstrução ficam em `evidencias/rodadas-23-24/`.

## Repetir os testes

Preparar Python com as dependências de testes e Java 17. O harness não baixa
dependências durante pytest. Usar os JARs identificados em
`suporte-validacao/dependencias-retomada.json` do pacote; exportar seus caminhos
absolutos nas variáveis abaixo:

```bash
export CORE_WORKER_ECJ_JAR=/caminho/ecj.jar
export CORE_WORKER_JSON_JAR=/caminho/json.jar
export CORE_WORKER_ANDROID_API_JAR=/caminho/android-api.jar
export CORE_WORKER_REQUIRE_JAVA_TESTS=1
python -m pytest tests/test_core_worker_java_behavior.py tests/test_core_worker_java_integration.py -q --tb=short
python -m pytest tests/test_core_worker_python_boundaries.py tests/test_core_worker_tts_benchmark.py tests/test_tts_worker_streaming.py -q --tb=short
python -m pytest tests/test_phone_worker_config.py tests/test_phone_worker_modular_release.py -q --tb=short
python -m pytest tests/test_phone_worker_telemetry.py tests/test_phone_worker_network_telemetry.py -q --tb=short
python -m pytest tests/test_phone_worker_control_plane.py tests/test_phone_worker_http_clients.py tests/test_phone_worker_modular_release.py -q --tb=short
python -m pytest tests/test_phone_worker_voice_state.py tests/test_phone_worker_voice_probe.py tests/test_phone_worker_pcm_lifecycle.py -q --tb=short
python -m pytest tests/test_phone_worker_tts_policy.py tests/test_phone_worker_tts_lifecycle.py tests/test_phone_worker_modular_release.py -q --tb=short
python -m pytest tests/test_phone_worker_tts_cache_io.py tests/test_phone_worker_tts_android.py tests/test_phone_worker_tts_services.py -q --tb=short
python -m pytest tests -q --tb=short -ra
```

Se houver `javac`, as fixtures o preferem e usam `--release 17`; ECJ é a
alternativa usada nesta sessão. `CORE_WORKER_REQUIRE_JAVA_TESTS=1` transforma
dependência Java ausente em falha, em vez de ocultar todos os cenários como skip.

## Escopo das evidências

- O harness básico executa 73 cenários de helpers reais e três TARs produzidos
  independentemente por `tarfile` (USTAR, GNU e PAX). Inclui falhas de persistência,
  promoção/rollback, subprocessos reais e chamadas JNI simuladas não cooperativas.
- O harness de integração executa nove cenários com dispatcher/domínios Direct,
  catálogo, builder e serviços reais. Android, Chaquopy, notificações e engine TTS
  são fronteiras controladas em `tests/java_integration`; não são instrumentação.
- Há seis testes Python de import, startup, estado vivo e lazy loading. Os
  contratos TTS existentes também são executados. Dois novos testes protegem a
  correção de NameError no benchmark Android Native e seus parâmetros/cache. O
  caminho de síntese normal não foi refeito nesta rodada.
- `tests/conftest.py` isola o diretório de APK releases e detecta mudanças em
  `data/` e em releases após cada teste. Os testes que persistem pending apontam
  explicitamente para `tmp_path`. O manifesto fictício 0.7.4/122 foi removido.
- Há 21 testes de configuração e 27 de release modular. Testam quoting literal
  em Bash/leitores Python, bindings tardios, import puro, carga concorrente e
  chegada tardia do módulo. A fixture do bootstrap 1.0.0 é uma cópia da base com
  hash fixado; extrai o pacote completo gerado pelo publisher em `tmp_path`.
  Symlinks, ausência de módulos, colisão com manifesto, nomes NUL e os três
  limites do bootstrap são recusados antes de substituir o target local.
- Há 57 testes de bateria/consumo/extração e 19 de rede. Leitores reais atuam em
  arquivos temporários; clocks, comandos, HTTP e conexões são controlados. Testam
  campos parciais, permissões, unidades, erros, bindings, TTL/cache, timeouts,
  fechamento de recursos e chegada tardia do módulo. Nenhum deles usa a VPS real.
- Há 41 testes de plano de controle e 41 de HTTP: payload integral, autenticação,
  env de pareamento, estado vivo, recuperação com módulo ausente/incompleto,
  registry local, corpo/headers/limites/fechamento e limpeza de download. Rede e
  respostas remotas são controladas em memória; nenhum job é enviado.
- Há 48 testes de Voice State, 18 de probes e 13 de PCM. Cobrem contratos públicos,
  persistência/privacidade, TTL/posse, flags, estado vivo e cancelamento cooperativo.
  O coroutine de probe real usa WS/UDP em memória; PCM usa processo controlado,
  arquivos temporários e bytes conhecidos. Não medem Discord/ffmpeg ou latência real.
- Há 39 testes de ciclo TTS e 56 de política/bindings/startup. Quatro providers
  controlados, cache em arquivos temporários, raw/base64, falhas, manutenção e
  28 chaves capturadas da entrada. A carga da política é antecipada no main e
  não acrescenta import pesado; clocks determinísticos não medem latência real.

- Há 18 testes de cache com IO, 44 do adaptador Android e 17 dos serviços opcionais.
  Arquivos reais, flock, barreiras de publicação, servidor HTTP local e clocks
  controlados verificam bytes, limites e ordem. Antes de extrair Android, 46 passes
  incluindo os dois benchmarks. Após ambos os cortes, 230 passes na seleção
  integrada. Suíte geral/reconstruída, lint e sintaxe ficam no registro externo 26.
- Errata de evidência histórica: rodada21-tts-entrada tem exit 1, mas log incompleto
  sem resumo final. A contagem 14 failed/25 passed citada anteriormente não foi
  reconfirmada. O rótulo "exit 0" da tabela externa 25 era um fallback incorreto
  do gerador. Não usar essa linha como gate; os logs originais permanecem intactos.

- Rodadas 25–26 acrescentam 72 testes de providers Teto/Edge/gTTS. Renderer
  singleton/guard, contenção, retry, cancelamento de stream real asyncio com
  provider em memória, áudio parcial, limites e parâmetros foram caracterizados
  antes de mover os ramos. São 25 testes de carga dos serviços e 25 de release.
  Seleção integrada: 320 passes. Auditoria compara três corpos por AST, todos os
  estados/assinaturas e 581 arquivos da entrada fora do escopo. Os resultados dos
  gates finais e da reconstrução pelo patch estão no registro externo 27.

- Rodadas 27–28 acrescentam 19 testes de IO PCM, incluindo processos host reais
  com stderr saturado, timeout/recolhimento, concorrência e expiração/substituição
  durante preparo. São 33 testes de serviços e 27 de release. Entrada: 9 falhas/16
  passes; seleção após corrigir: 116 passes; após extrair: 176 passes, mais três
  casos de bindings/módulo ausente. O teste existente de identidade de cache
  detectou uma regressão intermediária, corrigida sem relaxar a asserção.
  Auditoria de AST e 585 arquivos da entrada fora do escopo; gates finais e
  reconstrução documentados no registro externo 28. Nenhum ffmpeg/rede real.

A compilação auxiliar de todos os fontes Java usa as assinaturas de
`suporte-validacao/assinaturas-java`, fora de produção. Ela verifica tipos e
sintaxe, sem Gradle, resources, D8, Manifest merger, empacotamento ou assinatura.

## Limites que permanecem

O pacote recebido não contém
`cogs/games/assets/fonts/NotoSansCoptic-Regular.ttf`. O teste
`test_complete_profile_name_is_not_cut_when_it_fits` falha também na base original,
com as mesmas dependências. Não foi enfraquecido nem marcado como skip. Um teste
de ownership de processo pode pular quando o namespace não expõe o filho em
`/proc`; o relatório final identifica o caso e o resultado efetivo.

Falta executar Gradle/instrumentação com o material real do projeto e medir TTS
no mesmo aparelho/engine. O host não demonstra durabilidade contra perda física
de energia nem JNI nativo real. As janelas de rollback simuladas e as pendências
TC01–TC07 estão discriminadas no registro da retomada. Não usar o resultado do
harness como aprovação para instalar ou publicar.

- Rodada 29 adiciona testes isolados do lifecycle do Music Agent e uma unidade sem estado em `music_agent_runtime/lifecycle.py`. Gate dirigido máximo nesta sessão: 62 passed (lifecycle, release modular, boundaries e TTS streaming); compileall passou. A suíte completa está bloqueada no host por ausência de `discord` (mesmos 3 erros de coleta na rodada 28 limpa). `ruff` e os JARs do gate Java não estão disponíveis; tentativa de instalação Python falhou sem rede. Não registrar esses gates como aprovados até repeti-los no ambiente de validação completo.

- Rodada 30 acrescenta 10 testes de transições/lifecycle do Music Agent. Sete
  cenários foram vermelhos na entrada: pool Lavalink concorrente, conexão tardia
  após stop, TrackEnd obsoleto, cleanup HTTP, shutdown, cancelamento TTS direto e
  ducking com overlays concorrentes. Gate dirigido final da fonte: 72 passed.
  `compileall` passou. A suíte global segue bloqueada por três imports de `discord`.
  Em execução segmentada, os lotes fora de telemetria deram 249 passed/86 skipped,
  332 passed e 562 passed/9 subtests; telemetria deu 57 passed/54 teardown errors
  tanto na rodada 30 quanto no checkpoint 29 limpo por incompatibilidade do fixture
  `Path.glob` com Python 3.13. Java obrigatório continua sem JARs (85 skipped) e
  `ruff` não está instalado; nenhum desses dois gates foi convertido em aprovação.

- Rodada 31 iniciou o self-builder Chaquopy: 154 testes dirigidos passam após
  extrair toolchain/preflight; família Core Worker soma 218 passes/85 skips neste
  host. Dois manifests corrompidos foram reproduzidos antes da correção. A suíte
  ampla executável passou em três lotes (244/86, 340, 562 + 9 subtests). Os três
  coletores sem `discord` e a telemetria Python 3.13 mantêm exatamente os bloqueios
  da rodada 30. Reconstrução/hashes finais ficam no registro externo da rodada 31.

- Rodada 32 separa source/ZIP e private inputs/hidratação do self-builder em dois
  módulos sem estado. Seis cenários foram vermelhos antes da correção e passam após
  o corte: escrita parcial em ZIP inválido, colisão normalizada, private file antes
  de validação completa, newline em properties, symlink de repro-assets e stale
  file com mesmo tamanho. Gate relacionado: 203 passes; família Core Worker:
  218 passes/85 skips. Cobertura ampla executável passou em seis lotes
  (333 + 9 subtests; 103/76; 258; 183/10; 162; 113). A suíte global mantém somente
  os três coletores sem `discord`; telemetria repete 57 passes/54 teardown errors
  na rodada 32 e no checkpoint 31. `compileall` passou; Java obrigatório e `ruff`
  continuam não aprovados neste host. Reconstrução preliminar pelo patch: 613/613
  arquivos idênticos em bytes/modos, 203 passes no gate relacionado e 218/85 na
  família Core Worker. Nenhum download/Gradle/publicação real.

- Rodada 33 separa ownership/processo Gradle/recovery em
  `coreworker/builder/process.py`. Sete cenários caracterizam a fronteira: seis
  falham na entrada e um já passa; após a correção são 7 passes. Família Core
  Worker + modularização/source/process: 225 passes/85 skips, exatamente +7 passes
  sobre o perfil da rodada 32 e mesmos 85 skips Java. Cobertura ampla executável:
  278; 344; 223/77; 314/9 + 9 subtests. Suíte global mantém os mesmos três imports
  de `discord` ausentes + 1 skip tanto na rodada 33 quanto no checkpoint 32.
  Telemetria repete 57 passes/54 teardown errors nos dois estados. `compileall`
  passa; Java 21 está presente mas sem os JARs do gate e `ruff` segue ausente.
  Nenhum Gradle/APK, publicação, instalação ou job remoto real foi executado.

- Rodada 34 fecha build/artifact/publish/cleanup do self-builder em dois módulos sem
  estado. Entrada: 5 failures/1 pass; após correção: 6 passes. Gate relacionado:
  58 passes; família Core Worker+self-builder: 231 passes/85 skips, exatamente +6
  passes e nenhum skip novo sobre a rodada 33. Cobertura ampla executável em quatro
  lotes: 317/9 + 9 subtests; 292; 333; 223/77. Suíte global conserva os mesmos três
  imports de `discord` ausentes + 1 skip; telemetria conserva 57 passes/54 teardown
  errors nos dois estados. `compileall` passa. Java 21 segue sem os JARs do gate e
  `ruff` ausente. Reconstrução preliminar: 620/620 arquivos idênticos, 58 passes
  no gate relacionado e 231/85 na repetição final da família; uma flutuação inicial
  do cenário real de `/proc` passou isoladamente em fonte e reconstrução. Nenhum
  Gradle/APK, download/publicação real, instalação ou job remoto foi executado.
