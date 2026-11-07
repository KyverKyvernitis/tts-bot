#!/data/data/com.termux/files/usr/bin/bash
# Inicia compilacao cruzada no Ubuntu 22.04 JA INSTALADO via proot-distro.
# Nao instala nem cria distribuicoes Ubuntu; preserva Phone Worker e FFmpeg.
set -Eeuo pipefail
umask 077

if [[ "${PREFIX:-}" != /data/data/com.termux/files/usr || "$(uname -m)" != aarch64 ]]; then
  echo 'ERRO: execute no Termux ARM64.' >&2; exit 2
fi
if ! command -v proot-distro >/dev/null 2>&1; then
  echo 'ERRO: Ubuntu existente nao foi encontrado via proot-distro.' >&2
  echo 'Se usa outro launcher, execute etapa3/compilar_dentro_ubuntu_existente.sh dentro do Ubuntu.' >&2
  exit 3
fi

DEST="$HOME/storage/downloads"
[[ -d "$DEST" ]] || DEST="$HOME"
ZIP="${1:-$DEST/nucleo-fluxo-tts-rust-etapa3.zip}"
[[ -r "$ZIP" ]] || { echo "ERRO: ZIP nao encontrado: $ZIP" >&2; exit 4; }
DISTRIBUICAO="${UBUNTU_TTS_NOME:-ubuntu}"
DATA="$(date +%Y%m%d-%H%M%S)"
LOG="$DEST/compilacao-tts-rust-linux-x86_64-$DATA.log"
exec > >(tee "$LOG") 2>&1
trap 'resultado=$?; if ((resultado)); then echo "FALHA: codigo=$resultado | Relatorio: $LOG"; fi' EXIT

echo '=== ETAPA 3 / UBUNTU 22.04 JA INSTALADO ==='
echo "Inicio: $(date -Is)"
echo "Distribuicao: $DISTRIBUICAO"
echo "Fontes: $ZIP"
echo 'Nenhum Ubuntu sera instalado ou removido.'
FFMPEG_ANTES="$(dpkg-query -W -f='${Version}' ffmpeg 2>/dev/null || true)"
echo "FFmpeg Termux: ${FFMPEG_ANTES:-ausente}"

# Validar Ubuntu existente antes de criar a pasta de trabalho.
if ! proot-distro login "$DISTRIBUICAO" -- /bin/sh -c \
  '. /etc/os-release; test "$ID" = ubuntu && test "$VERSION_ID" = 22.04 && test "$(uname -m)" = aarch64' ; then
  echo "ERRO: '$DISTRIBUICAO' nao e um Ubuntu 22.04 ARM64 acessivel via proot-distro." >&2
  echo 'Se o Ubuntu usa outro nome, defina UBUNTU_TTS_NOME=nome_da_distribuicao.' >&2
  echo 'Se usa outro launcher, rode o script para Ubuntu diretamente dentro dele.' >&2
  exit 3
fi

TRABALHO="$HOME/.local/opt/tts-rust-etapa3-$DATA"
mkdir -p "$TRABALHO"
python - "$ZIP" "$TRABALHO" <<'PY'
from pathlib import Path, PurePosixPath
import shutil, stat, sys, zipfile
origem, destino = sys.argv[1:]
raiz = Path(destino).resolve()
with zipfile.ZipFile(origem) as z:
    itens = [i for i in z.infolist() if not i.is_dir()]
    prefixo = ('tts-bot-main', 'rust', 'nucleo_fluxo_tts')
    if not itens or sum(i.file_size for i in itens) > 20_000_000:
        raise SystemExit('ZIP vazio ou fontes maiores que 20 MiB')
    for i in itens:
        p = PurePosixPath(i.filename)
        if p.is_absolute() or '..' in p.parts or p.parts[:3] != prefixo:
            raise SystemExit(f'Caminho nao permitido no ZIP: {i.filename}')
        if stat.S_ISLNK(i.external_attr >> 16):
            raise SystemExit(f'Link simbolico inesperado: {i.filename}')
        alvo = (raiz / i.filename).resolve()
        if not alvo.is_relative_to(raiz):
            raise SystemExit('Tentativa de extracao fora da pasta')
        alvo.parent.mkdir(parents=True, exist_ok=True)
        with z.open(i) as f, alvo.open('wb') as out:
            shutil.copyfileobj(f, out)
print('Fontes extraidas:', raiz)
PY

# /mnt e um caminho existente no Ubuntu; bind temporario so nesta sessao.
proot-distro login --isolated --bind "$TRABALHO:/mnt" "$DISTRIBUICAO" -- \
  /bin/bash /mnt/tts-bot-main/rust/nucleo_fluxo_tts/vinculo_python/etapa3/compilar_no_ubuntu_arm64.sh

PASTA="$TRABALHO/saida/linux_x86_64"
[[ -s "$PASTA/tts_fluxo_experimental/_nucleo_fluxo_tts.abi3.so" ]] || {
  echo 'ERRO: extensao Linux x86-64 nao foi produzida.' >&2; exit 5
}
ARQUIVO="$DEST/tts-rust-linux-x86_64-$DATA.zip"
python - "$PASTA" "$ARQUIVO" <<'PY'
from pathlib import Path
import sys, zipfile
base, saida = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(saida, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for p in sorted(base.rglob('*')):
        if p.is_file():
            z.write(p, 'linux_x86_64/' + str(p.relative_to(base)))
print('Pacote Linux para VPS:', saida)
PY

FFMPEG_DEPOIS="$(dpkg-query -W -f='${Version}' ffmpeg 2>/dev/null || true)"
[[ "$FFMPEG_ANTES" == "$FFMPEG_DEPOIS" ]] || {
  echo 'ERRO: versao do FFmpeg Termux foi alterada inesperadamente.' >&2; exit 6
}
echo "SHA256: $(sha256sum "$ARQUIVO")"
echo "Relatorio: $LOG"
echo 'Nenhum servico do celular ou da VPS foi reiniciado.'
