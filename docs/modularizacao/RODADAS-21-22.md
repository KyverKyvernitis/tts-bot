# Rodadas 21–22 — ciclo e política TTS

Entrada de 11/09/2026: `87e57527e72e611061ed52f6d00fa10c83f0397a`.
Os 576 arquivos/modos e os 430 hashes do checkpoint 20 conferiram antes de editar.
O código atual era exatamente o estado entregue. Base e checkpoints preservados.
core-worker-retomar indisponível; usados os documentos equivalentes do pacote.

## Rodada 21 — caracterização e correções

Inventário: ver `INVENTARIO-TTS.md`. Admissão, contadores, executor de manutenção,
maps/locks, arquivos e providers continuam no facade/transporte existente. A
cobertura recebida demonstrava streaming/primeiro áudio, admissão concorrente e
bot sem worker, mas não o ciclo padrão completo frio/quente ou aliases/fallback.

Código: aliases de engine são canonicalizados antes de decidir se houve troca de
engine; o pedido primário não perde cache_key nem recebe parâmetros de fallback.
OSError ao ler cache opcional vira miss para a mesma engine. Gravação JSON direta
remove o temporário em finally se write/replace falhar, preservando o destino.
Nenhuma mudança de provider, protocolo, fila, lock, timeout, áudio ou prune.

Testes: 14 falhas/25 passes reproduziram dez aliases incorretos, duas leituras
opcionais desviando para fallback e dois temporários abandonados. Após corrigir,
78 passes no ciclo TTS/streaming/runtime/boundaries/benchmark. A caracterização
de política e ciclo passou 87 testes antes de extrair. Todas as execuções tiveram
zero mutações de runtime/release da árvore fonte.

Os 28 vetores SHA-256 de cache foram capturados do checkpoint 20 intacto, não da
implementação nova. Cobrem engines, aliases, override de chave, fallback, Teto e
parâmetros. Casos frio/quente usam arquivos reais temporários, quatro providers
controlados e saída raw/base64. Hit não sintetiza de novo, mantém bytes/digest e
prune é adiado. Coalescência, callback falhando e rejeição/retry do executor foram
testados sem iniciar manutenção em produção.

Pendências: funções ainda no facade; carga tardia e bindings após extração serão
validados a seguir. O clock determinístico comprova ordem/contratos, não latência
em aparelho. IO PCM saturado/preparos simultâneos, JNI/Android, Music/self-builder,
UI e discovery seguro permanecem pendentes. Sem publicação/aparelho/jobs remotos.

Próximo passo concreto: extrair normalização, ordem e política de cache como
unidade pura, passando env e callbacks explicitamente; validar chegada tardia,
concorrência, keys/payloads e release extraída pelo bootstrap original.

## Rodada 22 — política pura, bindings e primeira síntese

Código: `tts_policy.py` tem doze funções e 156 linhas, sem mapas, locks, IO,
leitura de ambiente ou import reverso. O facade mantém a síntese e os mesmos
donos dos contadores, admissão, arquivos, executor e manutenção. Wrappers mantêm
assinaturas; chave recebe normalizadores/sanitizador na chamada. Fingerprint e
base pitch Teto são obtidos pelo facade somente para essa engine.

O loader valida as doze funções, não cacheia falhas e usa uma instância sob
concorrência. Main faz preload opcional antes de jobs/HTTP para a primeira
síntese normal não ler o arquivo do módulo. Chegada tardia conserva retry e pode
ter uma carga posterior. O import do facade não inicia esse preload. O módulo
tem somente bibliotecas padrão já usadas no facade; não há provider pesado novo.

Testes: 145 passes após extrair e integrar distribuição. Na primeira bateria de
bindings, três falhas eram da fixture nova que omitia host/port obrigatórios do
payload de controle, com 156 passes; a chamada foi corrigida, sem alterar seu
contrato. Em seguida, 159 passes. Após acrescentar preload/startup e testes de
módulo ausente, a seleção ampliada passou 153 testes. Esses totais correspondem
a seleções distintas; não somá-los. Zero mutações de runtime/release.

Os testes demonstram 28 keys iguais à entrada, callbacks/env/escalares vivos,
uma carga sob concorrência e caminho quente sem path/lock de carga. Há 39 casos
de ciclo TTS e 56 de política/startup. A release local tem 29 arquivos e manifesto,
30 membros aceitos pelo bootstrap original 1.0.0; 19 testes de release incluem
ausência/hash isolado/modos/import sem site-packages. Nenhuma release foi publicada.

Auditoria: quatro definições de topo mudaram e duas foram adicionadas; doze
métodos de WorkerHandler mudaram com assinaturas iguais. Main só acrescenta o
preload; dispatch só canonicaliza a engine primária. Leitura de cache acrescenta
recuperação e gravação direta acrescenta cleanup. O restante da síntese padrão,
loops/outbox, locks/estado, Voice/PCM, Java, bot, Music, self-builder e transporte
dedicado permanece igual à entrada. Facade: 87 globais, 318 funções, 11.195 linhas.

Fechamento: suíte geral com Java obrigatório, lint/sintaxe, aplicação do diff na
rodada 20, igualdade de bytes/modos e suíte completa da reconstrução. Resultados
finais e hashes ficam no registro 25 do pacote externo depois dos gates, sem
editar a fotografia fonte testada. Os quatro JARs conferiram por hash e CRC.

Pendências: não houve medição de latência, providers reais, Gradle/APK, JNI/Android
ou instalação. Evicção/locks concorrentes do cache, IO PCM com stderr saturado e
preparos simultâneos ainda exigem caracterização antes de mover essas fronteiras.
Music, self-builder, UI, discovery seguro e limites Java anteriores continuam.

Próximo passo concreto: serviço de cache TTS, começando pelos testes de evicção,
flock/publicação concorrente; em seguida adaptador Android raw→JSON e limites.
Preservar executor/locks e a resposta de áudio sem poda/coleta adicional. VPS
continua sem worker obrigatório. Publicação, instalação e jobs remotos ficam fora.
