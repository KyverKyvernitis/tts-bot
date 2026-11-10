# Runtime TTS em Rust

O workspace Cargo fica na raiz do projeto. O TTS usa bibliotecas Rust para
fluxo/replay, provedores, filas e preferencias, fonetica/prosodia, renderizacao
TTV e supervisao dos motores nativos. O bot, a persistencia compartilhada,
os objetos Discord e os servicos do telefone fora do TTS continuam Python.

## Caminho de audio

Na VPS, o binding PyO3 conserva o modulo `_nucleo_fluxo_tts` e a ABI CPython
3.10+. O produtor pode gravar diretamente no replay Rust; leitores independentes
reproduzem o mesmo audio, com RAM limitada e spill em arquivo. O source nativo
alimenta o FFmpeg e devolve frames PCM de 20 ms para o `discord.py`, que conserva
a conexao de voz compartilhada com musica.

No telefone, `tts-worker` atende por socket Unix privado e recebe as operacoes
encaminhadas pelo Phone Worker. WORLDLINE usa um helper Rust Linux/glibc no
Ubuntu ARM64; o servico Termux usa Android/Bionic. ESPER continua no host .NET
residente e Android TTS continua na API do APK existente. Os adaptadores nativos
nao executam os provedores, G2P ou renderizadores Python.

EOF normal, erro e cancelamento sao distintos. Nenhum consumidor pode publicar
cache parcial, reiniciar uma frase depois do primeiro audio ou cancelar uma
sintese compartilhada ainda usada por outro leitor. O canto conserva PCM mono
44,1 kHz; a reproducao Discord/mixer conserva PCM estereo 48 kHz.

## Desenvolvimento e validacao

Instale as dependencias Python em um ambiente virtual, com Rust, compilador
C/C++, FFmpeg e `readelf` na maquina de desenvolvimento ou CI:

```bash
python -m pip install -r requirements.txt maturin pytest pytest-asyncio
python scripts/build-tts-native.py --profile release
python scripts/check-tts-native.py
python scripts/benchmark-tts-native.py --runs 200 --warmup 3
```

O build verifica arquitetura e GLIBC maxima 2.35 antes de distribuir artefatos
GNU/Linux. Em host mais novo, use `maturin[zig]`, `cargo-zigbuild` e a opcao
`--zig`. Nao compile ou execute benchmarks de carga na VPS de producao de 1 GB.
`Cargo.lock` e `rust-toolchain.toml` fixam dependencias e compilador.

A fila original permanece disponivel como contrato separado de consumidor
unico; ela nao substitui replay:

```bash
cargo test --locked --workspace --exclude vinculo_fluxo_tts
cargo run --locked --example fluxo_local
```

## Selecao e compatibilidade

O backend legado continua sendo o padrao ate os gates do aparelho e do piloto.
`TTS_BACKEND=rust` seleciona os componentes nativos da VPS; extensao ausente ou
incompativel deve falhar explicitamente. Para validar uma fronteira por vez,
existem `TTS_POLICY_RUST`, `TTS_REPLAY_RUST`, `TTS_PROVIDERS_RUST`,
`TTS_SOURCE_RUST`, `TTS_RENDER_RUST` e `TTS_ENGINES_RUST`. Source Rust exige
replay Rust. A selecao ocorre antes de cada operacao e nao muda durante uma fala.

No telefone, `TTS_NATIVE_WORKER=1` habilita a supervisao e o encaminhamento para
o servico nativo. Binarios, voicebanks, motores e dados persistentes sao
distribuidos separadamente do pacote Python legado, sem aumentar seus limites.
O manifesto identifica alvo, tamanho e SHA-256; instalacao, saude e rollback usam
releases anterior/atual. Consulte [operacao do worker](docs/tts-native-worker.md).

O catalogo de idiomas Google e congelado de gTTS 2.5.4 como dados; o cliente
nativo implementa o protocolo Google Translate existente e preserva sua
tokenizacao. O Edge conserva os endpoints administrativos e o token v2.
Os motores externos e formatos de configuracao existentes sao preservados.

## Evidencia e ativacao

Os gates locais exigem a extensao real, com testes de fluxo, filas, PCM,
provedores simulados, cancelamento e atualizacao. O benchmark registra amostras,
CPU, RSS e identidade do artefato; resultados desfavoraveis continuam no relatorio.
Nao se deve deduzir latencia audivel no Discord ou ganho de 20% de um teste de
fila. Consulte [metodologia e limites](docs/tts-native-validation.md).

A validacao no APK/aparelho, a conexao Discord/SSH e o piloto de 48 horas
dependem dos ambientes de producao. Os scripts nao publicam releases nem
alteram automaticamente os backends em producao. As implementacoes legadas
permanecem disponiveis para comparacao e rollback durante essa transicao.
