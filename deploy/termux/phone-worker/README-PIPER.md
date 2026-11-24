# Piper: duas vozes residentes no Termux

Os modelos são baixados pelo `start-phone-worker.sh` durante a manutenção,
quando `PHONE_WORKER_PIPER_MODEL_AUTO_DOWNLOAD` não estiver desativado.
Os arquivos ficam em `~/piper-termux-teste/voices`.

- `pt_BR-miro-high.onnx` e `.onnx.json`: Voz masculina (Miro High)
- `pt_BR-dii-medium.onnx` e `.onnx.json`: Voz feminina (Dii Medium OpenVoiceOS)

`PHONE_WORKER_PIPER_RESIDENT_ENABLED=true` mantém ambos carregados, com
no máximo uma síntese simultânea. Na falta da biblioteca residente, o caminho
CLI continua disponível. O modelo Faber antigo não é mais utilizado.
O download não apaga arquivos antigos, para permitir rollback manual.

Licenças dos modelos: Miro High CC BY-NC-SA 4.0 e Dii CC BY-NC-ND 4.0.
Consulte as condições de uso antes de distribuir o serviço comercialmente.
