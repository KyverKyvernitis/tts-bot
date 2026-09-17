# Estado Python — configuração, telemetria, controle, voz e serviços TTS

Checkpoint de desenvolvimento de 14/09/2026. O inventário em
`inventario-phone-worker.json` foi produzido por AST a partir do arquivo indicado
e contém seu SHA-256, 96 atribuições globais e 324 funções do facade. Ele não
executa o runtime. Music Agent e self-builder ainda não foram separados.

## Donos e contratos atuais

O dono dos globais de Phone Worker continua sendo a única instância do facade
composta por processo. `phone_worker_runtime/config.py` contém somente funções
puras: validação de chave, formatação de valor e composição de linhas. O facade
retém ambiente e estado do runtime. `telemetry.py` recebe os probes de bateria,
rede e Tailscale e o cache de ping do facade. Não tem cache/lock ou serviços próprios.
A modularização Python continua em andamento.

`control_plane.py` compõe o payload a partir de dados explícitos. O facade mantém
um envelope mínimo de recuperação e todos os donos de estado/locks. O loader lazy
tem uma instância por facade; falhas de carga permitem nova tentativa. Não há
import do facade, rede ou probes dentro desse módulo.

| Estado | Dono atual | Regra para a próxima extração |
| --- | --- | --- |
| Jobs, outbox, contadores e `_CORE_JOB_LOCK` | `phone_worker.py` | HTTP, heartbeat e polling recebem o mesmo contexto de jobs; snapshots consultam o mapa vivo e a mesma exclusão |
| `_APK_BUILD_THREAD_LOCK`, `_HEAVY_RESOURCE_LOCK` | `phone_worker.py` | Build, manutenção e TTS reutilizam as mesmas instâncias; não recriar locks no domínio |
| Renderer Teto, erro e lock | `phone_worker.py` | Um renderer lazy por processo, incluindo erro/cooldown compartilhados |
| Transporte, fila, contadores e manutenção TTS | `phone_worker.py` | Não mover coleta/varredura para a resposta; manter executor e locks existentes |
| Instalação de dependências e Lavalink | `phone_worker.py` | Probes e autoinstall continuam opcionais, lazy e com cooldown único |
| Streams PCM e Voice Agent | `phone_worker.py` | Leituras, expiração, cancelamento e gravações usam o mesmo mapa/lock de cada domínio |
| Porta efetiva, estado HTTP, último heartbeat e rede | `phone_worker.py` | Atribuições posteriores permanecem visíveis a status e heartbeat; nenhum `from state import ESCALAR` |
| Cache/hash de release | `phone_worker.py` | Invalidação acompanha os arquivos realmente instalados e o conjunto do publisher |
| Módulo puro de configuração e lock de carregamento | `phone_worker.py` | Uma instância por facade; resolução na pasta do entrypoint; falha de carga não fica em cache |
| Módulo de telemetria e lock de carregamento | `phone_worker.py` | Uma instância por facade, carga lazy, retry após chegada tardia/falha; wrappers mantêm dados indisponíveis quando o módulo falta |
| `_PING_CACHE` | `phone_worker.py` | O módulo recebe o mapa vivo em cada chamada; preserva TTL, chave host/porta e cópias de resultado; concorrência do cache não foi redesenhada |
| Módulo de payload de controle e lock de carga | `phone_worker.py` | Loader lazy com retry; snapshots de jobs/rede e escalares são lidos a cada payload; nenhum loop/estado foi transferido |
| Módulo puro Voice State e lock de carga | `phone_worker.py` | Nove funções recebem dados/clock/callbacks; mapas de sessão/handoff/transfer/conexão e persistência permanecem no facade |
| Geração de probe | Registro em `_VOICE_AGENT_CONNECTION_MEMORY` | Geração privada por agendamento, validada sob o RLock existente; clear/release/expiração/replacement impedem writes obsoletos; sem mapa/lock duplicado |
| Módulo puro de política TTS e lock de carga | `phone_worker.py` | Doze funções sem estado/IO recebem parâmetros, preferência e callbacks vivos; preload opcional no startup antes de jobs/HTTP; retry se o arquivo chegar depois |

Na composição futura, cada domínio recebe explicitamente suas dependências.
Domínios não importam `phone_worker.py`: quando o arquivo roda como `__main__`,
esse import criaria outro runtime. Até a migração deliberada do contrato, wrappers
do facade resolvem seus bindings no momento da chamada. Não capturar funções
substituíveis em argumentos default, aliases de import ou closures de startup.

