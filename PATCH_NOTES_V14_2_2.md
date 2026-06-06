# Patch V14.2.2 — Base Git leve sem binários pesados

Objetivo: o anexo "Base Git leve" do `/vps` deve continuar útil para patch/análise, mas não deve carregar binários grandes toda vez.

## Mudanças

- A base Git leve agora pula extensões binárias/pacotes comuns, incluindo `.so`, `.apk`, `.deb`, `.zip`, `.tar`, `.xz`, `.zst`, `.jar`, `.dex`, `.onnx`, `.bin` etc.
- Pula diretórios pesados conhecidos:
  - `android/core-worker-app/app/src/main/jniLibs/`
  - `android/core-worker-app/app/src/main/assets/core-linux/bin/`
  - `android/core-worker-app/app/src/main/assets/core-linux/rootfs/`
  - `android/core-worker-app/releases/`
  - `build/`, `dist/`
- Adiciona limite por arquivo na base leve: `1_250_000` bytes.
- Atualiza o texto do modal para: "sem assets, binários e manifestos".
- Atualiza a mensagem de anexo para "Repositório leve anexado".

## Segurança

- Não altera updater.
- Não altera build APK.
- Não altera Box64/Bedrock.
- Não toca CallKeeper.

