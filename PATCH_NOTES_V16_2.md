# Patch V16.2 — Core Linux rootfs glibc telemetry dedupe

- APK `0.6.0` / `115`.
- Corrige colisão de `jobId` nos jobs manuais internos: o ID antigo começava com o `type` longo e era truncado para 64 caracteres no fetch/result, fazendo retries diferentes virarem duplicados no APK.
- Gera IDs manuais curtos com timestamp/nonce no começo, por exemplo `m-<ts>-<nonce>-cl-glibc-v16-0`.
- Aumenta limite de `jobId` público/resultado para 128 caracteres para manter compatibilidade com jobs existentes.
- Atualiza o stage do rootfs glibc para `core-linux-rootfs-glibc-intake-preflight-v16.2`.
- Duplicatas ainda retornam `jobId`, `duplicateOf`, `reason`, `stage` e `state` para telemetria clara.
- Não inclui binários, rootfs, Box64 ou Bedrock.