As atribuições diretas a `_EFFECTIVE_HTTP_PORT`, `_DIRECT_HTTP_STATE` e
`_PENDING_CORE_JOB_RESULTS` são pontos observados pelos testes existentes. A
primeira extração deve preservá-los, ou migrar seus consumidores com testes de
comportamento e uma API explícita. Não sincronizar cópias de escalares nem usar
`globals().update`, wildcard ou `exec` de fragmentos para contornar esse vínculo.

## Mudança executada

Importar Phone Worker escrevia configuração privada de Music Agent e podia gerar
um token. A chamada de `_load_phone_worker_runtime_env()` saiu do nível de módulo
e passou ao começo de `main()`, antes do carregamento complementar de env. O
startup explícito preserva precedência, porta configurada e persistência do token
com modo 0600. Não há inicializador global que leia `getenv` depois dessa chamada.

O teste em subprocesso impede writes, criação de threads, conexão/bind e processos
durante import. Outros testes verificam o startup explícito, referências vivas de
jobs/locks, reatribuição de escalares, leitura tardia de ambiente e uma única
instância do transporte TTS sob concorrência. O executor de manutenção pode ser
construído no import, mas nenhuma thread é iniciada.

O lint encontrou ainda um `requested_engine` indefinido no benchmark Android
Native, também presente na base. Removemos desse caminho o bloco de substituição
de fallback: o benchmark já seleciona uma única engine. Dois testes demonstraram
o NameError antes da correção e verificam uma delegação, parâmetros, chave de cache
e bytes retornados depois dela, para `android_native` e `android-native`.

Na primeira retomada, a comparação AST contra o estado recebido mostrou mudanças apenas em `main` e
`WorkerHandler._task_tts_synthesize_benchmark`. A chamada no nível do módulo
foi removida. Síntese normal, PCM, cache keys, engines, pitch/rate e transporte
permanecem iguais. Isso não mede latência no aparelho; a comparação
frio/quente/primeiro áudio permanece pendente.

Na continuação (rodadas 8–9), a classe `WorkerHandler` inteira permaneceu igual ao
checkpoint `a60709d`. A extração altera os helpers de configuração e acrescenta
um loader lazy com lock. `render_env_lines` recebe o formatter na chamada: tanto
reatribuir o helper do facade quanto substituir a função do módulo puro continua
visível ao consumidor. Nenhum domínio importa o facade de volta.

A gravação de env também foi corrigida: `;` passa a exigir aspas, `$` e crases são
escapados dentro delas. Os leitores Python entendem as novas sequências e o JSON
legado, sem retirar aspas que pertencem ao valor. O leitor inicial permanece no
facade para que a chegada tardia dos módulos não impeça carregar configuração ou
iniciar o control plane. A edição de configuração requer o segundo estágio.

Os dois arquivos novos entram nas listas explícitas do runtime e do publisher,
com modo 0644. O hash conserva o algoritmo path/NUL/SHA/newline. Um ZIP completo
com 26 membros foi aceito pelo extrator original 1.0.0; imports e `--help` foram
executados da release extraída com site-packages desativado. Alterar apenas
`config.py` muda o hash no publisher e no runtime, incluindo invalidação do cache.

## Rodadas 10–13

Bateria/sysfs e rede foram caracterizados antes de mover o código. A temperatura
sysfs agora converte décimos de °C em toda a faixa, incluindo negativos. Erro ao
enumerar BAT* não descarta candidatos já encontrados. Respostas Termux sem medida
útil usam fallback; temperaturas não finitas são omitidas. Uma resposta de erro
de Wi-Fi também segue o fallback, em vez de anunciar Wi-Fi disponível.

`telemetry.py` contém cinco funções: bateria, sysfs, ping TCP, Tailscale e composição
de rede. Todas recebem probes/callbacks explícitos na chamada. O cache de ping fica
no facade, incluindo quando seu binding é substituído. Os timeouts, TTL, fechamento
de conexões e sondagem HTTP opt-in foram preservados. A inferência antiga de VPN
pela URL configurada continua uma inferência; não comprova uma conexão real.

O consumidor `_assist_readiness_snapshot` foi corrigido depois dos testes: nível
zero não é mais substituído pelo alias nem vira medida ausente; o booleano charging
tem prioridade, e strings unplugged/battery não significam carregamento. A seleção
de tarefas leves e o comportamento sem medida continuam os mesmos.

