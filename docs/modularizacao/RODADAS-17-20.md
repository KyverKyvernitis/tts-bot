# Rodadas 17–20 — estado e ciclo do Voice Agent

Entrada de 10/09/2026: `cb70bb2d375cdb38b53f29ef212d4a09c19f5e70`.
Os 570 arquivos/modos e os 349 hashes do checkpoint 16 foram conferidos antes de
editar. A árvore de trabalho é exatamente a fonte de continuação já entregue;
base e checkpoints anteriores permanecem referências intactas.

## Rodada 17 — caracterização e correções de estado

Inventário: sessões persistem somente metadados; handoff/token ficam em memória.
Sessões, handoffs, transferências e conexões compartilham o RLock do facade.
Streams PCM têm outro mapa/RLock. Não havia testes específicos desses helpers
Voice Agent; a cobertura anterior não demonstrava o ciclo de transferência.

Código: a concessão de posse não repete mais kwargs de estado, o que impedia
iniciar transferências. Expiração da lease revoga a permissão do handoff gerado
pela transferência; iniciar probe também verifica essa expiração. Tempo zero é
respeitado e registros recém-atualizados aparecem primeiro nos quatro resumos.
Pruning ignora/remove registros corrompidos sem perder válidos; estado vazio
explícito não é substituído pelo runtime. Operações de sessão e seu snapshot usam
o mesmo lock na leitura/mutação/persistência, preservando o dono dos mapas.

Testes: 15 falhas e 15 passes na entrada; após correção, 77 passes em estado de
voz, plano de controle e limites Python. Há testes de TTL, confirmação/liberação,
privacidade, env desativado, persistência local, falha de gravação e concorrência.
Zero mutações de runtime/release da árvore fonte. A fixture pública integral foi
capturada do monólito corrigido antes da extração.

Pendências: projeções/resumos ainda estão no facade; cancelamento de probe em
andamento e conclusão obsoleta ainda não foram validados. Não houve Voice WS/UDP
real, Discord, áudio ou jobs remotos.

Próximo passo concreto: extrair a unidade pura de projeção/seleção de registros,
passando clock/dados/callbacks explícitos, e validar carga/distribuição/bindings.

## Rodada 18 — projeções e resumos puros

Código: nove funções foram para `phone_worker_runtime/voice_state.py`: quatro
projeções públicas, quatro resumos e seleção de chaves expiradas/inválidas.
Recebem dados, timestamp, parser e projeções explicitamente. Não possuem estado,
locks, arquivos ou threads e não importam o facade. O facade continua sendo dono
dos mapas e da persistência. Loader lazy tem instância única, retry de carga e
validação das funções esperadas; o bootstrap de controle continua disponível
mesmo sem o módulo Voice Agent. Operações opcionais de voz aguardam esse estágio.

Testes executados: 95 passes após extração; 100 após carga concorrente, callbacks
reatribuídos, módulo ausente/incompleto/com sintaxe inválida e import puro. A
fixture dos quatro JSONs públicos permanece igual ao monólito corrigido. O
publisher/runtime incluem voice_state.py com modo 0644; os 29 membros da release
local foram aceitos pelo bootstrap original. Alteração só no módulo muda hash;
release anterior permanece intacta. Nenhuma mutação de runtime/release.

Pendências: o cancelamento lógico de probes e recursos de streaming ainda serão
testados antes do fechamento; nenhuma validação Discord/Voice WS/UDP real.

Próximo passo concreto: demonstrar e corrigir reentrada/concorrência de probes,
cancelamento por clear/release/expiração e conclusão que chega fora de ordem.

## Rodada 19 — geração, cancelamento e conclusão de probes

