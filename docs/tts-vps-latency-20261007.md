# Otimizações de TTS na VPS — 7 de outubro de 2026

Implementação sobre `repo-20261007-180638.zip`, com foco em Edge/gTTS e no
comando `_advanced`. SHA-256 da base:
`945657ea3ac9d0bec7051cadd3ee262d364ed4d13c3aefe37163dbb91a567a50`.

## Mudanças

- O fallback Edge → gTTS conserva o snapshot dos efeitos, inclusive quando o
  FFmpeg começa antes da chegada do áudio. Os efeitos continuam pessoais e a
  síntese bruta permanece compartilhada entre variantes.
- O encoder do cache preparado transcodifica MP3/WAV corretamente. A preparação
  descarta cabeçalhos Ogg e armazena somente pacotes Opus de áudio, com um cursor
  independente por reprodução. Opções de entrada e saída fazem parte da chave.
- Variantes frequentes com efeitos também podem usar o cache preparado. Há um
  encoder, fila de até oito pedidos e orçamento padrão de 8 MiB. O trabalho
  aguarda ociosidade e é interrompido se uma fala atual precisar dos recursos.
  Arquivo aberto, flock e verificação de inode protegem áudio em uso/substituído.
- gTTS curto também pode usar streaming, sobrepondo a abertura do FFmpeg à
  espera HTTP. A sondagem de MP3 progressivo conhecido passou a 512 bytes.
- A antecipação gTTS preserva uma vaga para falas atuais. Com apenas uma vaga,
  o pedido antecipado aguarda promoção; timeout/cancelamento não liberam uma
  vaga física enquanto a requisição bloqueante ainda estiver encerrando.
- O roteador local aceita sources previamente preparados, preservando a decisão
  de posse da voz e ducking. Para música no mixer local, um leitor PCM em thread
  alimenta uma fila limitada a oito frames de 20 ms; a thread musical consome
  somente frames disponíveis. Underrun retorna silêncio e não conta como
  primeiro áudio. EOF, falha do decoder e síntese incompleta são distintos.
- FIFO musical é ativado antes de abrir o decoder. A posse do mixer é validada
  antes do despacho e novamente após aguardar o primeiro PCM. Cancelamento
  remove somente o overlay TTS.
- Filtros FFmpeg personalizados e efeitos usam um único graph. Opus com filtros
  exige transcodificação. O timeout considera Slowed, velocidade Edge e a cauda
  do Reverb, mantendo o teto de duração e a margem de segurança existentes.
- Save/reset de `_advanced` reconhecem a interação antes de persistir. Mudanças
  sem alteração de nível não são gravadas; atualizações de outros painéis são
  agrupadas em segundo plano.

## Configuração

Valores explícitos no ambiente continuam prevalecendo.

| Variável | Padrão atual | Ajuste disponível |
| --- | --- | --- |
| `TTS_GTTS_STREAM_MIN_CHARS` | `1` | `101` recupera o caminho de arquivo para falas curtas. |
| `TTS_STREAM_FFMPEG_PROBESIZE_BYTES` | `512` | `2048` recupera a sondagem anterior. Opções FFmpeg explícitas prevalecem. |
| `TTS_GTTS_PREFETCH_CONCURRENCY` | `1` | Limitado à concorrência total menos uma; `0` exige promoção antes de sintetizar. |
| `TTS_PREPARED_OPUS_CACHE_ENABLED` | `true` | `false` desativa a preparação em segundo plano. |
| `TTS_PREPARED_OPUS_CACHE_MAX_BYTES` | `8388608` | Orçamento global de pacotes preparados. |

O cache preparado também mantém teto de 128 entradas, 512 KiB/40 segundos por
frase, TTL de 600 segundos e no máximo oito preparações pendentes. Música local
continua exigindo PCM; pacotes Opus preparados são usados em reprodução sem mix.

## Medição

O health snapshot em `tts_metrics.latency_percentiles_ms` conserva os indicadores
anteriores e inclui:

- `message_to_first_frame`: entrada no handler da mensagem até frame observado
  pelo player, incluindo triagem e montagem do payload;
- `total_to_first_frame`: despacho até frame observado, com recortes por motor,
  origem do áudio, conexão aberta/fria e prioridade;