Há 57 casos de bateria/consumo/bindings/chegada tardia e 19 de rede. Os testes de
release agora incluem telemetry.py: 26 arquivos mais manifesto, 27 membros aceitos
pelo extrator original. A classe WorkerHandler, main, transporte TTS e os fontes
Java não foram editados. O gate Teto recebe a unidade corrigida; não houve nova
coleta no áudio, alteração de engine ou medição de latência em aparelho.

## Rodadas 14–16

Autenticação/flags, identidade de pareamento e payload foram caracterizados antes
da separação. URL normalizada vazia, IPv6 sem colchetes, mutação de listas de
capabilities, respostas opcionais não objeto, ID físico incorreto no pareamento
e alteração prematura de env foram corrigidos. A fixture JSON integral foi
capturada do monólito corrigido e continua igual após extrair o enriquecimento de
payload. Roles/capabilities solicitadas persistem somente após pareamento válido.

O envelope de recuperação permanece no facade para o bootstrap de um arquivo.
Sem o módulo, não coleta os probes opcionais, mantém identidade, tarefas de
recuperação, jobs/rede, porta efetiva e control_plane_alive. O registry local
aceita o heartbeat e preserva medidas omitidas. `health.payload_mode=bootstrap`
desaparece quando o módulo chega e o payload completo volta a ser enviado.
Capturar a porta efetiva uma vez evita misturar portas no mesmo payload; a próxima
chamada observa reatribuições. Safe mode e limites de roles/capabilities valem
também durante recuperação. Loops, timers, threads, locks e outbox não mudaram.

O módulo entra nas allowlists/hash do runtime e publisher: 27 arquivos e manifesto,
28 membros no ZIP de release local. O bootstrap original 1.0.0 aceitou a release;
hash, import da pasta extraída e preservação da release anterior foram testados.
Clientes JSON/download continuam no facade para a recuperação mínima. Agora
fecham HTTPError e limpam temporários mesmo quando a leitura do erro falha.

O registro `RODADAS-14-16.md` discrimina os testes executados e o próximo passo:
caracterizar estado/cancelamento/expiração de Voice Agent antes da extração.
Java, Music, self-builder, bot/webserver, WorkerHandler, main e transporte dedicado
TTS foram preservados. A modularização completa e latência em aparelho continuam
pendentes; os gates consolidados estão no pacote externo de continuidade.

## Rodadas 17–20

`voice_state.py` tem nove funções puras: projeções públicas, seleção de expirados
e resumos. Dados, timestamp, parser e projeções são passados na chamada; o módulo
não possui IO, threads ou import do facade. Quatro payloads públicos integrais
foram capturados antes da extração e preservados. Loader é lazy, único e permite
retry; ausência/incompletude não impede o bootstrap mínimo de controle.

Transferências agora iniciam sem repetir kwargs, expiração revoga a permissão
temporária e flags textuais negativas não são confundidas com confirmação. Tempo
zero, ordenação de atualizados e pruning de entradas corrompidas foram corrigidos.
Sessões preservam metadados, modo 0600 e memória disponível se persistir falhar.
Handoff/credenciais não vão para JSON de sessão ou payload público.

Probes têm geração privada no registro já existente. Agendamento/duplicidade e
transições de posse usam o mesmo RLock. Respostas de gerações antigas não recriam
nem substituem conexões. Clear, release, expiração e substituição invalidam o
probe. Cancelamento é cooperativo; operações bloqueadas mantêm timeouts antigos.
Não foi criado executor, timer ou lock adicional para esse ciclo.

Registro/expiração e recursos PCM têm testes. Falhas de preparo limpam temporários;
streaming live fecha pipes e não envia uma segunda resposta após headers. Engines,
bytes, parâmetros PCM, prebuild e transporte TTS dedicado permanecem iguais. Em
WorkerHandler mudou somente a condição de confirmação no método Voice Agent TTS;
os outros métodos e main não mudaram. Não foi medida latência em aparelho.

A release local agora tem 28 arquivos e manifesto, 29 membros, aceitos pelo
bootstrap original 1.0.0. Voice State participa das duas allowlists/hash. O
registro `RODADAS-17-20.md` discrimina os testes e seus limites.

## Rodadas 21–22

