# Teto com ESPER-Utau no Termux

O kit testa o ESPER-Utau **v2.5.0** com a Teto no Ubuntu ARM64 que já existe no
Poco. Usa o backend UTAU do worker e a composição de áudio existente. ESPER é
um resampler diferente do WORLDLINE-R; não substitui `libworldline.so`.

A release oficial Linux ARM64 foi baixada e seu tamanho, SHA-256 e ELF foram
conferidos. Uma frase real da Teto English foi gerada em Linux x64 com o mesmo
resampler. A execução ARM64 no Poco ainda depende do teste abaixo. O projeto
prioriza suavidade, baixo ruído do motor e clareza; isso não garante português
natural com uma voicebank de canto.

## Instalar e comparar no Poco

Baixe `teto-esper-termux-kit-v1.zip` para Downloads e execute:

```bash
mkdir -p "$HOME/esper-utau-termux-kit"
unzip -o "$HOME/storage/downloads/teto-esper-termux-kit-v1.zip" \
  -d "$HOME/esper-utau-termux-kit"

python "$HOME/esper-utau-termux-kit/deploy/esper-utau/termux/setup.py" \
  --container voicepeak-arm64

python "$HOME/esper-utau-termux-kit/deploy/esper-utau/termux/compare-esper.py" \
  --mp3
```

O setup baixa aproximadamente **102 MiB**, verifica os arquivos oficiais,
confere as dependências do guest, testa síntese artificial e só então publica
o runtime em `~/.esper-utau/releases/v2.5.0`. Não requer uma instalação separada
do .NET, Box64 ou interface gráfica. O nome histórico `voicepeak-arm64` apenas
identifica seu Ubuntu ARM64 existente.

Sucesso do setup exige `runtime_ready=true` e
`checks.native_probe.synthetic_render_verified=true`. Esse teste usa uma fonte
artificial; a geração da Teto é verificada pelo comparador seguinte.

O comparador usa a Teto English já instalada em
`~/voicebanks/kasane-teto-english`, com a mesma frase, C4, tom de 0 semitons e
velocidade 1.0 nos dois motores. Preserva `~/.phone-worker.env` e a seleção do
motor no bot. Gera em **Downloads/teto-comparacao**:

- `04-worldline-english.wav` e `.mp3`: referência WORLDLINE-R atual.
- `05-esper-english.wav` e `.mp3`: ESPER-Utau.
- `esper-summary.json`: confirmação de cada geração e eventuais erros.

ESPER e WORLDLINE usam montagens diferentes; a duração dos dois WAVs pode
variar. Os relatórios confirmam síntese, não inteligibilidade ou naturalidade.
Compare timbre metálico, clareza das palavras, consoantes e pausas por audição.

O primeiro uso analisa as gravações; usos posteriores podem aproveitar os
caches. A amostra local “Olá. Eu sou a Teto.” levou cerca de 8,09 s inicialmente
e 0,046 s com cache de fragmentos, em Linux x64. Esses tempos não representam
o desempenho no Poco. O prazo do comparador é 180 s por amostra, mais até 5 s
para limpeza; MP3 permite até 15 s adicionais por arquivo.

## Banco original e caches

ESPER normalmente escreve `.esp` e `.frq` junto ao WAV de entrada. O wrapper
passa ao guest uma cópia privada, inteira, de cada gravação, em
`~/.esper-utau/cache/sources`. Copia o FRQ original quando válido; os arquivos
gerados permanecem nessa pasta privada. Isso também evita que o FRQ produzido
pelo ESPER afete outros motores.

O cache de fragmentos é separado do WORLDLINE e dos resamplers anteriores.
Pedidos da mesma fonte são serializados para evitar corridas na análise.
Timeout e encerramento do processo pai interrompem o grupo do guest. Código
zero do PRoot acompanhado de falha por sinal é rejeitado. A publicação de áudio
exige WAV PCM válido e não silencioso.

O INI oficial fica junto ao executável. A v2.5.0 escolhe CUDA ou CPU conforme
disponibilidade; os testes locais funcionaram com CPU, sem CUDA. A biblioteca
interna não oferece uma chave documentada para desligar GPU no INI. `Velocity`
é recebido, mas não aplicado pelo código dessa versão; o teste mantém 100 e
controla a velocidade da frase pelo planejador do worker.

## Outros comandos

Para testar somente ESPER, sem repetir a referência WORLDLINE:

```bash
python "$HOME/esper-utau-termux-kit/deploy/esper-utau/termux/compare-esper.py" \
  --esper-only --mp3 --text "Olá. Eu sou a Teto."
```

Para comparar com a bank japonesa existente:

```bash
python "$HOME/esper-utau-termux-kit/deploy/esper-utau/termux/compare-esper.py" \
  --mode standard --voicebank "$HOME/voicebanks/kasane-teto" --mp3
```

Para repetir apenas o diagnóstico sintético do runtime instalado:

```bash
python "$HOME/.esper-utau/bin/resampler.py" --probe
```

Não execute `ESPER-Utau --help`: o executável oficial exige os 13 argumentos
UTAU. Os scripts deste kit oferecem `--help` normalmente. Se o setup encontrar
arquivos diferentes no destino, preserva-os e informa o erro. Não apague o
Ubuntu nem a biblioteca WORLDLINE para instalar ESPER.

## Seleção futura no worker

Este kit executa a comparação sem selecionar ESPER em produção. O worker já
aceita resamplers UTAU por `PHONE_WORKER_TETO_RESAMPLER_COMMAND`; após escolher
o resultado por audição, a integração usa `PHONE_WORKER_TETO_BACKEND=utau`, o
wrapper instalado e `PHONE_WORKER_TETO_LENGTH_MODE=total`. Configurações
anteriores de Straycat ou flags WORLDLINE não devem ser herdadas.

## Fontes e licença

- [Release oficial v2.5.0](https://github.com/CdrSonan/ESPER-Utau/releases/tag/v2.5.0)
- Commit: `2ff1a4692fa88fb1b60933408125632d0c5d8356`.
- [Executável Linux ARM64](https://github.com/CdrSonan/ESPER-Utau/releases/download/v2.5.0/ESPER-Utau-Linux-arm64): 107335629 bytes.
- SHA-256 ARM64: `e2cb6dc113593cb3b788debb51996f591a5f9d47bbd2a54df57bc1ecd843602f`.
- [INI oficial](https://github.com/CdrSonan/ESPER-Utau/releases/download/v2.5.0/esper-config.ini): 762 bytes.
- SHA-256 INI: `21951154b9bafdebde0b3a1d6533bb1fde549113b63b99a573b49ea68fadbe83`.
- [Licença MIT](LICENSE.txt).

Os ZIPs do patch e do kit mantêm as pastas originais do repositório. Não contêm
executáveis, voicebanks ou credenciais; o setup baixa os assets oficiais.
