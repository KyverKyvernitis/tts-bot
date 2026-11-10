# Validação local do TTS Rust

Os gates desta página são executados em desenvolvimento/CI. Não execute Cargo,
Maturin ou benchmarks de carga na VPS de produção de 1 GB.

## Build e gate obrigatório

```bash
python scripts/build-tts-native.py --profile release
python scripts/check-tts-native.py
python scripts/benchmark-tts-native.py --runs 200 --warmup 3
```

Use um ambiente virtual que contenha Maturin, pytest, pytest-asyncio e as
dependências do bot. FFmpeg e um compilador C/C++ são necessários. O toolchain
Rust está fixado em `rust-toolchain.toml`; `Cargo.lock` deve acompanhar a fonte.

O build gera wheel, serviço `tts-worker` e helper `tts-worldline-native`, instala
o wheel no Python usado e copia a mesma extensão para o caminho do loader
existente. O relatório `native-dist/<target>-build.json` registra alvo, perfil,
tamanho, SHA-256, versão do compilador e início/fim em UTC. Ele é uma evidência de build local; a instalação por manifesto
e o rollback do worker são descritos em `tts-native-worker.md`.
Artefatos GNU/Linux são inspecionados com `readelf --version-info`; por padrão,
o build rejeita referências obrigatórias que exijam GLIBC superior a 2.35,
inclusive dentro do wheel. Referências ELF `WEAK` usadas por sondas opcionais do
Rust não elevam esse mínimo. A verificação também se aplica ao ARM64 compilado
em runner mais novo.
Sem Zig, o wheel privado usa a tag `linux_<arquitetura>`: o mínimo de GLIBC é
comprovado pelo gate de referências obrigatórias, pois o auditwheel pode incluir
sondas `WEAK` ao escolher uma tag manylinux. Essa tag Linux não representa uma
certificação manylinux nem uma publicação para distribuição pública.
Nesse caso, instalar `maturin[zig]` e `cargo-zigbuild` no ambiente de build e usar
`--zig` faz ambos os artefatos GNU/Linux linkarem contra a GLIBC máxima escolhida.
O workflow usa essa opção no runner ARM64 mais novo. Alterar `--max-glibc` é uma
decisão explícita de compatibilidade, não um contorno automático da verificação.

Para compilar somente a extensão durante desenvolvimento:

```bash
python scripts/build-tts-native.py --profile dev --wheel-only
```

O gate carrega a extensão antes de executar qualquer teste: ausência de `.so`,
protocolo incompatível ou exports obrigatórios ausentes causam erro. Os contratos
de replay são executados com `TTS_REPLAY_RUST=1`, de modo que uma extensão ausente
não possa transformar esse gate em `skip`. O gate Rust exclui o executável de
testes da crate PyO3, pois o binding é validado carregado pelo Python real.
FFmpeg e o binário do worker também são obrigatórios; um skip inesperado nos
arquivos de integração nativa faz o relatório final falhar mesmo se pytest tiver
retornado zero. O gate inclui os diferenciais PCM do CD, a cadeia de provider
local até source/FFmpeg nativo, a política nativa e o adapter do agente musical.

Cada arquivo pytest legado roda em um processo separado. Algumas suítes do ZIP
substituem `discord` ou provedores em `sys.modules` durante a coleta; combiná-las
no mesmo processo pode produzir falhas que não existem quando isoladas. O
relatório `native-dist/validation.json` mantém o resultado e a saída de cada
comando, inclusive skips e falhas. Uma contagem histórica não substitui a execução
atual. Ao iniciar o gate, o relatório anterior é substituído por um resultado
`passed=false`, `completed=false`; uma falha de preflight não deixa um resultado
antigo com aparência de sucesso atual. O relatório também registra início/fim em
UTC e caminho, tamanho e SHA-256 da extensão e do worker usados. Testes excluídos
por plataforma ou ignorados não são evidência de execução aprovada nessa plataforma.
Alteração desses artefatos durante os testes também reprova o gate, identificada
por `artifacts_unchanged=false`.

## O que o benchmark mede

O benchmark usa o mesmo MP3 sintético nos dois backends e verifica SHA-256 e
quantidade de bytes do replay/PCM. Os cenários padrão são RAM, spill, dois
leitores independentes e replay progressivo alimentando FFmpeg real. O cenário
`ffmpeg_native_source` compara a ponte de leitura/escrita Python com o caminho
Rust que mantém os bytes comprimidos dentro do replay e alimenta o decoder sem
voltar ao Python; ambos completam o último frame de 20 ms com silêncio. Cada par
backend/cenário roda em um processo novo e inclui aquecimento separado.

São reportados p50/p95 de primeiro bloco ou primeiro frame PCM, tempo total, CPU
do processo, CPU dos filhos e pico RSS do processo. O RSS inclui o interpretador
Python e código nativo importado; é um high-water mark, não uma medição isolada
do alocador Rust. O relatório mantém as amostras e o SHA da extensão usada em cada
processo. `single_native_artifact=false` indica troca da extensão entre cenários;
`native_artifact_unchanged=false` indica alteração do arquivo durante um cenário.
Nesses casos, repetir o ensaio com o artefato fixado antes de aceitar desempenho.
Início/fim UTC do ensaio e de cada processo permitem identificar a ordem sequencial
e relacionar as medições ao build validado.

O adapter de replay ainda agenda as operações do produtor sintético com o
executor de threads Python. O cenário de source nativo elimina o retorno dos
bytes comprimidos na reprodução, mas nenhum cenário representa a rede real dos
provedores. Um resultado local
mais lento deve ser preservado e investigado, nunca renomeado como ganho.

