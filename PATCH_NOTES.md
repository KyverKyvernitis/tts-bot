# Patch: event-loop-anti-lag-updater-priority

Base: `repo-20260605-004304.zip`

## Corrige / melhora

- Reduz travadas causadas por logging síncrono:
  - logs do bot agora entram em fila (`QueueHandler`) e são gravados por uma thread de logging;
  - formatação pesada de traceback/exception fica fora do event loop;
  - mantém arquivo `logs/bot.log` e saída no journal, sem mudar formato público.
- Diminui spam de traceback do `discord.gateway` quando heartbeat trava:
  - primeiro alerta continua aparecendo;
  - alertas repetidos de heartbeat/`Loop thread traceback` entram em cooldown configurável;
  - evita que o próprio diagnóstico gere mais I/O durante lag.
- Watchdog do event loop ficou mais útil e menos agressivo:
  - cooldown configurável dos avisos;
  - guarda os últimos atrasos em `/health`;
  - em travas severas registra um resumo leve das tasks pendentes, sem dump gigante de stack.
- Updater passa a rodar em baixa prioridade:
  - aplica `renice` e `ionice` no processo do updater;
  - filhos como `git fetch`, validações Python e scripts auxiliares herdam prioridade menor;
  - reduz disputa com heartbeat/voz do Discord na VPS pequena.

## Não muda

- Não toca CallKeeper.
- Não muda assinatura/publicação de APK.
- Não builda APK na VPS.
- Não remove nem refatora módulos grandes neste patch.
