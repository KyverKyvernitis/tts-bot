# Teto com WORLDLINE-R no Termux

O worker `1.11.32` inclui o adaptador de frases **WORLDLINE-R**. Ele converte o
planejamento fonético PT-BR e os tempos de `oto.ini` em requisições nativas,
com uma curva de altura compartilhada pela frase. Aceita a bank japonesa e a
English CVVC da Teto já suportadas pelo worker. Não usa um resampler separado.

O relatório do Poco X7 Pro confirmou a biblioteca ARM64, a API, o ABI e a geração
da frase “Olá. Eu sou a Teto.” com a bank japonesa: WAV de 1,46 s, a 44,1 kHz,
mono PCM16, gerado em aproximadamente 2,07 s. Essa geração não comprova a
inteligibilidade ou a naturalidade da adaptação portuguesa.

Box64, QEMU, X11, GUI do OpenUtau e VOICEPEAK não são necessários. Python do
Termux usa Bionic, enquanto a biblioteca usa glibc: as chamadas rodam pelo Python
do Ubuntu ARM64. O nome histórico `voicepeak-arm64` não inicia VOICEPEAK.

## Aplicar o patch no bot

Os ZIPs preservam as pastas originais, sem pasta externa envolvendo o patch.
As etapas são incrementais; aplique somente as que ainda faltarem:

1. `teto-worldline-01-updater.zip`: correção da exclusão de arquivos opcionais.
2. `teto-worldline-02-patch.zip`: retirada de VOICEPEAK e instalador nativo.
3. `teto-worldline-03-adapter.zip`: adaptador, backend, publicação e teste de WAV.
4. `teto-worldline-04-defaults.zip`: tom padrão neutro e restauração da velocidade.
5. `teto-worldline-05-quality.zip`: correção das junções, comparação WAV e instalador English.

Os ZIPs 3, 4 e 5 não contêm exclusões. A release normal distribui os módulos ao
worker e preserva `~/.phone-worker.env`. O padrão permanece `utau`. Um pedido
explícito da Teto falha com diagnóstico quando o motor está indisponível;
não troca para outra voz.

## Primeiro WAV no Poco

O kit v3 permite testar antes da release chegar ao worker. Não distribui áudio,
voicebanks ou bibliotecas binárias. A biblioteca já verificada não precisa ser
reinstalada.

```bash
mkdir -p "$HOME/worldline-r-termux-kit"
unzip -o "$HOME/storage/downloads/teto-worldline-termux-kit-v3.zip" \
  -d "$HOME/worldline-r-termux-kit"

python "$HOME/worldline-r-termux-kit/deploy/termux/phone-worker/scripts/validate-teto-assets.py" \
  --backend worldline-r --container voicepeak-arm64 \
  --voicebank "$HOME/voicebanks/kasane-teto" \
  --render-test --text "Olá. Eu sou a Teto." --timeout 90 \
  --output "$HOME/storage/downloads/teto-worldline-teste.wav" \
  --report "$HOME/storage/downloads/teto-worldline-teste.json"
```

Abra o WAV em Downloads. Sucesso exige confirmação JSON nativa, PCM válido e
áudio não silencioso. `teto_synthesis_verified=true` confirma a geração com os
assets selecionados; `portuguese_speech_verified=false` permanece porque a
pronúncia precisa ser avaliada por audição. Timeout ou erro não é sucesso.

Para a English instalada separadamente, acrescente `--mode english` e use
`--voicebank "$HOME/voicebanks/kasane-teto-english"`. O modo standard usa a bank
informada e não exige English. O validador aceita `--audit-dir CAMINHO` para dez
frases de comparação. Comece com uma frase: a análise de voz custa CPU e o
desempenho de outras frases e banks precisa ser medido no Poco.

## Comparar o timbre e as junções

O adaptador `worldline-r-phrase-2` corrige uma soma excessiva de pesos nas
sobreposições simples entre sílabas. Antes, a entrada podia durar 40 ms e a
saída apenas 10 ms; o peso combinado chegava a 1,75. Agora os fades são
complementares nesses pares, mantendo o peso combinado em 1. Sobreposições
complexas preservam os envelopes anteriores. Tempos, pausas, OTO, curva de
altura e duração planejada permanecem iguais.

Isso pode reduzir oscilações nas junções. O timbre metálico também pode vir
do vocoder WORLD ou dos sons ausentes na bank japonesa; a correção não garante
fala natural. Reverb e mudança de tom não recuperam esses sons.

Com o kit v3 extraído, gere a mesma frase com os envelopes anteriores e os
corrigidos, usando sua bank atual:

```bash
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/compare-quality.py"
```

Os arquivos ficam em `~/storage/downloads/teto-comparacao`:

- `01-anterior.wav`: envelopes do adaptador anterior, para referência.
- `02-juncoes-corrigidas.wav`: junções corrigidas, mesma bank japonesa.
- `03-teto-english.wav`: bank English da própria Teto, quando instalada.
- `summary.json`: resultado de cada geração e controles utilizados.

O comando usa C4, deslocamento de 0 semitons e velocidade 1.0. Não edita
`~/.phone-worker.env` nem troca a bank do bot. Uma falha é registrada por
amostra, sem impedir a tentativa das outras; um WAV antigo não é reaproveitado.

### Instalar a Teto English oficial para comparação

