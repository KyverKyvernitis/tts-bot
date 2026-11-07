#!/usr/bin/env bash
# Compilacao cruzada dentro do Ubuntu 22.04 ARM64 JA EXISTENTE no celular.
# Nao altera o bot, o Phone Worker nem o FFmpeg do Termux.
set -Eeuo pipefail
umask 077

TRABALHO="${TTS_RUST_DIRETORIO_TRABALHO:-/mnt}"
RAIZ="$TRABALHO/tts-bot-main/rust/nucleo_fluxo_tts"
VINCULO="$RAIZ/vinculo_python"
PACOTE="$TRABALHO/saida/linux_x86_64"
ALVO=x86_64-unknown-linux-gnu
VERSAO_RUST=1.99.0

[[ "$(uname -m)" == aarch64 ]] || { echo 'ERRO: Ubuntu deve executar em ARM64.' >&2; exit 2; }
[[ -r /etc/os-release ]] || { echo 'ERRO: /etc/os-release ausente.' >&2; exit 2; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 22.04 ]] || {
  echo "ERRO: esperado Ubuntu 22.04 ARM64, obtido ${PRETTY_NAME:-desconhecido}." >&2; exit 2;
}
[[ -r "$VINCULO/Cargo.toml" ]] || { echo "ERRO: fontes ausentes em $VINCULO" >&2; exit 3; }

printf '\n=== UBUNTU EXISTENTE ===\n%s / %s\n' "$PRETTY_NAME" "$(uname -m)"
echo "Area de trabalho: $TRABALHO"
echo 'Nenhum outro PRoot sera criado.'