- `edge_first_byte` / `gtts_first_byte`: primeiro byte do pedido compartilhado;
- `edge_network_first_byte` / `gtts_network_first_byte`: primeiro byte descontando
  espera por vaga;
- `edge_prebuffer_wait` / `gtts_prebuffer_wait`: tempo adicional até prontidão do
  buffer; `source_prime` continua medindo a preparação do primeiro frame.

O primeiro frame observado é uma medida do player local; não mede o instante em
que outro usuário ouve o áudio no Discord. Silêncio inserido durante underrun do
overlay não alimenta essa medição.

## Validação

- **422 testes TTS passaram**, com 220 subtests. Incluem as 28 combinações de
  efeitos, fallback progressivo, chave de cache, timeout, painel, cancelamento,
  admissão, streaming e transporte.
- **91 testes de integração musical/domínio passaram**, incluindo buffer PCM,
  sources preparados, troca de backend, cancelamento e FIFO com FFmpeg real.
- MP3/WAV preparados com e sem DSP e com `-ss 0.2` foram comparados byte a byte
  com encoding frio equivalente. Libopus real decodificou os pacotes preparados.
- O teste completo `_resolve_audio_path → _play_file → router → FIFO/FFmpeg →
  mixer` confirmou primeiro PCM antes do fim do provedor simulado, ducking,
  preservação da voz/música, remoção do FIFO e encerramento do leitor.
- O ZIP de patch passa pelo verificador estrutural do atualizador e é comparado
  byte a byte com os arquivos implementados.

Três problemas de testes já existiam no ZIP original: `test_tts_route_telemetry.py`
extrai métodos que mudaram de classe; um teste exige o arquivo removido
`cogs/tts/utils/history.py`; outro exige o texto antigo do launcher. O primeiro
arquivo e os dois casos foram excluídos da suíte ampliada acima. Esses testes
legados não foram modificados. O teste de fallback por arquivo agora desativa
streaming explicitamente para continuar verificando esse caminho, e há novos
testes para o fallback progressivo padrão.

Ambiente: Python 3.12.14, discord.py 2.7.1, edge-tts 7.2.8, gTTS 2.5.4 e FFmpeg
7.1.5. A reprodução externa no Discord e os provedores online não foram usados.

## Benchmark reproduzível

`scripts/benchmark-tts-local.py` usa FFmpeg e discord.py reais, MP3 sintético e
gTTS simulado, com deadline e limpeza de processos/arquivos. Foram executadas
sete repetições mais um aquecimento por caso, em 18 casos, sem falhas. O JSON
completo da base e da implementação está em
`docs/benchmarks/tts-vps-latency-20261007.json`.

| Cenário offline sem DSP | Antes: p50 / p95 | Depois: p50 / p95 |
| --- | --- | --- |
| gTTS curto, primeira leitura do AudioSource | 179,8 / 184,1 ms | 123,3 / 123,7 ms |
| MP3 progressivo, primeiro PCM, probe 2048 → 512 | 123,2 / 123,9 ms | 90,4 / 90,4 ms |

A simulação gTTS entrega a resposta inteira após 120 ms. O MP3 progressivo chega
em chunks de 512 bytes a cada 8 ms, após 80 ms iniciais. Probes distintos e os
dois thresholds produziram o mesmo PCM dentro de cada combinação de efeitos.
Os ganhos acima são de preparo local; o p95 vem de amostra pequena. O resultado
de produção depende da VPS, da rede, do provedor e da carga de reprodução.

```bash
python scripts/benchmark-tts-local.py --runs 30 > tts-benchmark-local.json
```

Para medir a VPS, comparar os percentis do health snapshot com cache frio/quente,
Edge/gTTS, efeitos desligados/máximos, conexão aberta/fria, várias guilds e
música local. Acompanhar cortes, falhas, fila, CPU e RAM junto dos tempos.

## Aplicação

`tts-vps-latency-patch-20261007.zip` contém somente arquivos alterados ou novos,
com caminhos relativos à raiz do projeto, e pode ser aplicado pelo fluxo usual
do atualizador. É necessário reiniciar o bot para carregar código e padrões.
O ZIP completo é uma cópia do projeto para checkout/substituição manual; para o
atualizador, usar o patch, que respeita seu limite de entradas.
