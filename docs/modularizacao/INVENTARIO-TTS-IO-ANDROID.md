# TTS — cache com IO e adaptador Android

Entrada: rodada 22, commit `f763aa631afa64f0095a0d45a0af5d199020ec67`.
Conferidos antes de editar: 582 arquivos em bytes/modos, 500 hashes do pacote,
suíte final/reconstruída e quatro JARs por tamanho/SHA-256/CRC. A árvore local
era exatamente o checkpoint entregue. core-worker-retomar indisponível;
usados os documentos equivalentes do pacote.

| Fronteira na entrada | Dono e contrato | Lacuna antes desta rodada |
| --- | --- | --- |
| Cache normal e direto | Handler prepara chaves, limites, autorização e envelopes; MP3/WAV/OGG na ordem existente | Publicações simultâneas e falhas de gravação opcional |
| Prune compartilhado TTS/Piper | Varredura, limites bytes/arquivos, proteção explícita, idade 180 s, flock não bloqueante, comparação de inode | Substituição e touch entre scan/open/stat; leitor com flock compartilhado |
| Touch e manutenção | Um lock, mapa touches limitado a 4096, throttle 30 s, um executor, pending/running no facade | Troca dos bindings após carregar módulo de IO |
| Streaming/binário | tts_transport.py publica por replace; leitor mantém flock SH; primeiro áudio/EOF têm testes HTTP locais | Transporte permanece no arquivo existente |
| HTTP nativo | Dois helpers urllib; JSON GET/POST e raw POST; URL/version/timeout do facade | Requisições reais contra fixture HTTP local, limites e fechamento das respostas |
| Síntese Android | Ramo dentro do handler; raw antes de JSON, mesmos parâmetros; logs/timing por requisição | Erros raw/JSON, flags e deadlines de cada requisição antes de mover |
| Status/voices Android | Cache de status e ambiente no facade, request helper vivo | Expiração/rebinding/status desativado e normalização de voices |

Os novos serviços recebem callbacks/estado na chamada e não importam o facade.
O executor, admissão e contadores não mudam de dono. Preload opcional antes de
jobs/HTTP evita carregar os arquivos na primeira síntese normal; chegada tardia
permite retry. O import de cada serviço não inicia IO, threads ou rede.

As fronteiras HTTP Android usam um servidor local de teste, não o APK real.
Clocks controlados verificam ordem e parâmetros, sem medir latência no aparelho.
VPS, Java/APK, Voice/PCM, Music, self-builder, bootstrap e transporte TTS ficam
fora das edições de código desta rodada. PCM com stderr saturado/preparos
simultâneos, JNI/Android real, UI e discovery permanecem pendentes.

Prune é manutenção de cache por melhor esforço. A revalidação não constitui
transação entre o último stat e unlink diante de processos externos. Não se
introduz exclusão global de publicação nem se executa poda na resposta TTS.