A [English CVVC oficial](https://kasaneteto.jp/utau/) é gratuita e oferece mais
sons e transições que a bank japonesa atual. Ainda exige aproximações para
português. O download tem aproximadamente 80 MiB e a extração 90 MiB.

```bash
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/install-english-voicebank.py"
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/compare-quality.py"
```

O instalador baixa exclusivamente do site oficial, confere tamanho, SHA-256 e
CRC e publica a bank em `~/voicebanks/kasane-teto-english`. Preserva todos os
arquivos e avisos do ZIP; recusa sobrescrever uma pasta não vazia. Não seleciona
English no worker. O comparador detecta essa pasta automaticamente.

- [ZIP oficial TETO-English-150401](https://kasaneteto.jp/assets/download/utau/TETO-English-150401.zip)
- SHA-256 calculado do download verificado: `addb3ab9dbe3dce7cb40fe6ba0c93ab5814d02acf5b091d0a762de370b813d6e`
- [Regras de uso oficiais](https://kasaneteto.jp/guidelines/voice.html)

Para mudar o motor de síntese, o [ESPER-Utau 2.5.0](https://github.com/CdrSonan/ESPER-Utau/releases/tag/v2.5.0)
oferece uma distribuição Linux ARM64 e constitui uma opção para avaliar depois
desta comparação. Ainda não foi integrado ou verificado no Poco: ele precisa
de um backend de resampler separado; não substitui `libworldline.so`.

## Selecionar no worker

### Restaurar o tom e a velocidade do WAV de teste

O WAV usa tom base C4, deslocamento de 0 semitons, velocidade 1.0, tempo 140
e velocidade consonantal 100. O patch `teto-worldline-04-defaults.zip` também
alinha o tom padrão do bot a 0 semitons. Para restaurar esses parâmetros no
Termux, com backup do `.env` e sem alterar os tokens:

```bash
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/reset-speech-defaults.py"
bash "$HOME/.core-worker-runtime/current/start-phone-worker.sh" --force-restart
```

Se um tom personalizado tiver sido salvo no Discord, abra
**Configurar TextToTeto → Tom da Teto (semitons)** e informe `0.0`.
Configurações explícitas salvas continuam tendo prioridade sobre o padrão.
Se a VPS definir `TTS_TETO_DEFAULT_PITCH_SEMITONES` no ambiente, use `0.0`
nessa variável para o mesmo padrão neutro.
Velocidade e tom do Edge não controlam a Teto.

Depois do WAV e da atualização para `1.11.32`, revise estas chaves em
`~/.phone-worker.env`, preservando os tokens e as outras configurações:

```dotenv
PHONE_WORKER_TETO_ENABLED=true
PHONE_WORKER_TETO_BACKEND=worldline-r
PHONE_WORKER_TETO_VOICEBANK_MODE=standard
PHONE_WORKER_TETO_VOICEBANK_DIR=/data/data/com.termux/files/home/voicebanks/kasane-teto
PHONE_WORKER_WORLDLINE_CONTAINER=voicepeak-arm64
PHONE_WORKER_WORLDLINE_LIBRARY=/data/data/com.termux/files/home/.worldline-r/lib/libworldline.so
```

Reinício canônico após a release:

```bash
bash "$HOME/.core-worker-runtime/current/start-phone-worker.sh" --force-restart
```

O status anuncia `backend=worldline-r` e `renderer_version=worldline-r-phrase-2`.
Os limites de texto, áudio, memória, bateria, temperatura, concorrência e prazo
continuam sendo aplicados. O cache distingue backend, código do adaptador,
biblioteca, configuração e identidade da bank.

## Diagnóstico e instalação

```bash
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/diagnostic.py" \
  --report "$HOME/storage/downloads/teto-worldline-diagnostic.json"
```

O diagnóstico distingue o adaptador no kit do worker instalado. `runtime_ready`
confirma o motor; `tts_ready` confirma a configuração de render;
`worker_tts_ready` exige a integração no worker instalado. Esses campos não
comprovam naturalidade. O diagnóstico não gera fala da Teto por padrão e não
altera `.env`, pacotes ou voicebank. Lê somente configurações permitidas, sem
avaliar comandos shell ou incluir credenciais no relatório.

Se a biblioteca faltar em outro aparelho:

```bash
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/setup.py" \
  --container voicepeak-arm64
```

O instalador usa o guest ARM64 existente, instala somente pacotes necessários
ausentes, verifica hash/API antes da publicação e preserva uma biblioteca
anterior diferente. Requer Termux ARM64, Python 3.10+, FFmpeg, PRoot e Ubuntu
ARM64 com Python 3, libc6, libstdc++6 e libgcc-s1. A biblioteca exige glibc >=2.34
e GLIBCXX >=3.4.29; o loader confirma as versões.

## Fonte e verificação

API fixada em [OpenUtau 0.1.565](https://github.com/openutau/OpenUtau/releases/tag/0.1.565),
commit `a60ca5830b9064556157245d4bf8f5920d93e5f8`. A API atual mudou; não substitua
esse pin sem adaptar o código.

- [Biblioteca ARM64 oficial](https://raw.githubusercontent.com/openutau/OpenUtau/a60ca5830b9064556157245d4bf8f5920d93e5f8/runtimes/linux-arm64/native/libworldline.so)
- SHA-256: `80fb77357de4fae608e2d2fa12db867d0c48d0366263e6a280eccd90a1584dfe`
- [Documentação dos renderers](https://github.com/openutau/OpenUtau/wiki/Tutorials)
- [Licença MIT OpenUtau](LICENSE.openutau.txt)

Chamadas nativas rodam em processo isolado, com entradas e tempo limitados.
Falhas por sinal, JSON ausente, WAV inválido e código zero enganoso do PRoot são
rejeitados. Testes Linux x64 verificam síntese real, altura e volume com gravações
matemáticas. Uma amostra adicional foi gerada com a bank English oficial em
Linux x64; isso não comprova a execução dessa bank ou a qualidade PT-BR no Poco.
