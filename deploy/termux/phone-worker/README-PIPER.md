# Piper local: fase 1

O campo de prefixo legado `atts_prefix` (padrão `%`) seleciona `piper`.
A síntese utiliza um novo processo CLI por mensagem e retorna WAV PCM 16-bit.
Não existem streaming de áudio e modelo residente nesta etapa.

### Pré-requisitos no telefone

Manter a instalação de laboratório confirmada:
- `~/piper-termux-teste/voices/pt_BR-faber-medium.onnx`
- `~/piper-termux-teste/voices/pt_BR-faber-medium.onnx.json`
- `~/piper-termux-teste/pkg/` com o binário ARM64 e as bibliotecas
- `espeak-ng-data` no Termux

Opcionalmente, se preferir locais diferentes, configure `~/.phone-worker.env`:
```sh
PHONE_WORKER_PIPER_ENABLED=true
PHONE_WORKER_PIPER_MODEL=$HOME/piper-termux-teste/voices/pt_BR-faber-medium.onnx
PHONE_WORKER_PIPER_CONFIG=$HOME/piper-termux-teste/voices/pt_BR-faber-medium.onnx.json
# PHONE_WORKER_PIPER_BIN=/caminho/absoluto/para/piper
PHONE_WORKER_PIPER_OUTPUT_FORMAT=wav
```

A VPS usa `TTS_PIPER_MODEL_NAME=pt_BR-faber-medium`,
`TTS_PIPER_WORKER_TIMEOUT_SECONDS=20`.

A rota `/task` `tts_synthesize_piper` é processada pelo Python do Phone Worker
mesmo quando `TTS_NATIVE_WORKER=1`. O Rust continua nos outros motores.
O caminho Piper não substitui a voz por gTTS se falhar, para que seja
possível identificar problemas de instalação.

Rollback: restaurar os arquivos anteriores do patch e reiniciar bot e worker.
Modelos locais permanecem intactos.
