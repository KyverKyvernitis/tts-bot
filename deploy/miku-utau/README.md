# Miku English CVVC no Termux

Este kit instala o port **MIKU_V4_English / Mocaloid Generator** divulgado por
[DEMO](https://demosvocas.blogspot.com/2025/12/mikuv4xbeta-utau.html).
O [ZIP de terceiros](https://drive.google.com/file/d/1VoNNANLMEW-TEcqLPWa1hxgATWZPMfwP/view)
é `mukki english cvvc.zip`, com 71.819.802 bytes e SHA-256
`e1fc9a37a7e45e0ad4209b2b0f37d90541b2528c85d1b58a81c0b399ecaf79be`.
Não é um banco UTAU oficial da Crypton; a autorização e a licença de distribuição
não foram confirmadas. O kit não inclui os áudios nem baixa ou executa programas
do arquivo de voz.

Reutiliza Python, FFmpeg e o ESPER já instalado no Ubuntu ARM64
`voicepeak-arm64`. Não precisa de VOICEPEAK, Box64 ou reinstalação do Ubuntu.
A Miku fica em `~/voicebanks/hatsune-miku-english`, separada das duas banks da Teto.
Os aliases X-SAMPA originais, o sufixo `_D4`, o `prefix.map` e os avisos do ZIP
são preservados.

## Instalar e gerar os dois MP3s

Depois de extrair o kit em `~/miku-utau-termux-kit`, rode:

```bash
python "$HOME/miku-utau-termux-kit/deploy/miku-utau/termux/install-voicebank.py" \
  --archive "$HOME/storage/downloads/mukki english cvvc.zip" \
  --container voicepeak-arm64

python "$HOME/miku-utau-termux-kit/deploy/miku-utau/termux/validate-miku.py" \
  --container voicepeak-arm64 --timeout 120
```

Se o Android salvou o ZIP com outro nome, altere somente o valor de `--archive`.
O instalador confere o SHA-256 antes da publicação. Um arquivo diferente gera
erro e não substitui uma bank existente. A segunda execução confirma o inventário
da bank instalada e reaplica somente as configurações da Miku, quando necessário.

As amostras ficam em **Downloads/miku-comparacao**:

- `01-miku-portugues.mp3`: “Olá. Eu sou a Miku. Como você está?”
- `02-miku-english.mp3`: “Hello. I am Miku. How are you?”
- Os WAVs correspondentes e `miku-comparacao.json` com o resultado dos testes.

O teste usa tom normal (offset de 0 semitons) e velocidade 1,0. Confere PCM
audível, identidade da voz, idioma, tom, velocidade e decodificação do MP3.
Ouça os arquivos para avaliar timbre e pronúncia; gerar um arquivo válido não
comprova naturalidade ou inteligibilidade. Português adaptado aproxima os sons
brasileiros usando o banco inglês.

## Habilitar no bot

O instalador registra somente `PHONE_WORKER_MIKU_*` em `~/.phone-worker.env`,
com backup antes de uma alteração, e preserva os ajustes e comentários da Teto.
O comando ESPER é reutilizado; se não estiver configurado, a ponte instalada
`~/.esper-utau/bin/resampler.py` é usada. Nenhum `.env` ou código do ZIP é executado
durante a instalação. As banks existentes em uma pasta sem marcador válido são
preservadas.

A atualização do bot/worker precisa incluir o suporte à Miku. Após concluir a
instalação e as amostras, reinicie o worker pelo fluxo normal que você já usa.
No Discord, abra **TTV → Configurar → Escolha sua vocaloid → Hatsune Miku** e
escolha **Português adaptado** ou **Inglês**. A Teto continua selecionada até essa
troca; instalar o banco não muda as preferências dos usuários.

Os parâmetros `--env`, `--directory` do instalador e `--voicebank`,
`--output-dir` do validador permitem usar outros caminhos. `--timeout` admite
1 a 120 segundos por amostra. O relatório não imprime tokens do worker.
