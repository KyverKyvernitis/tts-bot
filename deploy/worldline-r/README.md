# Teto no Termux: requisitos do WORLDLINE-R

Esta revisão retira a integração VOICEPEAK do bot e do phone worker. O worker
`1.11.30` volta a usar a voicebank UTAU da Teto e o resampler já configurado.
O valor antigo `PHONE_WORKER_TETO_BACKEND=voicepeak` é interpretado como `utau`;
as variáveis do motor retirado são ignoradas. O `.env` do aparelho não é alterado
pelos ZIPs. Um pedido explícito da Teto não troca para outra voz em caso de falha.

O nome oficial do renderer do OpenUtau é **WORLDLINE-R**. Ele utiliza uma
biblioteca nativa, `libworldline.so`, com uma API de síntese de frases. Esse arquivo
não é um executável UTAU: colocá-lo em `PHONE_WORKER_TETO_RESAMPLER_COMMAND` não
integra o WORLDLINE-R. O executável resampler WORLDLINE também não equivale ao
renderer de frases WORLDLINE-R.

O Poco X7 Pro já mostrou `aarch64`, aproximadamente 11 GiB de RAM e um Ubuntu
ARM64 saudável com glibc 2.39 nos relatórios anteriores. Isso é compatível com
os requisitos binários da biblioteca escolhida. Ainda falta confirmar no aparelho
os pacotes do guest, o carregamento da biblioteca e a voicebank. O adaptador de
frases para o worker **ainda não está implementado nesta revisão**; por isso o
diagnóstico sempre mantém `phrase_adapter_available=false` e `tts_ready=false`.

| Requisito | Como verificar |
| --- | --- |
| Termux ARM64 e Python 3.10 ou posterior | Inventário do host |
| PRoot e Ubuntu ARM64 existente | Arquitetura real do guest, sem emulação x86 |
| Python 3, libc6, libstdc++6 e libgcc-s1 no guest | Estado dos pacotes e carregamento nativo |
| glibc >= 2.34 e GLIBCXX >= 3.4.29 | Dependências da biblioteca; o loader verifica as versões ao carregá-la |
| `libworldline.so` Linux ARM64 do pin abaixo | ELF, SHA-256, símbolos e chamadas da API |
| FFmpeg do Termux | Execução de `ffmpeg -version` |
| Teto UTAU: `oto.ini`, aliases e arquivos WAV presentes | Mesmo índice usado pelo worker, com contagem mínima; sem decodificação de todos os WAVs |
| Adaptador de frases, tempos e curvas | Falta implementar e testar com a Teto |

Box64, QEMU, X11, GUI do OpenUtau e licença VOICEPEAK não são necessários para
verificar essa API nativa. Python do Termux usa Bionic; a biblioteca oficial usa
glibc, então suas chamadas são feitas pelo Python do Ubuntu ARM64.

## Aplicar os ZIPs do updater

Os arquivos ficam nas pastas originais, sem uma pasta externa envolvendo o patch.
Envie os ZIPs nesta ordem, aguardando o sucesso de cada update:

1. `teto-worldline-01-updater.zip`: permite excluir componentes opcionais que já
   estão ausentes, preservando as verificações de caminhos, Git e arquivos.
2. `teto-worldline-02-patch.zip`: retira o backend VOICEPEAK, restaura os controles
   UTAU e exclui os 36 arquivos antigos declarados em `update-manifest.json`.

Essa ordem é necessária porque o updater que prepara o segundo ZIP já precisa
conhecer a correção do primeiro. O processo normal de release atualiza o worker;
o kit de diagnóstico abaixo também funciona independentemente desse processo.

## Conferir o telefone sem instalar nada

Extraia `teto-worldline-termux-kit.zip` em `~/worldline-r-termux-kit`. Exemplo,
depois de permitir o acesso do Termux ao armazenamento:

```bash
mkdir -p "$HOME/worldline-r-termux-kit"
unzip -o "$HOME/storage/downloads/teto-worldline-termux-kit.zip" -d "$HOME/worldline-r-termux-kit"
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/diagnostic.py" \
  --report "$HOME/storage/downloads/teto-worldline-diagnostic.json"
```

O padrão reaproveita o guest existente chamado `voicepeak-arm64`; o nome é
histórico e não inicia o programa retirado. Se o seu guest tiver outro nome,
passe `--container NOME`. O diagnóstico não configura pacotes nem o worker.
Ele lê apenas as chaves da Teto de `~/.phone-worker.env`, sem avaliar comandos
shell e sem incluir tokens de acesso no relatório.

Se as voicebanks estiverem em outras pastas, informe os caminhos:

```bash
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/diagnostic.py" \
  --voicebank "$HOME/voicebanks/kasane-teto" \
  --english-voicebank "$HOME/voicebanks/kasane-teto-english" \
  --report "$HOME/storage/downloads/teto-worldline-diagnostic.json"
```

O kit contém o código do índice de voicebanks; nenhum áudio ou voicebank é
distribuído. Se o `.env` usar expressões shell em vez de caminhos literais, o
diagnóstico rejeita essas expressões. Use os argumentos de caminho nesse caso.

## Preparar a biblioteca nativa, se faltar

O instalador trabalha no guest ARM64 já existente. Ele verifica a arquitetura,
instala apenas os pacotes necessários ausentes e baixa a biblioteca oficial
fixada abaixo. A publicação ocorre depois de verificar SHA-256 e a API nativa;
uma biblioteca anterior diferente recebe backup antes da substituição. O
instalador não habilita um backend WORLDLINE-R no worker.

```bash
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/setup.py" \
  --container voicepeak-arm64
python "$HOME/worldline-r-termux-kit/deploy/worldline-r/termux/diagnostic.py" \
  --report "$HOME/storage/downloads/teto-worldline-diagnostic.json"
```

O diagnóstico também exercita a API com um tom sintético em memória.
`runtime_ready=true` significa que o ambiente e a API nativa foram confirmados.
Isso não significa que a Teto foi sintetizada. O teste sintético do instalador
gera um tom matemático em memória, sem usar a voicebank. A próxima etapa técnica
é converter o planejamento PT-BR/CVVC e o `oto.ini` em requisições de frase,
curvas de F0 e envelopes WORLDLINE-R, e comparar a fala por audição.

## Fonte fixada e validação

Usamos [OpenUtau 0.1.565](https://github.com/openutau/OpenUtau/releases/tag/0.1.565),
commit `a60ca5830b9064556157245d4bf8f5920d93e5f8`, cuja biblioteca ainda exporta
`PhraseSynthNew`, `PhraseSynthDelete`, `PhraseSynthAddRequest`,
`PhraseSynthSetCurves` e `PhraseSynthSynth`. A API do branch principal mudou;
trocar o pin sem adaptar o código pode quebrar a integração.

- [Biblioteca ARM64 oficial](https://raw.githubusercontent.com/openutau/OpenUtau/a60ca5830b9064556157245d4bf8f5920d93e5f8/runtimes/linux-arm64/native/libworldline.so)
- SHA-256 ARM64: `80fb77357de4fae608e2d2fa12db867d0c48d0366263e6a280eccd90a1584dfe`
- [Documentação dos renderers do OpenUtau](https://github.com/openutau/OpenUtau/wiki/Tutorials)
- Licença OpenUtau: [LICENSE.openutau.txt](LICENSE.openutau.txt)

O probe isola as chamadas nativas em outro processo. Erros do loader, término
por sinal, timeout e respostas sem prova JSON são falhas; um código zero do
PRoot sozinho não confirma execução. O teste Linux x64 de desenvolvimento
confirmou a API e a geração de 13.231 amostras finitas de um tom sintético.
A biblioteca ARM64 e a síntese da Teto ainda precisam ser verificadas no Poco.
