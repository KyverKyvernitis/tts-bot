# TTS — adaptadores Teto e fallbacks Edge/gTTS

Entrada: rodada 24, commit `575b34d52e89a06c26dafb70b1d01c2f8b16a128`.
Antes de editar foram conferidos 589 arquivos/modos, 570 hashes do pacote,
quatro JARs por tamanho/SHA/CRC e os gates completos da fonte e reconstrução.
Árvore limpa. core-worker-retomar indisponível; documentos equivalentes usados.

| Fronteira existente | Dono e contrato | Lacuna a caracterizar |
| --- | --- | --- |
| Ramo Teto | Handler adquire o mesmo lock não bloqueante de build/manutenção, chama renderer e projeta áudio/metadados | Contenção, liberação em erros, bytes/limites, cache hit com recurso ocupado |
| Renderer e guard | Singleton, RLock e erro no facade; import tardio; guard consulta memória, bateria, jobs e env | Concorrência, retry, status desativado, limites e observação de valores vivos |
| Edge sem transporte | Import tardio, chunks de áudio concatenados, asyncio wait_for/run | Timeout/cancelamento, falha parcial, parâmetros, áudio e propagação de erros |
| gTTS sem transporte | Import tardio, write_to_fp, timeout de conexão/leitura limitado a 3,5/8 s | Escrita parcial, erros, idioma e limites exatos |
| Transporte compartilhado | Ramo prioritário já modular em tts_transport.py | Confirmar precedência e ausência de import dos fallbacks |

Um serviço opcional sem estado reunirá os três ramos de providers em dois cortes.
Renderer, guard, locks, cache, admissão e envelopes permanecem no facade. Bindings
são fornecidos na chamada. Preload não importa providers nem inicia IO/threads.
Chegada tardia preserva retry e controle mínimo; incluir módulo em listas/hash.

Não redesenhar deadlines ou limitar o buffer no meio do áudio nesta extração:
os fallbacks existentes verificam o tamanho depois da síntese. Tld não é passado
pelo fallback gTTS recebido; o transporte compartilhado conserva seu parâmetro.
Fixtures locais/controladas não medem latência ou validam providers externos.
Java, VPS, PCM, Music, self-builder, bootstrap e transporte dedicado preservados.
