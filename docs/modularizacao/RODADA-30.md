# Rodada 30 — transições Lavalink, shutdown e TTS assíncrono

Entrada: checkpoint da rodada 29, SHA-256
`3ff097b2ec9434610a434115e59222f3fa72b89e3d77133ccba92ec7b314fb50`,
601 arquivos. `core-worker-retomar` não está disponível neste ambiente; foram
relidos o handoff portátil e `docs/11b-music-agent-async.md` do pacote da rodada
28 antes das edições. O patch 27–28 não foi reaplicado.

## Caracterização antes das correções

Foram adicionados cenários locais sem Discord, Lavalink ou rede reais. A entrada
falhou em 7/7 casos novos:

1. duas chamadas concorrentes de `ensure_lavalink_pool` iniciavam duas conexões;
2. `stop` durante `channel.connect` permitia que a continuação tardia republicasse
   o player e chamasse `play` para uma geração já inválida;
3. `TrackEnd` de um playable anterior podia encerrar a faixa atual no mesmo player;
4. `run()` não limpava o `AppRunner` quando o cliente terminava;
5. não havia shutdown coordenado para tasks, TTS, players, cliente e HTTP;
6. cancelamento de TTS direto podia deixar o estado em `tts_direct` sem timer idle;
7. um overlay TTS concluído marcava `ducked=False` mesmo com outro overlay ativo.

Um ajuste no próprio fixture foi necessário durante a caracterização: uma task
criada e cancelada antes do primeiro ciclo não executa seu `finally`. O teste de
shutdown passou a dar um único yield antes da ação para garantir tasks realmente
ativas; nenhuma asserção de produção foi relaxada.

## Correções e unidade extraída

O pool Lavalink agora é serializado por lock e revalida `_pool_connected` dentro
da seção crítica. A compatibilidade com Wavelink sem `cache_capacity` permanece,
mas a segunda tentativa também fica dentro do tratamento que restaura o estado
em caso de erro.

A reprodução Lavalink guarda ownership por guild com player, chave do playable e
`playback_token`. Eventos Start/End/Exception só atuam quando player, playable e
geração ainda pertencem ao estado atual; End/Exception consomem o ownership antes
de qualquer await. `_bump_playback_generation` invalida ownership anterior.
`_play_lavalink` verifica posse após cada fronteira assíncrona relevante; conexão
que termina depois de stop é recolhida se o estado não a possui e não inicia áudio.
Fila, histórico, loop mode, volume e resolução não foram redesenhados.

`music_agent_runtime/lifecycle.py` continua sem estado global e passa a ter quatro
primitivas/60 linhas: remoção condicional de task, teste de posse por faixa+geração,
cancelamento/coleta deduplicada de tasks e stop/disconnect best-effort do player.
Maps, locks, players, tokens, event loop e Wavelink continuam pertencendo ao
`MusicAgent`.

O shutdown do agente cancela e coleta TTS ativos antes dos demais timers/prefetch,
invalida gerações, despublica players antes de IO, tenta stop/disconnect de cada
player único, fecha o cliente Discord e limpa o runner HTTP. É idempotente. `run`
limpa o runner também em retorno/erro; `amain` coleta stop/run tasks em vez de
apenas cancelá-las e abandonar referências pendentes.

No TTS direto, toda saída após publicar `tts_direct` restaura `idle` e agenda o
mesmo timeout idle se o player ainda pertencer à sessão e não houver música. No
mixer, `has_tts()` consulta overlays sob o lock; o campo público `ducked` só volta
a falso quando o último overlay daquele mixer sai. O mixer já fazia o ducking de
áudio pela lista de overlays; a mudança corrige lifecycle/estado observável sem
alterar fatores, engines, cache, formatos ou parâmetros de síntese.

## Testes e ambiente

- Novos cenários: 7 failed na entrada; 7 passed após as correções; 10 passed após
  cobrir diretamente as novas primitivas e `amain`.
- Lifecycle da rodada 29 + rodada 30: 22 passed.
- Gate dirigido com release modular, boundaries e streaming TTS: 72 passed.
- `compileall` de Phone Worker, Python APK, scripts e testes do Music Agent: exit 0.
- Suíte global sem ajustes: 3 erros de coleta por `discord` ausente e 1 skip; o
  mesmo bloqueio já existia no checkpoint anterior.
- Cobertura ampla em lotes, excluindo apenas os 3 coletores de chatbot bloqueados
  e tratando telemetria separadamente: 249 passed/86 skipped; 332 passed; 562
  passed/9 subtests passed.
- `test_phone_worker_telemetry.py`: 57 passed/54 teardown errors no estado atual e
  exatamente 57 passed/54 errors no checkpoint 29 limpo. Em Python 3.13 o fixture
  monkeypatcha `Path.glob` sem aceitar os kwargs novos usados por `Path.rglob`;
  não é regressão da rodada 30 e o teste não foi enfraquecido.
- Gate Java obrigatório não pôde ser executado: Java 21 existe, mas o pacote não
  contém JARs; os dois arquivos de gate resultam em 85 skipped neste host.
- `ruff` não está instalado. Não registrar lint ou Java como aprovados.

Os testes do Music Agent foram tornados autocontidos: o fixture adiciona a pasta
do agente ao `sys.path` apenas durante a carga, eliminando dependência externa de
`PYTHONPATH` para importar `music_agent_runtime`.

Nenhuma publicação, instalação, job remoto, Gradle/APK real, Discord real,
Lavalink real, ffmpeg/provider real ou medição de latência foi executada. A VPS
continua sem depender do worker.

## Fechamento

O patch preliminar foi aplicado com `git apply --check` e `git apply` sobre uma
extração limpa do checkpoint 29. A reconstrução resultou em 603 arquivos e
11.716.397 bytes, exatamente iguais à fonte em caminhos, conteúdo e modos. O gate
dirigido da reconstrução passou com 72 testes e `compileall` retornou zero. Cinco
linhas em branco com whitespace introduzido durante a edição foram detectadas por
`git diff --check`, removidas e a reconstrução foi repetida antes do patch final.

O patch final deve repetir essa reconstrução limpa sem warnings de whitespace; os
hashes dos artefatos e a validação da extração do checkpoint são registrados no
relato externo da rodada para evitar autorreferência dos próprios artefatos.

## Próximo passo concreto

Com lifecycle assíncrono, transições Lavalink, shutdown e cancelamento TTS agora
caracterizados, seguir para o self-builder Python conforme o handoff. Depois vêm
MainActivity/UI e discovery/release. Permanecem também os limites já registrados
do PCM (stderr do preparo em memória, transcoder não interrompido proativamente
por expiração e limpeza best-effort) e os gates externos Java/ruff/plataforma.
