# Rodada 29 — lifecycle assíncrono do Music Agent

Entrada: rodada 28, commit documentado `ecf8b0961070f1594c2892a5ad35c09bf4088ded`, 597 arquivos. O teste interrompido `tests/test_music_agent_lifecycle.py` não existia no checkpoint; os casos foram reconstruídos a partir dos quatro sintomas registrados no handoff, sem reaplicar o patch 27–28.

## Caracterização e correções

Foram adicionados 11 cenários isolados de lifecycle, sem Discord, Lavalink ou rede reais. Eles reproduziram: (1) `finally` de idle/prefetch cancelado removendo uma task substituta; (2) falha em `stop` impedindo `disconnect`; (3) `cmd_stop` mantendo `st.player` durante o await de disconnect; (4) callback direto repetido avançando a fila enquanto a próxima faixa ainda estava em resolução/preparo.

As correções exigem identidade para remover tasks registradas; stop e disconnect são tentativas independentes; `cmd_stop` limpa a referência do player antes de aguardar IO; e `_direct_after` invalida sua geração antes de qualquer await da transição. Fila, histórico, loop, resolução, áudio e parâmetros de TTS não foram redesenhados.

## Extração

`music_agent_runtime/lifecycle.py` é uma unidade sem estado com duas primitivas: remoção condicional pelo dono da task e cleanup best-effort do player. O módulo não importa Discord/Wavelink e recebe o tipo do player por binding. Registry de tasks, estados por guild, tokens de playback e event loop continuam em `MusicAgent`.

A release do Phone Worker inclui `music_agent_runtime/__init__.py` e `music_agent_runtime/lifecycle.py` como 0644 tanto no registry do worker quanto na lista do publisher. O primeiro gate após a extração detectou a ausência desses arquivos no publisher; a distribuição foi corrigida antes do gate aceito.

## Validação executada

- `tests/test_music_agent_lifecycle.py`: 11 passed antes da extração; 12 passed após incluir o teste unitário do módulo.
- lifecycle + release modular: 41 passed.
- lifecycle + release + boundaries + streaming TTS: 62 passed.
- `compileall` do Phone Worker, Python APK, automation e teste de lifecycle: exit 0.
- Suíte global nesta sessão: bloqueada na coleta por `discord` ausente (3 errors, 1 skipped); a mesma execução na rodada 28 limpa produz os mesmos 3 errors/1 skipped. Não é regressão atribuída à rodada 29.
- `ruff` não está instalado e a tentativa de instalar `ruff`, `discord.py` e `wavelink` falhou por ausência de rede. O gate Java obrigatório também não pode ser repetido porque os JARs externos não estão no pacote/host desta sessão.

Nenhuma publicação, instalação, job remoto, Gradle/APK real ou medição de latência foi executada. A VPS continua sem depender do worker e nenhum contrato de engine/cache/formato/parâmetro/scheduling TTS foi alterado.

## Próximo passo concreto

Ampliar a análise do lifecycle ainda no Music Agent para transições Lavalink, encerramento completo e TTS async; depois seguir self-builder Python, MainActivity/UI e discovery conforme o handoff. Antes de uma entrega final, repetir suíte global, Java obrigatório e ruff num ambiente com as dependências registradas.