`INVENTARIO-TTS.md` mapeia cache, admissão, contadores, manutenção e providers.
Aliases de engine não perdem a chave nem recebem parâmetros de fallback quando
representam a engine primária. OSError na leitura de cache opcional permite
sintetizar pela mesma engine; write/replace do cache JSON limpa temporário em
falha e preserva o arquivo anterior. O registro anterior cita
14 falhas, mas o log de entrada preservado está incompleto e não confirma esse
total. Ver a errata em RODADAS-23-24.md. Nenhuma engine, formato ou parâmetro foi
redesenhado por essas correções.

`tts_policy.py` contém doze funções puras em 156 linhas: normalização, engines,
ordem e política/chave de cache. O facade continua dono dos sete contadores/
escalares TTS, locks, executor, mapas de manutenção, arquivos e síntese. As 28
chaves da fixture conferem com o checkpoint 20 intacto. Wrappers preservam
assinaturas e callbacks reatribuídos. Env e fingerprint Teto continuam sendo
lidos pelo facade, somente no caso correspondente.

Main carrega opcionalmente a política antes de jobs/HTTP, para retirar o custo
de carregar seu arquivo da primeira síntese normal. Importar o facade não a
carrega; falha não é cacheada nem impede recuperação mínima. Carga concorrente
produz uma instância; chamadas quentes não leem o path nem adquirem lock de carga.
Chegada tardia ainda pode pagar o custo de carga uma vez; não se mediu latência
em aparelho. Providers e transporte mantêm seus timeouts e fluxo anteriores.

Há 39 testes de ciclo TTS e 56 de política/bindings/startup. Frio/quente, quatro
engines controladas, raw/base64, flags, fallback, contador, manutenção e falhas de
cache foram executados. A release com 30 membros inclui o módulo no hash/listas;
19 testes de distribuição cobrem extrator original 1.0.0 e import sem dependências.
Java, bot/VPS, Music, self-builder, bootstrap, transporte e Voice Agent não mudaram.
O registro `RODADAS-21-22.md` e o pacote externo discriminam resultados e limites.

## Rodadas 23–24

`tts_cache.py` concentra cinco operações de IO, com 18 testes específicos de
arquivos/locks/publicação. Dois bugs reproduzidos foram corrigidos: revalidar
inode e mtime impede apagar candidatos substituídos ou tocados depois do scan.
A poda continua por melhor esforço e fora da resposta. Touch/pending/executor,
lock e contadores continuam com um dono; o serviço recebe referências vivas.

`tts_android.py` concentra três funções: JSON HTTP, raw HTTP e ramo de síntese
raw→JSON. Seus corpos conferem por AST com a entrada após substituir bindings;
status/voices/cache Android e demais engines permanecem no facade. São 44 testes
de protocolo/erros/limites/flags/TTL/áudio e dois benchmarks existentes, executados
antes de extrair. HTTP é local, clocks controlados; não há medição em aparelho.
Timeouts por requisição raw/JSON foram preservados, sem novo orçamento agregado.

Os dois serviços têm 17 testes de carga concorrente, retry, import sem efeitos,
preload independente e bindings. Ambos entram nas allowlists/hash em 0644; são
31 arquivos e manifesto, 32 membros aceitos pelo bootstrap original, com 23 testes
de distribuição. Preload opcional antes de jobs/HTTP; o caminho quente não relê
o arquivo nem adquire o lock de carga. Não há dependências de produção novas.

A seleção integrada após extrair passou 230 testes. Gates e reconstrução final
ficam no pacote externo, sobre a fonte congelada. Ver RODADAS-23-24.md e
INVENTARIO-TTS-IO-ANDROID.md. Os 589 arquivos incluem o inventário atualizado;
Phone Worker tem 11.097 linhas. Java, VPS, Voice/PCM, Music, self-builder e
transporte dedicado TTS continuam iguais à entrada da rodada 22.

## Rodadas 25–26

`tts_providers.py` reúne os ramos Teto, Edge e gTTS em três funções/66 linhas sem
estado nem imports de providers ao carregar. Teto recebe o mesmo lock pesado,
getter do renderer, clock e formatter; singleton, erro e guard ficam no facade.
Os fallbacks Edge/gTTS recebem normalizadores e APIs IO/asyncio na chamada.
Transporte compartilhado mantém prioridade e código byte a byte igual.

