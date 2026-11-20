# Serviço TTS nativo no Phone Worker

`TTS_NATIVE_WORKER=1` seleciona um processo Rust separado para as rotas TTS.
O padrão continua desligado. O processo Python existente conserva identidade,
pareamento, heartbeat, updater e jobs/música. A ponte `native_tts.py` encaminha
HTTP por socket Unix privado: não decodifica áudio, não reconstrói chunks HTTP e
não escolhe motores. O socket tem modo 0600 e não abre outra porta de rede.

## Build e instalação separados

Compile em CI ou máquina de desenvolvimento, nunca na VPS de 1 GB:

```bash
python scripts/build-tts-native.py --binaries-only --target x86_64-unknown-linux-gnu
python scripts/install-tts-native.py native-dist/x86_64-unknown-linux-gnu/manifest.json
```

No Termux use `aarch64-linux-android`. O helper WORLDLINE executa no Ubuntu
ARM64 existente e exige outro artefato `aarch64-unknown-linux-gnu`; um ELF glibc
não substitui o executável Android/Bionic. Não instalar o helper glibc como
serviço Android. Os motores externos, bibliotecas e voicebanks permanecem nos
caminhos existentes.

O manifesto separado possui `schema: tts-native-artifact-v1`, `version`, `target`
e `artifacts` com `name`, `file` (basename local) ou `url` HTTPS, `bytes` e
`sha256`. Nomes aceitos: `tts-worker` e `tts-worldline-native`. Releases remotas
devem conservar a origem HTTPS do manifesto, inclusive nos redirects. O token
opcional fica em `TTS_NATIVE_ARTIFACT_TOKEN`, nunca no manifesto/ZIP. O argumento
`--manifest-sha256` aceita o digest confiável do canal de entrega.

O instalador valida tamanho, SHA-256, tipo ELF e arquitetura antes de promover.
Faz staging e publicação imutável por hash, mantém `current`/`previous`, valida
o HTTP real do candidato e restaura ambos os ponteiros se a confirmação falhar.
Uma transação interrompida é restaurada antes da próxima instalação. A pasta
padrão é `~/.local/share/tts-native`; `--runtime-root` permite outra pasta.
Instalar não reinicia o bot ou o telefone. Planeje a troca em janela sem áudio.

Os binários não entram no ZIP antigo do worker: seus limites permanecem em
64 membros, 8 MiB comprimidos e 32 MiB expandidos. A ponte Python ocupa um slot
antes usado somente pelo `__init__.py` documental de `runtime_telefone`; o
checkout conserva esse arquivo e o pacote instalado funciona como namespace.
A release completa continua sendo testada com o bootstrap original 1.0.0.

## Ativação

Depois de instalar e validar no alvo, adicione ao ambiente privado existente:

```dotenv
TTS_NATIVE_WORKER=1
TTS_NATIVE_RUNTIME_ROOT=/caminho/privado/tts-native
```

O root padrão dispensa a segunda variável. O supervisor verifica o manifesto e
o hash do binário instalado a cada partida. `TTS_NATIVE_WORKER_BINARY` permite
um executável de desenvolvimento explícito; nesse caso, forneça opcionalmente
`TTS_NATIVE_WORKER_SHA256`. `TTS_NATIVE_WORKER_SOCKET` permite um caminho Unix
privado livre com até 103 bytes; o padrão inclui o PID do processo proprietário.

O launcher consulta a prontidão até o processo correto responder, sem uma
espera fixa antes da primeira checagem. No modo nativo, o orçamento é de pelo
menos 15 segundos, cobrindo a preparação inicial dos bancos de voz. Valores
maiores de `PHONE_WORKER_START_WAIT_SECONDS` são preservados;
`PHONE_WORKER_NATIVE_START_TIMEOUT_SECONDS` pode ampliar o prazo nativo.
Uma resposta de outro PID ou um status antigo continua sendo recusado.

Um pedido explícito de `--restart` aguarda o lock de outra inicialização
por até 30 segundos; `PHONE_WORKER_START_LOCK_WAIT_SECONDS` ajusta esse prazo.
Se não conseguir o lock, retorna falha e informa que o reinício não ocorreu.
As verificações normais do watchdog continuam podendo encerrar sem reiniciar
quando outra inicialização está em andamento. A aquisição e a liberação são
serializadas; locks com dono vivo são preservados.

O supervisor inicia o filho com `--owner-stdin`: fechar o pipe do pai encerra o
serviço, inclusive se o Python morrer sem executar seu cleanup. Conexões
interrompidas cancelam a fonte nativa. O serviço limita requisições/conexões,
filas internas, prazos e bytes. Não reintroduz thresholds de bateria/RAM que o
worker atual removeu.

`/tts-agent/synthesize.stream` usa MP3/chunked v2 e só envia o terminador HTTP
depois do EOF normal confirmado pelo provider. Falhas após o primeiro áudio
fecham a resposta sem terminador; clientes não devem guardar esse áudio parcial
como sucesso. Cache-only miss devolve HTTP 409 sem consultar a rede. Cache bruto
exige Content-Length e SHA-256 e só se torna visível por rename após validação.

