# Termux Kit v8

Leia [INSTRUCOES-IMPORTACAO.md](INSTRUCOES-IMPORTACAO.md) para instalação,
sidecar, extração polifônica, configuração UTAU e validação/publicação.

```sh
python ~/ttv-cd-kit/deploy/ttv-cd/import-midi.py --watch
```

O kit usa o Phone Worker instalado e seu `~/.phone-worker.env` sem mudar o
runtime. Cada ZIP é um incremento por música, com artefatos consistentes e
hash da partitura separado do hash MIDI. A amostra pendente não descarta
artefatos prontos. `--retry` permite repetir imediatamente após uma correção.

O relatório informa caminho absoluto, SHA-256, validação e pendências.
O pacote só está completo com partitura, metadados, JPEG e MP3 validado.
Publicação opcional: `--publish-url https://HOST/api/osaka/cd/publish`, com
`OSAKA_CD_PUBLISH_TOKEN` no ambiente. Recebimento na fila não confirma
instalação na VPS; verifique o updater e o painel `_cd`.