O benchmark não acessa Edge, Google, o gateway Discord ou o worker remoto. Ele
não mede áudio ouvido pelo usuário, `message_to_first_frame` do bot em produção,
limites dos provedores ou contenção real entre servidores. A meta de 20% é uma
meta de engenharia, não um resultado pré-aprovado. Usar menos de 200 execuções
marca `acceptance_sample_size=false`; isso serve somente como smoke test.

## Alvos e limites da evidência

O workflow executa contratos Linux x86-64 e ARM64 e compila o serviço para Android
ARM64/Bionic com NDK. Android recebe apenas `tts-worker`; o helper WORLDLINE deve
ser Linux/glibc para o Ubuntu ARM64 do telefone. Um build Android não demonstra
execução no aparelho. A validação real do APK TTS, PRoot/WORLDLINE, ESPER, SSH,
voz compartilhada e latência Discord deve ocorrer nos ambientes correspondentes.

O piloto em um servidor/worker e a observação de 48 horas exigem esses ambientes.
Nenhum script desta entrega ativa backends ou publica releases em produção.

## Resultado conferido em 2026-11-10

O gate local, de 15:16:11 a 15:16:47 UTC, aprovou os 17 grupos de comandos:
106 testes Rust e 148 testes Python, sem falhas, skips ou testes ignorados.
Os hashes da extensão e do worker permaneceram iguais durante a execução.
Isso inclui os gates obrigatórios de imports estritos, PCM do CD, política,
provider/source com FFmpeg e agente musical. As referências NumPy também estavam
disponíveis nesta execução.

O host era Linux x86-64/glibc 2.41, Python 3.12.14 e FFmpeg 7.1.5. O build usou
perfil **dev**; estes números não certificam desempenho de um release otimizado.
A inspeção ELF verificou GLIBC obrigatória 2.34 na extensão, no worker e no helper,
abaixo do máximo configurado de 2.35. O wheel privado foi reempacotado com a tag
`cp310-abi3-linux_x86_64`, sem substituir os binários usados no gate/benchmark.

- Extensão: SHA-256 `dc3a30fa7d1d80138e944ffd141aa3d3ed61e64d7dbeb517783f30aa81ac81e5`.
- Worker: SHA-256 `c7cb03b1fce62bf3b9e03fa4ad525b4041260f7deb1c694b825336c1921605a9`.
- Wheel privado: SHA-256 `7057d369afb9dcf1721f0f9909a65faf1f068cb2a46ca99bf74e6469e2c44ea3`.

O benchmark final foi executado uma vez, de 15:19:34 a 15:20:38 UTC, após encerrar
os builds/testes concorrentes. Cada backend/cenário teve 200 amostras medidas e
3 aquecimentos, com MP3 sintético de 2 s, blocos de 1024 bytes, orçamento de 64 KiB
(512 bytes no spill) e produtor sem atraso artificial. A extensão foi a mesma em
todos os processos e permaneceu inalterada. Replay e PCM tiveram SHA-256 e tamanho
idênticos entre os backends em todos os cenários.

| Cenário | Métrica p95 | Python (ms) | Rust (ms) | Variação Rust/Python |
| --- | --- | ---: | ---: | ---: |
| RAM | Primeiro bloco | 0.0311 | 0.1239 | +298.4% |
| Spill | Primeiro bloco | 1.1669 | 0.9686 | -17.0% |
| Dois leitores | Primeiro bloco | 0.0432 | 0.0763 | +76.6% |
| FFmpeg progressivo | Primeiro frame PCM | 75.4689 | 84.2782 | +11.7% |
| Source nativo | Primeiro frame PCM | 79.8322 | 81.2437 | +1.8% |

Os valores a seguir usam a ordem Python/Rust. CPU é p95 por amostra; RSS é o pico
do processo, incluindo Python e a extensão. A CPU dos filhos inclui FFmpeg.

| Cenário | CPU do processo (ms) | CPU dos filhos (ms) | Pico RSS (MiB) |
| --- | ---: | ---: | ---: |
| RAM | 0.1973 / 0.6187 | 0 / 0 | 17.50 / 23.76 |
| Spill | 4.8912 / 5.1793 | 0 / 0 | 18.00 / 23.88 |
| Dois leitores | 0.3566 / 0.5556 | 0 / 0 | 17.50 / 23.74 |
| FFmpeg progressivo | 2.4686 / 3.3880 | 81.2010 / 88.8821 | 18.13 / 23.81 |
| Source nativo | 2.4016 / 14.0575 | 85.8595 / 89.3945 | 18.13 / 24.41 |

Esta execução não demonstrou a meta de ganho de 20%. O spill teve redução local
de p95 de aproximadamente 17%; os outros cenários apresentaram regressão nessa
métrica. As amostras, p50/p95 completos e proveniência estão em
`native-dist/benchmark.json`; o resultado por comando está em
`native-dist/validation.json`. Esses relatórios locais são regeneráveis pelos
comandos acima; os limites de rede, Discord, aparelho e perfil dev continuam
aplicáveis.

## Falhas conhecidas na base recebida

A execução isolada de CD na base original do ZIP também falha em
`test_local_pcm_skips_voice_handoff_before_remote_side_effects`, em
`tests/test_cd_progressive_integration.py`: o stub de owner omite os métodos de
métrica que o caminho de retorno antecipado já chamava. A seleção correspondente
registrou 62 passes e essa falha, confirmada antes de atribuí-la à migração. O
fixture e a produção não foram modificados para esconder o resultado.

Os testes antigos de helpers/fluxo de mensagens também têm divergências de
expectativa sobre cooldown Edge e a voz TTV padrão; contaminação de `sys.modules`
por suítes combinadas deve ser separada dessas falhas. O gate nativo executa
arquivos isoladamente e conserva as saídas para distinguir origem e regressão.