Código: agendamento e checagem de duplicidade usam o RLock existente e respeitam
idade zero e o estágio HELLO. Cada probe recebe uma geração privada no registro
de conexão, sem novo mapa ou lock. Callbacks verificam essa geração e a posse
vigente antes de iniciar, depois de awaits e ao gravar sucesso/erro. Clear,
substituição/remoção de handoff, release, expiração ou nova geração impedem que
respostas antigas recriem ou sobrescrevam a conexão. Falha ao iniciar a thread
deixa estado terminal e permite nova tentativa. Concessão/liberação atualizam
transfer e handoff na mesma seção crítica.

Testes: 13 falhas e 1 passe demonstraram duplicação, execução depois de cancelada,
resultado obsoleto e thread registrada como ativa após falha no start. Depois,
50 passes em probe/estado; a seleção ampliada passou 112 testes, incluindo
substituição de handoff, release/expiração durante receive e reentrada no HELLO.
O coroutine real executou com fronteiras WS/UDP em memória, comprovando fechamento
do contexto e ausência de envio seguinte após cancelamento observado. Nenhum
socket Discord/UDP real ou job remoto foi usado; zero mutações da árvore fonte.

Limite: cancelamento é cooperativo, nos pontos de checagem e timeouts existentes;
não é interrupção forçada de thread ou garantia de fechamento instantâneo de
uma operação de rede já bloqueada. A geração não aparece no payload público nem
no arquivo de sessão. A lógica WS/UDP e timeouts existentes não foram redesenhados.

Próximo passo concreto: caracterizar registro/expiração e cleanup dos recursos
PCM, corrigir falhas demonstradas e fechar suíte geral/reconstrução do checkpoint.

## Rodada 20 — PCM, flags e fechamento

Código: falha/timeout de preparo remove o PCM temporário e mantém o destino
anterior. No streaming live, stdout/stderr são fechados após kill/wait, inclusive
em desconexão/erro, e não se envia uma segunda resposta HTTP depois dos headers.
Flags textuais negativas não confirmam transferência nem autorizam probes. O
único método alterado em WorkerHandler foi a condição de confirmação de posse
em `_task_voice_agent_play_tts`; delegação/prebuild/engine/cache/áudio permanecem
iguais. A confirmação afirmativa mantém aliases e o caminho Music Agent gateway.

Testes: 5 falhas e 8 passes na entrada PCM; depois 88 passes na seleção de
PCM/Voice/streaming/limites. Flags tiveram 6 falhas/4 passes/36 deselected, e a
condição do TTS direto 1 falha/1 passe/46 deselected antes de corrigir. O gate
dirigido final passou 119 testes. Registro/lookup/TTL usam o mapa e lock vivos;
PCM preserva bytes, 48 kHz/stereo/16 bits/20 ms e limites de leitura. Processos e
áudio são fronteiras controladas; não executamos ffmpeg/Discord reais nesses testes.

Gates de entrega: suíte geral com Java obrigatório, lint/sintaxe, AST/hashes das
áreas preservadas, aplicação do diff sobre a rodada 16, comparação de bytes/modos
e suíte completa da reconstrução. Os resultados finais e hashes ficam no registro
24 e em `evidencias/rodadas-17-20/` do pacote externo, consolidados após os gates
para não modificar a fotografia fonte testada.

Pendências: não há cancelamento preemptivo de thread, medição de latência, JNI/
plataforma ou Voice WS/UDP/ffmpeg reais. Streaming live com stderr saturado e
preparos simultâneos ainda não foram caracterizados. Operações de Voice Agent,
rede/threads e persistência continuam no facade; foi extraída a unidade pura.
TTS, Music, self-builder, UI e discovery seguro ainda não estão modularizados
completamente. A fonte Coptic ausente e limites Java anteriores permanecem.

Próximo passo concreto: inventariar o domínio TTS (cache, fila, transportes e
manutenção), comparar caminhos frio/quente por testes locais e extrair a primeira
unidade pura sem mudar engines, fallback, bytes/frames ou introduzir IO na resposta.
Manter as validações de saturação/concorrência PCM como gate antes de mover seu IO.
Não houve publicação, instalação em aparelho ou envio de jobs remotos.
