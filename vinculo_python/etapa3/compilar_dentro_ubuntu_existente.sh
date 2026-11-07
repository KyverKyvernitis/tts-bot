#!/usr/bin/env bash
# Alternativa para Ubuntu 22.04 iniciado por launcher proprio (sem proot-distro).
# Uso: bash compilar-tts-rust-ubuntu-existente.sh /sdcard/Download/nucleo-fluxo-tts-rust-etapa3.zip [/sdcard/Download]
set -Eeuo pipefail
umask 077

ZIP="${1:-}"
DESTINO="${2:-$HOME}"
[[ -n "$ZIP" && -r "$ZIP" ]] || {
  echo 'Uso: bash compilar-tts-rust-ubuntu-existente.sh CAMINHO_DO_ZIP [PASTA_DE_SAIDA]' >&2; exit 2;
}
[[ -d "$DESTINO" && -w "$DESTINO" ]] || { echo "ERRO: pasta de saida inacessivel: $DESTINO" >&2; exit 2; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 22.04 && "$(uname -m)" == aarch64 ]] || {
  echo 'ERRO: execute dentro do Ubuntu 22.04 ARM64 existente.' >&2; exit 2
}
DATA="$(date +%Y%m%d-%H%M%S)"
TRABALHO="$HOME/.local/opt/tts-rust-etapa3-$DATA"
mkdir -p "$TRABALHO"
LOG="$DESTINO/compilacao-tts-rust-linux-x86_64-$DATA.log"
exec > >(tee "$LOG") 2>&1
trap 'resultado=$?; if ((resultado)); then echo "ERRO: codigo=$resultado | Relatorio: $LOG"; fi' EXIT

python3 - "$ZIP" "$TRABALHO" <<'PY'
from pathlib import Path, PurePosixPath
import shutil, stat, sys, zipfile
arquivo, destino = sys.argv[1:]
raiz = Path(destino).resolve()
with zipfile.ZipFile(arquivo) as z:
    itens = [i for i in z.infolist() if not i.is_dir()]
    prefixo = ('tts-bot-main','rust','nucleo_fluxo_tts')
    if not itens or sum(i.file_size for i in itens) > 20_000_000:
        raise SystemExit('ZIP vazio ou grande demais')
    for i in itens:
        p = PurePosixPath(i.filename)
        if p.is_absolute() or '..' in p.parts or p.parts[:3] != prefixo:
            raise SystemExit(f'Caminho invalido: {i.filename}')
        if stat.S_ISLNK(i.external_attr >> 16):
            raise SystemExit(f'Link simbolico inesperado: {i.filename}')
        alvo = (raiz/p).resolve()
        if not alvo.is_relative_to(raiz):
            raise SystemExit('Extracao fora da pasta')
        alvo.parent.mkdir(parents=True, exist_ok=True)
        with z.open(i) as f, alvo.open('wb') as out:
            shutil.copyfileobj(f,out)
PY

TTS_RUST_DIRETORIO_TRABALHO="$TRABALHO" bash \
  "$TRABALHO/tts-bot-main/rust/nucleo_fluxo_tts/vinculo_python/etapa3/compilar_no_ubuntu_arm64.sh"

PACOTE="$TRABALHO/saida/linux_x86_64"
[[ -s "$PACOTE/tts_fluxo_experimental/_nucleo_fluxo_tts.abi3.so" ]] || exit 5
ARQUIVO="$DESTINO/tts-rust-linux-x86_64-$DATA.zip"
python3 - "$PACOTE" "$ARQUIVO" <<'PY'
from pathlib import Path
import sys, zipfile
base, arquivo = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(arquivo,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in sorted(base.rglob('*')):
        if p.is_file(): z.write(p, 'linux_x86_64/' + str(p.relative_to(base)))
print('Pacote para validar na VPS:', arquivo)
PY
sha256sum "$ARQUIVO"
echo "Relatorio: $LOG"
echo 'Fim: nenhum projeto em execucao foi alterado.'