Caracterização prévia: 39 passes Teto/renderer local; depois 70 casos de providers
antes de mover Edge/gTTS. Após extração e bindings quentes, são 72 testes de
providers, 25 de serviços opcionais e 25 de release. Seleção integrada: 320 passes,
zero mutações. Não houve bug novo reproduzido; três corpos iguais por AST após
substituir bindings. Um método do handler muda; main acrescenta um preload
opcional independente. As assinaturas, estados, cache e envelopes são preservados.

Release local tem 33 membros incluindo manifesto; extrator original 1.0.0 aceita
a árvore. O facade tem 11.085 linhas; Java, VPS, Voice/PCM, Music, self-builder,
bootstrap, renderer Teto e transporte TTS não mudaram. Limite dos fallbacks ainda
é verificado depois da síntese; timeouts e parâmetros não foram redesenhados.
Gates completos/reconstrução e hashes finais estão no registro externo 27.

## Rodadas 27–28

`pcm_io.py` reúne comando ffmpeg, preparo integral, envio de arquivo preparado e
stream live: quatro funções/189 linhas. Registry, RLock, TTL, coordenação por
stream e publicação com validação de dono ficam no facade. O mapa novo de preparos
é transitório e libera entradas após o último usuário. Serviços não importam o
facade nem possuem mapas, locks ou processos globais.

Nove cenários falharam na entrada: stderr saturado, preparos duplicados, temporário
ativo removido pela limpeza, publicação após expiração/substituição, resíduo após
promoção falha, Content-Length de outro arquivo e segunda resposta após erro de
leitura. Correções foram testadas antes da extração. PCM mantém comando/codec,
blocos e modo prepared por padrão; fallback live continua opt-in. Arquivo preparado
usa um descritor; erro após headers encerra a conexão.

São 19 testes novos de IO PCM, 13 de ciclo, 33 de serviços opcionais e 27 de release.
Seleções: 116 passes após corrigir, 176 após extrair, três adicionais de bindings/
módulo ausente. Gates gerais/reconstrução e hashes finais estão no registro externo
28. A auditoria confirma as quatro funções iguais ao facade corrigido após trocar
bindings e o callback de publicação; classe WorkerHandler inteira permanece igual
à entrada. O facade tem 10.983 linhas, com 96 atribuições globais e 324 funções.

A release local inclui pcm_io.py 0644 e tem 34 membros incluindo manifesto;
bootstrap original 1.0.0 aceita o conjunto. Loader único/lazy com retry e preload
opcional independente. Java, VPS, TTS de fala, Voice State/probes, Music Agent,
self-builder e bootstrap permanecem iguais à rodada 26. Latência real não medida.

## Próxima rodada concreta

1. Inventariar o ciclo async do Music Agent: criação/cancelamento de tarefas,
   disconnect/stop e posse dos callbacks antes de separar responsabilidades.
2. Caracterizar encerramento e efeitos tardios com fronteiras locais controladas;
   extrair a unidade sustentada pelos testes, preservando fila, áudio e estado.
3. Seguir self-builder Python e MainActivity/UI pela ordem do handoff; manter
   distribuição/hash, recuperação mínima e extrator original. Discovery seguro e
   gates Java/plataforma continuam pendentes.
4. PCM: stderr do preparo ainda é coletado em memória, expiração não interrompe
   proativamente o transcoder e limpeza de publicados é por melhor esforço.
   Não repetir IO PCM, serviços TTS e demais extrações já validadas.

Não houve publicação, instalação em aparelho ou envio de jobs remotos. As versões
continuam Phone Worker 1.11.6, bootstrap 1.0.0 e APK 0.8.6 / 133.

## Rodada 29 — lifecycle do Music Agent

O lifecycle mínimo agora possui `music_agent_runtime/lifecycle.py`, sem estado próprio. Remoção de timers/prefetch exige que a task no registry ainda seja a mesma task em `finally`; cleanup do player tenta stop e disconnect separadamente. `cmd_stop` despublica o player antes do await, e callback direto invalida a geração antes da transição, evitando avanço duplicado durante preparo assíncrono. Estados, filas, event loop, tokens e bindings Discord/Lavalink permanecem no facade.

Os testes novos são isolados de rede/Discord real. Gate dirigido aceito: 62 passes incluindo release/boundaries/TTS streaming. A suíte completa não pôde ser repetida neste host por dependências ausentes; a rodada 28 limpa falha na mesma coleta, portanto o bloqueio é ambiental e não foi convertido em skip novo.

