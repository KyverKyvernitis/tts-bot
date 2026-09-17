# Rodadas 23–24 — cache com IO e adaptador Android

Entrada de 13/09/2026: `f763aa631afa64f0095a0d45a0af5d199020ec67`.
582 arquivos/modos, 500 hashes do checkpoint 22 e quatro JARs íntegros.
Ver `INVENTARIO-TTS-IO-ANDROID.md` para donos, contratos e lacunas da entrada.

## Rodada 23 — cache com IO

Caracterização: 18 testes com arquivos reais, flock do host, barreiras de threads
e interleavings controlados. A entrada falhou em dois: poda apagava arquivo
substituído após scan/antes de open, e arquivo tocado após scan. Os demais 16
passaram. Correção revalida inode contra a varredura e mtime após adquirir flock;
57 testes de cache/ciclo passaram antes de extrair.

Código: tts_cache.py concentra cinco operações de IO: busca, leitura, publicação,
touch e prune. Wrappers preservam assinaturas; cache direto continua propagando
erros e cache opcional retorna miss/log de falha. Publicação continua por replace,
sem temporários abandonados. Mapas, lock, executor, coalescência e escalares ficam
no facade; bindings de IO/clock/estado são fornecidos na chamada. Piper conserva
suas rotinas, usando a poda compartilhada pelo wrapper existente.

Módulo opcional validado por cinco funções, uma instância sob concorrência,
retry após ausência/incompletude/erro de sintaxe e preload independente da política
antes de jobs/HTTP. Não abre arquivos nem inicia rede/threads ao importar.
Distribuição e hash incluem tts_cache.py 0644, com o bootstrap original 1.0.0.

Validação integrada: 176 testes passaram, zero mutações de fontes/runtime/release.
Uma seleção anterior (`rodada23-cache-extraido`) terminou exit 4 sem executar
testes porque o comando apontava para um nome inexistente; corrigido para
test_tts_worker_streaming.py na seleção integrada, sem alterar asserções.
Os totais são seleções distintas; não somar. Logs/JSONs registram comandos e hash.

Limites: prune continua por melhor esforço; não é transação stat/unlink entre
processos externos. Fixtures verificam bloqueios/bytes/ordem, não latência real.
Transporte streaming/binário, Java, bot/VPS, PCM, Music e self-builder não mudaram.

Próximo passo concreto: caracterizar os helpers HTTP e o ramo Android raw→JSON
antes de separá-los, preservando timeout, payload, áudio, logs e cache de status.

## Rodada 24 — adaptador Android

Caracterização antes de mover: 44 testes novos mais dois benchmarks passaram.
Cobrem HTTP local GET/POST/Unicode/headers, raw com metadados, fallback raw→JSON,
limite exato/excedido/áudio vazio, tipos JSON inválidos, base64, erros e fechamento
de respostas. Clocks e providers controlados demonstram timeouts por chamada,
flags, áudio raw/base64, parâmetros primários, TTL e cópias de status/voices.
A fixture preserva as funções HTTP originais para testar o protocolo real local.

Código: tts_android.py contém três funções em 125 linhas. O facade fornece URL,
versão, request/open, clocks, env e codecs em cada chamada. Status/cache Android,
voices, admissão, contadores, logs/timing por requisição e resultado/cache normal
mantêm seus donos. Os corpos HTTP e do ramo nativo são iguais por AST aos da
entrada após substituir esses bindings; as demais engines e dispatch não mudam.

Módulo opcional com validação de três funções, carga única, retry e preload
independente antes de jobs/HTTP. Release inclui os dois serviços em 0644: 31
arquivos mais manifesto, 32 membros. São 23 testes de release e 17 dos serviços;
bootstrap original 1.0.0 aceita a árvore e import sem site-packages.

Validação integrada após a extração: 230 passes, zero mutações de runtime/release.
A auditoria confirma sete métodos de WorkerHandler alterados, cinco definições
de topo alteradas e dois loaders novos, sem mudar assinaturas. Main só acrescenta
dois preloads opcionais. IO de cache mantém envelopes e semântica de erros; prune
só acrescenta revalidação de inode/mtime. Todos os estados existentes permanecem.

Fechamento: suíte geral com Java obrigatório, lint/sintaxe, comparação com a
rodada 22, patch aplicado numa cópia limpa, igualdade de bytes/modos e suíte da
reconstrução. Resultados finais e hashes ficam no registro externo 26 depois dos
gates, sem editar a fonte testada. Inventário: 589 arquivos, facade com 91
atribuições globais, 320 funções e 11.097 linhas; quatro JARs íntegros.

Pendências: os timeouts raw e JSON continuam independentes como na entrada; não
se redefiniu deadline agregado. Prune não é uma transação stat/unlink entre
processos externos. Não houve providers reais, Android/JNI/Gradle/APK ou medição
de latência em aparelho. VPS de 1 GB continua sem exigir worker. Java, bot/VPS,
Voice/PCM, Music, self-builder, bootstrap e transporte TTS não foram alterados.
Publicação, instalação e jobs remotos permanecem fora desta retomada.

Próximo passo concreto: caracterizar/separar Teto com seu guard de recurso pesado,
depois fallbacks Edge/gTTS sem transporte compartilhado. Antes de mover IO PCM,
caracterizar stderr saturado/preparos simultâneos. Music/self-builder, UI e
discovery seguro seguem pendentes; não repetir cache/política/Android já extraídos.

## Errata das evidências anteriores

O registro externo 25 entregue traz "exit 0" na coluna de resultado de
rodada21-tts-entrada, embora seu próprio metadado tenha exit 1. O gerador usou
um fallback incorreto quando não encontrou resumo do pytest. O log preservado
está incompleto e não comprova os totais 14 failed/25 passed citados no histórico.
Nesta retomada não reutilizamos essa contagem como gate; somente o exit 1 está
confirmado. Logs e checkpoint anteriores permanecem byte a byte iguais. As suítes
completas da rodada 22 têm resumo final explícito e continuam comprovadas.
