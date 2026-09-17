# Rodadas 25–26 — adaptadores Teto e fallbacks Edge/gTTS

Entrada: `575b34d52e89a06c26dafb70b1d01c2f8b16a128` (13/09/2026).
589 arquivos/modos, 570 hashes e quatro JARs íntegros antes de editar.
Ver `INVENTARIO-TTS-PROVIDERS.md` para contratos, donos e lacunas de entrada.

## Rodada 25 — Teto

Caracterização antes da extração: 39 passes, incluindo dois testes do renderer
com assets/resampler locais. Os 37 casos novos exercitam o facade: parâmetros,
metadados, bytes raw/base64, limite exato/erro, cache com recurso ocupado,
concorrência e liberação do mesmo lock em falhas de carga/render/formatação.
Singleton lazy concorrente, retry após erro, status desativado/force, limites de
memória/bateria e os oito tipos de job pesado também foram caracterizados.
Não houve bug reproduzido nesse conjunto; o ramo foi extraído sem mudar sua lógica.

Código: `tts_providers.py` recebe lock pesado, getter do renderer, clock e
formatter vivos. Renderer, RLock, erro, guard, cache, admissão e envelopes ficam
no facade. Não há estado nem import de provider ao carregar o módulo. Loader
opcional único com retry e preload independente antes de jobs/HTTP. Release/hash
incluem o arquivo 0644, com 33 membros incluindo manifesto e bootstrap 1.0.0.

Seleção integrada após extrair: 136 passes, zero mutações de fontes/runtime/
release. Acrescentado teste de reatribuição após aquecer o adaptador para
demonstrar que renderer, lock, clock e formatter continuam visíveis na chamada.
Comandos, hash da fonte e resultados estão nas evidências externas da rodada 25.
Os totais das seleções não devem ser somados.

Pendências: providers externos e latência em aparelho não foram medidos. O guard
e a coleta de dependências existentes não foram redesenhados. Java, VPS, PCM,
Music, self-builder e transporte TTS permanecem iguais à entrada.

Próximo passo concreto: caracterizar os fallbacks Edge/gTTS no facade e extraí-los
para o mesmo serviço, preservando imports tardios, cancelamento/timeouts,
parâmetros, erros e bytes, além da precedência do transporte compartilhado.

## Rodada 26 — fallbacks Edge/gTTS

Caracterização antes de mover: 70 passes no arquivo de providers, sendo 38 Teto
(incluindo bindings quentes) e 32 dos fallbacks. Fixtures com asyncio real e
providers em memória demonstram cancelamento/fechamento do stream no timeout,
concatenação somente de áudio, falhas após áudio parcial, bytes raw/base64,
limite exato/excedido, imports com erro encadeado/truncado e retry. Também verificam
precedência do transporte compartilhado, cache hit sem import e fallback real
Edge→gTTS com parâmetros trocados e contadores balanceados.

Código: os ramos Edge e gTTS foram movidos para o mesmo `tts_providers.py`, agora
três funções/66 linhas. Imports de edge_tts/gTTS continuam dentro das funções;
IO/asyncio, normalizadores e short_text são bindings vivos. Dois testes adicionais
após aquecer o módulo demonstram reatribuição de IO/asyncio/normalizadores. O loader
passa a validar as três funções. Nenhuma dependência de produção foi acrescentada.

Timeout Edge continua wait_for/run; gTTS mantém o par conexão/leitura limitado a
3,5/8 s. O fallback gTTS não recebe tld, como na entrada; o transporte compartilhado
continua recebendo. Limite de tamanho dos fallbacks segue após a síntese completa.
Não houve bug novo demonstrado nem redesenho de comportamento nesta rodada.

Validação integrada: 320 passes, incluindo os 72 de providers, 25 dos serviços,
25 de release, cache/Android/política/ciclo TTS, streaming/runtime, benchmarks e
fronteiras Python. Zero mutações de fontes/runtime/release. A auditoria comprova
os três corpos iguais por AST após substituir bindings, um método de handler
alterado, um loader novo e um preload independente. As assinaturas e todos os
estados anteriores permanecem; cache, envelopes, dispatch e ramo de transporte
compartilhado são iguais à entrada. São 581 arquivos da base da rodada fora do
escopo iguais em bytes/modos, incluindo os 44 protegidos anteriores.

Fechamento previsto: fonte congelada, suíte geral com Java obrigatório, lint e
sintaxe; diff aplicado a uma extração limpa da rodada 24, comparação de todos os
593 arquivos/modos e suíte geral nessa reconstrução. Os resultados finais e
hashes ficam no registro externo 27, sem editar a fonte após os gates. Inventário
atual: 93 atribuições globais, 321 funções e 11.085 linhas no facade; quatro JARs
íntegros. Phone Worker 1.11.6, bootstrap 1.0.0 e APK 0.8.6/133 preservados.

Pendências: PCM com stderr saturado/preparos concorrentes, Music/self-builder,
UI, discovery seguro, fonte Coptic legítima e gates Java/plataforma. Prune continua
por melhor esforço; raw/JSON Android preserva timeout por chamada. Não foram
medidos providers externos ou latência em aparelho, nem executados Gradle/APK,
publicação, instalação ou jobs remotos. VPS de 1 GB segue sem worker obrigatório.

Próximo passo concreto: caracterizar IO PCM com processo local real para saturação
de stderr e preparos simultâneos, preservando cancelamento/EOF/limpeza/posse; depois
extrair apenas a fronteira de IO demonstrada pelos testes. Não repetir os
adaptadores TTS já tratados. O checkpoint completo + diff permanece intermediário.