## Rodada 30 — transições Lavalink, shutdown e TTS assíncrono

Sete falhas novas foram reproduzidas antes da correção: conexão de pool duplicada,
continuação Lavalink tardia após stop, TrackEnd obsoleto, runner HTTP sem cleanup,
ausência de shutdown coordenado, cancelamento TTS preso em `tts_direct` e estado
de ducking incorreto com overlays concorrentes. Os dez cenários finais passam.

`music_agent_runtime/lifecycle.py` continua sem estado e agora tem quatro funções/
60 linhas. O facade mantém pools, ownership Lavalink por guild, states, tokens,
locks e event loop. Cada fronteira assíncrona do início Lavalink revalida faixa e
geração; eventos aceitam apenas owner atual. Shutdown cancela/coleta tasks, desliga
players e fecha cliente/HTTP; `amain` coleta as tasks canceladas. TTS direto
restaura idle em cancelamento e `ducked` permanece verdadeiro até o último overlay.
Engines, cache, formatos, volumes/fatores e parâmetros TTS não mudaram.

Gate dirigido: 72 passed. `compileall` passou. A suíte ampla executável foi
segmentada por limite do host; telemetria mantém exatamente o mesmo problema de
fixture Python 3.13 do checkpoint 29 (57 passed/54 teardown errors). Três coletores
continuam bloqueados por `discord` ausente. Java obrigatório e ruff continuam sem
gate neste ambiente. Ver `RODADA-30.md` para comandos/limites e fechamento.

### Próxima rodada concreta após 30

1. Caracterizar o self-builder Python antes de mover API, arquivos temporários,
   promoção/rollback ou subprocessos.
2. Extrair somente a unidade sustentada por testes, preservando contratos do
   builder Chaquopy e recuperação já documentados.
3. Depois seguir MainActivity/UI e discovery/release; repetir Java/ruff/plataforma
   em ambiente completo antes da entrega final.

## Rodada 31 — self-builder Chaquopy: toolchain e preflight

O self-builder do APK iniciou a separação sem alterar o import Java
`coreworker.apk_self_builder` nem as quatro assinaturas públicas. Toolchain e
preflight/readiness/resource preflight agora ficam em `coreworker/builder/` e o
facade conserva wrappers/bindings vivos. Source, processo Gradle, ownership,
recovery, build, publish e cleanup permanecem no facade.

Dois manifests corrompidos que antes escapavam com `ValueError`/`TypeError` agora
são classificados como toolchain inválido e retornam readiness bloqueado. O schema
v1 preserva a regra anterior; v2 continua exigindo versões exatas e os cinco
smokes. Gate dirigido: 154 passed; família Core Worker: 218 passed/85 skipped.
Cobertura ampla executável em lotes: 244 passed/86 skipped, 340 passed e 562
passed/9 subtests passed. Limitações ambientais de Discord, Java/JARs, ruff e o
fixture de telemetria Python 3.13 permanecem iguais à rodada 30.

### Próxima rodada concreta após 31

1. Caracterizar e extrair source/download/ZIP/injeção privada/hidratação do
   self-builder, preservando hash mismatch como determinístico e limites atuais.
2. Depois mover ownership/processo Gradle + recovery como um contrato único,
   incluindo PID/start ticks/PGID/job/attempt e cancelamento.
3. Fechar build/publish/cleanup antes de seguir MainActivity/UI e discovery/release.

## Rodada 32 — source e private inputs do self-builder

`coreworker/builder/source.py` separa download autenticado, ZIP seguro e descoberta
do projeto; `coreworker/builder/private_files.py` separa google-services/keystore,
properties de assinatura e hidratação de runtime assets. O facade continua sendo o
entrypoint Java→Chaquopy e caiu de 1.579 para 1.341 linhas.

Seis regressões foram caracterizadas antes da correção: extração parcial antes de
erro ZIP tardio, colisão de nomes normalizados, escrita privada antes de validar
toda a entrada, injeção de linha em properties, symlink externo em repro-assets e
arquivo stale com mesmo tamanho. Os seis cenários passam após o corte. Download
mantém mesma origem, hash/bytes, três tentativas e erros determinísticos/transientes
anteriores; ZIP mantém os limites existentes e agora pré-valida tudo antes de
escrever. Privados são validados antes da primeira gravação e usam 0600; hidratação
não segue symlinks e compara conteúdo por SHA-256 quando necessário.

