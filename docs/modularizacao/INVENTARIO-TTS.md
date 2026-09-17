# TTS — inventário antes da extração de política

Entrada: checkpoint 20, commit `87e57527e72e611061ed52f6d00fa10c83f0397a`.
Os 576 arquivos/modos e 430 hashes do pacote conferiram antes de editar. O
entrypoint continua em `deploy/termux/phone-worker/phone_worker.py`.

| Grupo existente | Dono e consumidores | Contrato / limite |
| --- | --- | --- |
| Normalização de rate/pitch/language, engine, formato e chave | Helpers e métodos de WorkerHandler; síntese normal, benchmark e tts_transport | Mesmo texto, aliases e SHA-256 `tts-v2`; callbacks reatribuídos continuam visíveis |
| Ordem de engines | Facade lê preferência de env; handler filtra disponibilidade | Teto explícito primeiro; Piper não anunciado; fallback somente ao trocar de engine |
| Admissão e contadores | `_TTS_AGENT_LOCK` e sete escalares no facade; síntese e streaming v2 | Reserva atômica, sem fila persistente; recusa não conta como síntese iniciada |
| Síntese padrão e cache | Handler; arquivos MP3/WAV/OGG | Cache antes do provider; hit preserva bytes, digest e metadados; leitura falha deve permitir sintetizar pela engine solicitada |
| Cache direto JSON | `_task_tts_cache_lookup/store` | Perfil/capacidade, checksum e limites; publicação via replace; falha deve limpar temporário e preservar destino |
| Cache binário / streaming v2 | `tts_transport.py`, chamando helpers vivos do handler/facade | Shared file lock, cache atômico, primeiro áudio antes do fim, EOF só em sucesso; código preservado nesta rodada |
| Manutenção | Executor de uma thread, lock, pending/running/touches no facade | Passes coalescidos por categoria; prune fora da resposta; touch com throttle de 30 s e mapa limitado a 4096 |
| Prune | `_prune_audio_cache` | Scandir, limite bytes/arquivos, idade 180 s, flock/inode e proteção de arquivo; permanece no facade |
| Android / Teto | Requests locais, renderer lazy e `_HEAVY_RESOURCE_LOCK` | Preservar raw→JSON, timeouts, fingerprint e exclusão de build/manutenção |
| Bot na VPS | `cogs/tts/` e testes de runtime/fallback existentes | Edge/gTTS continuam disponíveis sem worker; nenhum arquivo do bot será movido nesta rodada |

O caminho padrão ainda coleta o snapshot de dependências antes da admissão e
novamente para compor o resultado, inclusive em hit. Isso é comportamento recebido;
não foi acrescentada coleta pela extração nem feita otimização desse caminho.
Cache padrão grava antes de responder e agenda somente a poda. Streaming v2
tem fronteiras e cancelamento próprios; não mover seu IO na mesma extração.

Cobertura da entrada: streaming HTTP real local, primeiro áudio/EOF, autenticação,
upload binário, admissão concorrente, transporte lazy, benchmark Android e bot
sem worker. Faltavam testes diretos do ciclo padrão frio/quente, aliases versus
fallback, IO opcional do cache e coalescência/rejeição de manutenção.

Os novos testes usam arquivos temporários e fronteiras de provider controladas.
Clock determinístico verifica contratos e ordem das operações; não representa
medição de latência real. Não houve Edge/gTTS/Android/Teto/ffmpeg remotos ou reais.
Antes de mover IO PCM: stderr saturado e preparos simultâneos ainda são pendências.