Uma falha de partida deixa os serviços de controle disponíveis. Requisições
TTS nativas falham explicitamente; elas não voltam silenciosamente aos motores
Python. Desativar a flag e reiniciar pelo launcher existente restaura o caminho
legado. Para reverter o binário, escolha a release anterior validada no fluxo de
instalação e reinicie em janela sem áudio; caches, `.env`, tokens e voicebanks
ficam fora das releases.

## Tempo até o primeiro áudio

Cada pedido Teto reaproveita o índice do banco de voz ao calcular a chave de
cache e montar a renderização. O próximo pedido lê o banco de novo para
reconhecer mudanças. A leitura do índice roda fora das threads que atendem
as conexões do worker. A resposta de fala Teto continua em WAV completo.

No Music Agent, o stream MP3 nativo de Edge e gTTS usa uma sondagem inicial de
512 bytes. `TTS_STREAM_FFMPEG_PROBESIZE_BYTES` permite ajustar esse valor.
Quando a rota direta precisa abrir uma conexão de voz, o agente pode preparar
o primeiro PCM enquanto a conexão é estabelecida. Para pedidos compatíveis,
o Phone Worker envia o texto ao Music Agent se ambos usarem o mesmo socket
nativo. `PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_WITH_TTS_AGENT=true`
mantém a passagem por áudio completo. Uma interrupção confirmada após o
primeiro frame encerra a fala com erro para evitar que a VPS a repita.

Em `/health` na VPS, `tts_metrics.latency_percentiles_ms` contém
`worker_command_to_first_frame` e recortes por motor e por stream Rust. Esses
tempos começam ao chegar ao Music Agent e terminam no primeiro frame entregue
ao player. Para rotas com áudio pré-renderizado, a síntese anterior à chamada
do agente fica fora desse tempo. `message_to_first_frame` continua medindo o
caminho com player na VPS. O atraso até o ouvinte depende também da transmissão
e do cliente Discord.

Para comparar antes e depois, use a mesma rota, voz, efeitos e frases, com
conexão fria e já aberta em lotes separados. Registre acertos de cache e erros
junto com p50, p95 e número de amostras. O `/health` resume uma janela móvel da
execução atual, então use janelas distintas para cada versão.

### Resultados locais

| Cenário | p50 anterior → novo | p95 anterior → novo |
| --- | ---: | ---: |
| MP3 no Music Agent, voz aberta | 326 → 198 ms | 340 → 241 ms |
| MP3 no Music Agent, abertura de voz simulada em 160 ms | 485 → 195 ms | 547 → 226 ms |
| Teto sem cache da resposta, rodada inicial | 200 → 87 ms | 302 → 523 ms |
| Teto com cache da resposta, rodada inicial | 116 → 76 ms | 177 → 431 ms |
| Teto sem cache da resposta, repetição | 186 → 96 ms | 277 → 162 ms |
| Teto com cache da resposta, repetição | 105 → 66 ms | 124 → 93 ms |

O benchmark do Music Agent usa FFmpeg real, MP3 de teste servido por socket
Unix e 30 pedidos por caso e versão. O PCM foi idêntico. Para reproduzir,
execute `python scripts/benchmark-tts-worker-first-audio.py --baseline-zip ARQUIVO.zip`
com o ZIP de origem e as dependências do bot instaladas.

O benchmark da Teto mede o primeiro byte do WAV servido pelo worker Rust em
release. Usa 5 mil entradas geradas para o banco de voz e um resampler de
teste. Foram 60 pedidos por caso e versão na rodada inicial e 40 na repetição,
com a ordem das versões invertida. Os WAVs foram idênticos. A mediana melhorou
nas duas rodadas. O p95 melhorou na repetição e piorou na rodada inicial,
quando um lote da versão nova teve picos de mais de um segundo. Para
reproduzir, rode `cargo run --locked --release -p tts-worker --example benchmark_ttv_startup -- 30 5000`
com o mesmo exemplo nas duas árvores de código e as mesmas entradas de build.
Os dois ensaios medem etapas locais. A latência percebida no Discord ainda
precisa ser medida no telefone com os provedores e as vozes do uso normal.

## Validação

```bash
cargo test -p tts-worker
cargo build -p tts-worker
python -m pytest -q tests/test_tts_native_worker_platform.py tests/test_phone_worker_modular_release.py
```

O gate cobre sockets reais, processo Rust real, fechamento do pipe proprietário,
cache-only, integridade de artefatos, rollback e respostas chunked truncadas.
Testes de processo exigem `target/debug/tts-worker` ou
`TTS_NATIVE_WORKER_TEST_BINARY`; sem isso aparecem explicitamente como skip.
Linux x86-64 não substitui testes de APK/engine Android, Termux ARM64, guest
glibc, Discord real ou audição das voicebanks instaladas.