Gate relacionado: 203 passed. Família Core Worker: 218 passed/85 skipped. Cobertura
ampla executável ficou verde em seis lotes; bloqueios ambientais de `discord`, JARs
Java, `ruff` e telemetria Python 3.13 permanecem iguais à rodada 31.

### Próxima rodada concreta após 32

1. Extrair ownership/processo Gradle + recovery mantendo PID/start ticks/PGID,
   job/attempt, lock, sinais, cancelamento e reconciliação como um contrato único.
2. Depois separar build/publish/cleanup sem alterar validação do APK ou barreira de
   outbox/finalização.
3. Só então seguir MainActivity/UI e discovery/release.

## Rodada 33 — ownership/processo Gradle e recovery

`coreworker/builder/process.py` passa a ser o dono sem estado de PID/start ticks,
PGID, lock, cancelamento, subprocesso Gradle, reconcile e finalize. O facade
`apk_self_builder.py` mantém as quatro APIs Java→Chaquopy e wrappers/bindings vivos,
e cai de 1.341 para 945 linhas.

Na entrada, 6 de 7 cenários novos falharam: lock fresco ownerless roubado, lock
órfão após falha de owner publish, `attempt` corrompido escapando nas duas APIs,
`pythonFinishedAt` inválido e `work` igual ao root podendo apagar todas as
attempts. O caso de lock ownerless realmente stale já passava. Após a correção os
7 passam; lock fresco ganha grace de publicação, falha da própria publicação limpa
somente seu lock, metadata inválida vira identidade não verificada e work precisa
ser descendente estrito.

Família Core Worker + modularização/source/process: 225 passed/85 skipped. Cobertura
ampla executável: 278; 344; 223/77; 314/9 + 9 subtests. A suíte global e telemetria
repetem exatamente os bloqueios da rodada 32. `compileall` passa; Java obrigatório
e `ruff` continuam não aprovados neste host.

### Próxima rodada concreta após 33

1. Separar build orchestration, validação/publicação e cleanup preservando a
   barreira de outbox durável antes de liberar ownership/resources.
2. Manter identidade do APK, hashes, retenção, rollback e cancelamento sem alterar
   versão/protocolo Java→Chaquopy.
3. Depois seguir MainActivity/UI e discovery/release/plataforma.

## Rodada 34 — fechamento do self-builder Python

`coreworker/builder/build.py` recebe a orquestração e
`coreworker/builder/artifact.py` recebe validate/persist/publish/cleanup. O facade
`apk_self_builder.py` conserva as quatro APIs Java→Chaquopy e cai de 945 para 597
linhas. Work/log passam a ser publicados no owner antes de qualquer private input;
assim falha pré-Gradle pode ser limpa pelo finalize depois da outbox durável.

Cinco de seis cenários novos falharam na entrada: work privado órfão do owner,
republicação sem SHA persistido, conexão multipart aberta após send falhar, APK
parcial exposto no nome final e `notificationId` não sanitizado em colisão. O caso
de finalize pré-Gradle com work já conhecido passava. Após correção são 6 passes.
Promoção agora é temp→hash→replace, publish exige SHA durável e multipart fecha em
`finally`. Retenção/identidade/versionName/versionCode e cancelamento permanecem.

Família Core Worker + self-builder: 231 passed/85 skipped. Cobertura executável em
quatro lotes: 317/9 + 9 subtests; 292; 333; 223/77. Suíte global e telemetria
repetem exatamente os bloqueios da rodada 33. `compileall` passa; Java obrigatório
e `ruff` continuam sem aprovação neste host. Nenhum Gradle/APK/publicação real.
Reconstrução preliminar pelo patch: 620/620 arquivos idênticos em bytes/modos,
58 passes no gate relacionado e 231/85 na repetição final da família. Um primeiro
run da reconstrução teve flutuação do teste real de `/proc`; o caso isolado passou
em fonte e reconstrução sem mudança de código.

### Próxima rodada concreta após 34

1. Inventariar MainActivity/UI: ownership de estado, renderização, listeners,
   lifecycle e reentrada; caracterizar antes de mover classes/helpers.
2. Extrair somente fronteiras cobertas mantendo protocolos Java↔Python/worker,
   identidade/versionamento e funcionamento sem worker na VPS.
3. Depois seguir discovery/release/plataforma e repetir Java/Gradle/aparelho no
   ambiente completo antes da entrega final.
