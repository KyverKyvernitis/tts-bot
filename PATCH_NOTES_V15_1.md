# V15.1 — Box64 smoke streaming/no-OOM

Corrige a falha do V15 no APK causada por `OutOfMemoryError` durante o smoke do Box64.

## Mudanças

- APK `0.5.94` / `109`.
- Stage novo: `core-linux-box64-version-smoke-v15.1`.
- O smoke do Box64 agora extrai o asset `core-linux/bin/box64` por streaming/chunks.
- SHA256 é calculado incrementalmente durante a cópia.
- ELF/AArch64 é validado pelo cabeçalho inicial, sem carregar o binário inteiro em memória.
- O resultado JSON não inclui estruturas grandes nem conteúdo bruto do binário.
- O smoke continua executando apenas `box64 --version` e `box64 --help` se o runtime glibc existir.

## Segurança mantida

- Sem Bedrock.
- Sem shell livre.
- Sem comando arbitrário.
- Sem binário x86_64 do usuário.
- Sem download em runtime.

## Estado esperado se o rootfs ainda não tiver glibc

`box64_smoke_blocked_missing_glibc_runtime`