# Ferramentas de compilacao: somente pacotes ausentes, sem apt upgrade.
# Antes de instalar, rejeita remocoes e modificacoes nos pacotes de audio.
NECESSARIOS=()
command -v x86_64-linux-gnu-gcc >/dev/null 2>&1 || NECESSARIOS+=(gcc-x86-64-linux-gnu)
command -v readelf >/dev/null 2>&1 || NECESSARIOS+=(binutils)
command -v curl >/dev/null 2>&1 || NECESSARIOS+=(curl)
command -v python3 >/dev/null 2>&1 || NECESSARIOS+=(python3)
command -v file >/dev/null 2>&1 || NECESSARIOS+=(file)
if ! command -v cc >/dev/null 2>&1; then NECESSARIOS+=(build-essential); fi
if [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then NECESSARIOS+=(ca-certificates); fi

if ((${#NECESSARIOS[@]})); then
  echo '=== SIMULANDO PACOTES DO UBUNTU ==='
  echo "Necessarios: ${NECESSARIOS[*]}"
  if [[ "$(id -u)" != 0 ]]; then
    echo 'ERRO: instalar compiladores exige root dentro do Ubuntu; use seu launcher com usuario root.' >&2
    exit 4
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  SIMULACAO="$(mktemp)"
  trap 'rm -f "$SIMULACAO"' EXIT
  apt-get -s install --no-install-recommends "${NECESSARIOS[@]}" > "$SIMULACAO"
  grep -E '^(Inst|Remv) ' "$SIMULACAO" || true
  if grep -Eq '^Remv ' "$SIMULACAO"; then
    echo 'ERRO: a simulacao removeria pacotes existentes; abortando.' >&2; exit 4
  fi
  if grep -EiEq '^(Inst|Remv) (ffmpeg|libavcodec[^ ]*|libavformat[^ ]*|libavutil[^ ]*|libswresample[^ ]*|libswscale[^ ]*|mpv|wine[^ ]*|box64)[[:space:]]' "$SIMULACAO"; then
    echo 'ERRO: a simulacao modificaria audio/Wine/Box64; abortando.' >&2; exit 4
  fi
  apt-get install -y --no-install-recommends "${NECESSARIOS[@]}"
fi

# Rustup EXCLUSIVO desta etapa dentro do Ubuntu existente.
# Nao usa /root/.cargo nem modifica a configuracao de Rust ja instalada.
FERRAMENTAS="$HOME/.local/share/tts-rust-cross-etapa3"
export RUSTUP_HOME="$FERRAMENTAS/rustup"
export CARGO_HOME="$FERRAMENTAS/cargo"
export PATH="$CARGO_HOME/bin:$PATH"
mkdir -p "$RUSTUP_HOME" "$CARGO_HOME"
if [[ ! -x "$CARGO_HOME/bin/rustup" ]]; then
  echo '=== INSTALANDO RUSTUP PRIVADO DA ETAPA 3 ==='
  INSTALADOR="$TRABALHO/rustup-init-etapa3.sh"
  curl --fail --location --show-error --silent --retry 2 \
    https://sh.rustup.rs -o "$INSTALADOR"
  RUSTUP_INIT_SKIP_PATH_CHECK=yes sh "$INSTALADOR" \
    -y --profile minimal --default-toolchain "$VERSAO_RUST" --no-modify-path
fi
rustup toolchain install "$VERSAO_RUST" --profile minimal --no-self-update
rustup target add --toolchain "$VERSAO_RUST" "$ALVO"

echo '=== TOOLCHAIN UTILIZADO ==='
rustc "+$VERSAO_RUST" --version
cargo "+$VERSAO_RUST" --version
x86_64-linux-gnu-gcc --version | head -n 1

export CARGO_BUILD_JOBS=2
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_RELEASE_DEBUG=0
export CARGO_TARGET_DIR="$TRABALHO/target-linux-x86_64"
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc
export PYO3_NO_PYTHON=1
export CARGO_TERM_COLOR=never

echo '=== COMPILANDO EXTENSAO X86-64 LINUX ==='
if command -v nice >/dev/null 2>&1; then
  nice -n 10 cargo "+$VERSAO_RUST" build \
    --manifest-path "$VINCULO/Cargo.toml" --release --target "$ALVO" --jobs 2
else
  cargo "+$VERSAO_RUST" build \
    --manifest-path "$VINCULO/Cargo.toml" --release --target "$ALVO" --jobs 2
fi

SO="$CARGO_TARGET_DIR/$ALVO/release/lib_nucleo_fluxo_tts.so"
[[ -s "$SO" ]] || { echo "ERRO: binario ausente: $SO" >&2; exit 5; }
readelf -h "$SO" | grep -Eq 'Machine:.*(X86-64|x86-64|Advanced Micro Devices)' || {
  echo 'ERRO: binario nao e x86-64.' >&2; exit 5;
}
readelf -Ws "$SO" | grep 'PyInit__nucleo_fluxo_tts' >/dev/null || {
  echo 'ERRO: simbolo de inicializacao Python ausente.' >&2; exit 5;
}

python3 - "$SO" <<'PY'
import pathlib, re, sys
raw = pathlib.Path(sys.argv[1]).read_bytes()
versoes = sorted({(int(a), int(b)) for a,b in re.findall(rb'GLIBC_(\d+)\.(\d+)', raw)})
print('Maior GLIBC exigida:', '.'.join(map(str, versoes[-1])) if versoes else '(nao identificada)')
if versoes and versoes[-1] > (2,35):
    raise SystemExit('ERRO: exige GLIBC mais recente que a da VPS Ubuntu 22.04')
PY

mkdir -p "$PACOTE/tts_fluxo_experimental" "$PACOTE/testes_python" "$PACOTE/benchmarks"
cp "$SO" "$PACOTE/tts_fluxo_experimental/_nucleo_fluxo_tts.abi3.so"
cp "$VINCULO"/python/tts_fluxo_experimental/{__init__.py,adaptador.py,_nucleo_fluxo_tts.pyi,py.typed} "$PACOTE/tts_fluxo_experimental/"
cp "$VINCULO"/testes_python/teste_*.py "$PACOTE/testes_python/"
cp "$VINCULO"/benchmarks/comparar_fluxos.py "$PACOTE/benchmarks/"
cp "$VINCULO"/etapa3/validar_na_vps.sh "$PACOTE/validar_na_vps.sh"
cp "$VINCULO"/etapa3/benchmark_repetido.py "$PACOTE/benchmarks/"
cp "$VINCULO"/etapa3/teste_plataforma.py "$PACOTE/testes_python/"
chmod 700 "$PACOTE/validar_na_vps.sh"

python3 - "$PACOTE" "$VERSAO_RUST" <<'PY'
from pathlib import Path
import hashlib, json, sys
p, rustc = Path(sys.argv[1]), sys.argv[2]
so = p/'tts_fluxo_experimental/_nucleo_fluxo_tts.abi3.so'
metadata = {
    'etapa': 3,
    'alvo': 'x86_64-unknown-linux-gnu',
    'abi_python': 'abi3-py310',
    'glibc_maxima_planejada': '2.35',
    'sha256_modulo': hashlib.sha256(so.read_bytes()).hexdigest(),
    'rust_toolchain': rustc,
    'modo': 'validacao_isolada_sem_integracao_com_bot',
}
(p/'metadados.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False)+'\n')
files = sorted(x for x in p.rglob('*') if x.is_file())
(p/'SHA256SUMS').write_text(''.join(
    f'{hashlib.sha256(x.read_bytes()).hexdigest()}  {x.relative_to(p)}\n'
    for x in files
))
print('Pacote pronto:', p)
print('SHA-256:', metadata['sha256_modulo'])
PY
