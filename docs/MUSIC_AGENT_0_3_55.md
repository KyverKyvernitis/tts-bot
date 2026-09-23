# Music Agent 0.3.55 — início, transições e qualidade

## Alterações

- O helper persistente de yt-dlp atende também buscas textuais e a resolução musical de resultados Spotify. Mantém o fallback existente dentro do mesmo prazo total.
- Uma solicitação interativa promove a resolução compartilhada que estava em prefetch. Quando necessário, interrompe uma resolução especulativa de outra música para liberar a vaga.
- URLs assinadas de `*.googlevideo.com` respeitam `expire`, com margem de 60 s e idade local máxima de 30 min. Outras origens continuam usando os TTLs anteriores. Isso evita resolver novamente uma URL ainda válida apenas porque a faixa anterior durou mais de dois minutos.
- A reação de carregamento começa em segundo plano e não atrasa o início do trabalho musical.
- O decoder de música alimenta um buffer PCM limitado. Se a rede parar temporariamente, o mixer continua atendendo TTS; silêncio de espera não confirma o início da música. Um stall prolongado gera erro para a recuperação existente.
- A próxima faixa pode ter seu decoder preparado nos últimos 12 s da atual. O padrão permite apenas uma preparação adicional em todo o agente. A fonte é reutilizada somente quando identidade, URL, posição e idade continuam válidas.
- O mixer persistente atualiza o bitrate Opus na thread de áudio antes de codificar o próximo frame. Continua respeitando o limite do canal, sem recriar a sessão de voz ou interromper overlays TTS.
- A escolha padrão compara codec, bitrate e taxa de amostragem entre formatos já retornados pelo yt-dlp, mantendo idioma/preferências. Seletores personalizados continuam sendo respeitados.
- O ducking tem entrada e retorno graduais. A soma de música e TTS reserva margem de pico antes de misturar as fontes, reduzindo saturação.
- No padrão, EOF reconecta apenas transmissões ao vivo. HTTP 403/404/429 não provocam reconexões repetidas pelo FFmpeg. Não há implementação de `Retry-After` neste patch.

## Limites e configurações

A reprodução, o yt-dlp e o FFmpeg continuam no telefone. Lavalink continua dedicado a metadata. Não há download integral das músicas, ampliação automática da playlist ou processamento de áudio na VPS.

Cada buffer padrão armazena no máximo 75 frames de 20 ms: 1,5 s de PCM estéreo a 48 kHz, ou 288.000 bytes. Esse limite é da fila PCM; o processo FFmpeg e seus buffers internos consomem memória adicional. Preparar a próxima faixa acrescenta temporariamente um decoder no telefone.

Os novos valores estão documentados em `cogs/musica/runtime_telefone/termux/musica.env.example`. Funcionam também quando as variáveis não existem no ambiente instalado. Não é necessário substituir o `.env` de produção.

- `MUSIC_AGENT_NEXT_AUDIO_PREPARE_ENABLED=false` desliga a preparação antecipada.
- `MUSIC_AGENT_PCM_BUFFER_ENABLED=false` restaura a leitura síncrona de PCM e desativa a preparação que depende do buffer.
- `MUSIC_AGENT_FIRST_FRAME_TIMEOUT_SECONDS=8` é um limite de falha, não um atraso fixo. A configuração antiga `MUSIC_AGENT_DIRECT_CONFIRM_SECONDS` deixa de controlar essa confirmação.
- Se existir `MUSIC_AGENT_FFMPEG_BEFORE_OPTIONS` personalizado, ele continua prevalecendo. Revise manualmente eventuais opções antigas `-reconnect_at_eof 1` e listas HTTP que incluam 403/404/429.

## Validação

- 532 testes de música passaram; um teste já marcado para pular permaneceu pulado.
- 181 testes de distribuição do Phone Worker, ciclo de vida TTS, estado de voz, regressões e streaming TTS passaram.
- Integração com FFmpeg e discord.py reais, usando HTTP local: 50 frames corretos para uma faixa de 1 s, EOF sem repetição e resposta 403 sem loop.
- Testes específicos cobrem prioridade, assinatura expirada, limite do buffer, TTS durante bloqueio do decoder, invalidação/reutilização do áudio preparado, troca de bitrate e cancelamento durante seek.
- O pacote de distribuição do Phone Worker inclui os três módulos novos e permanece dentro do limite de 64 membros do bootstrap original.

Esses testes não medem latência real do telefone, da rede ou do Discord, nem substituem uma avaliação auditiva. Não foi realizada implantação remota.

## Aplicação do ZIP

O ZIP contém somente arquivos alterados ou novos, com caminhos relativos à raiz do repositório. Extraia sobre a mesma versão usada como base (`repo-20260923-144428.zip`) e utilize o fluxo de atualização já existente. Os arquivos de distribuição foram ajustados, mas nenhum job de atualização foi enviado.

Para observar o resultado, compare `resolve_slot_acquired.wait_ms`, `next_audio_ready`, `next_audio_reused`, `audio_pipeline_ready.transition_gap_ms`, `pcm_prepare_ms`, `play_start_ms`, `opus_encoder_updated` e os contadores `buffer_underruns`, `decoder_deadline_overruns` e `mix_limited_frames`. Não espere aumento da qualidade original da gravação: as mudanças reduzem esperas, interrupções e perdas introduzidas pela reprodução/mistura.
