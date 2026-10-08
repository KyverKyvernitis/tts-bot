#!/data/data/com.termux/files/usr/bin/bash
# Ubuntu ARM64 stays native; Box64 emulates only the separately licensed program.
set -euo pipefail

case "${1:-}" in
    "") ;;
    --help|-h)
        echo "Uso: bash setup-box64.sh"
        echo "Prepara Ubuntu ARM64 + Box64; preserva o container e a configuração QEMU."
        exit 0
        ;;
    *) echo "Opção desconhecida: ${1}" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    echo "Uso: bash setup-box64.sh" >&2
    exit 2
fi
if [[ -z "${TERMUX_VERSION:-}" || -z "${PREFIX:-}" ]]; then
    echo "Execute no Termux nativo, fora do PRoot." >&2
    exit 2
fi
if [[ "$(uname -m)" != "aarch64" ]]; then
    echo "Este preparo exige Termux ARM64 (aarch64)." >&2
    exit 2
fi

toolkit_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
toolkit_directory="${HOME}/.voicepeak-termux"
toolkit_config="${toolkit_directory}/config-box64.json"
toolkit_container="voicepeak-arm64"
for toolkit_file in launcher.py diagnostic.py; do
    [[ -f "${toolkit_source}/${toolkit_file}" ]] || { echo "Toolkit incompleto: ${toolkit_file}" >&2; exit 2; }
done
pkg install -y python proot-distro
toolkit_install_help="$(proot-distro install --help 2>&1)"
if [[ "${toolkit_install_help}" != *--architecture* ]]; then
    echo "PRoot-Distro antigo: atualize somente proot-distro para v5 ou posterior." >&2
    exit 2
fi
# Keep an existing Box64 selection. Never select, reset or configure the old x64 guest.
if [[ -e "${toolkit_config}" || -L "${toolkit_config}" ]]; then
    toolkit_container="$(python - "${toolkit_config}" "${toolkit_source}" <<'PY'
import os
from pathlib import Path
import sys
path = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
from launcher import ConfigurationError, load_config
try:
    if path.is_symlink() or not path.is_file():
        raise ValueError("configuração não é um arquivo regular")
    os.environ["VOICEPEAK_TERMUX_CONFIG"] = str(path)
    value = load_config()
    if value.get("backend") != "box64":
        raise ValueError("backend deve ser box64")
    container = value.get("container", "")
    # These installation-owned paths must match what is actually verified below.
    if value.get("guest_box64") != "/opt/voicepeak-box64/bin/box64":
        raise ValueError("guest_box64 personalizado: configuração preservada; ajuste manualmente")
    if value.get("box64_library_path") != "/opt/voicepeak-box64/lib/x86_64-linux-gnu":
        raise ValueError("box64_library_path personalizado: configuração preservada; ajuste manualmente")
except (OSError, ConfigurationError, ValueError) as exc:
    raise SystemExit("Configuração Box64 preservada: " + str(exc))
print(container)
PY
)"
fi
toolkit_existing=false
for toolkit_root in "${PREFIX}/var/lib/proot-distro/containers/${toolkit_container}/rootfs" "${PREFIX}/var/lib/proot-distro/installed-rootfs/${toolkit_container}"; do
    [[ ! -d "${toolkit_root}" ]] || toolkit_existing=true
done
if [[ "${toolkit_existing}" == false ]]; then
    proot-distro install ubuntu:24.04 --architecture aarch64 --name "${toolkit_container}"
fi
# The pinned x64 libstdc++ requires GLIBC_2.36. Keep older ARM guests intact;
# they need a separate Ubuntu 24.04 container, not a forced distro upgrade.
toolkit_glibc="$(proot-distro login "${toolkit_container}" -- /usr/bin/getconf GNU_LIBC_VERSION)"
if [[ ! "${toolkit_glibc}" =~ ^glibc[[:space:]]+([0-9]+)\.([0-9]+)$ ]] || (( BASH_REMATCH[1] < 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] < 36) )); then
    echo "O guest precisa de glibc >= 2.36; ambiente antigo preservado. Use um container Ubuntu 24.04 ARM64 separado." >&2
    exit 2
fi
toolkit_architecture="$(proot-distro login "${toolkit_container}" -- /usr/bin/dpkg --print-architecture)"
if [[ "${toolkit_architecture}" != "arm64" ]]; then
    echo "O container existente não é arm64; ele foi preservado. Nenhuma configuração Box64 foi publicada." >&2
    exit 2
fi
if ! proot-distro login "${toolkit_container}" -- /usr/bin/env LANG=C LC_ALL=C /usr/bin/apt-get update; then
    echo "apt update ARM64 falhou; o container existente foi preservado." >&2
    exit 1
fi
if ! proot-distro login "${toolkit_container}" -- /usr/bin/env LANG=C LC_ALL=C DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install -y --no-install-recommends python3 git cmake gcc g++ make pkg-config ca-certificates libcurl4t64 libfreetype6 libstdc++6 libgcc-s1 libasound2t64 libx11-6 libxext6 libxrender1 libxrandr2 libxcursor1 libxinerama1 libxfixes3 fonts-noto-cjk; then
    echo "Bibliotecas ARM64 não confirmadas; nenhum runtime pronto foi anunciado." >&2
    exit 1
fi

# Official v0.4.0 tag, resolved with git ls-remote on 2026-10-08. The immutable
# commit also pins the project's bundled x64 libstdc++ and libgcc libraries.
toolkit_box64_commit="dae0917c47b4edd8956f314210417a20fd225c4b"
if ! proot-distro login "${toolkit_container}" -- /usr/bin/env LANG=C LC_ALL=C PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -s -- "${toolkit_box64_commit}" <<'GUEST'
set -euo pipefail
box64_commit="$1"
box64_directory=/opt/voicepeak-box64
box64_source="${box64_directory}/src"
box64_build="${box64_directory}/build-v0.4.0"
mkdir -p -- "${box64_directory}"
if [[ ! -e "${box64_source}" && ! -L "${box64_source}" ]]; then
    git clone --depth 1 --branch v0.4.0 https://github.com/ptitSeb/box64.git "${box64_source}"
fi
if [[ -L "${box64_source}" || ! -d "${box64_source}/.git" ]]; then
    echo "Fonte Box64 existente preservada: não é o checkout esperado." >&2
    exit 1
fi
if [[ "$(git -C "${box64_source}" rev-parse HEAD)" != "${box64_commit}" ]]; then
    echo "Fonte Box64 existente preservada: commit diferente do pin oficial." >&2
    exit 1
fi
if [[ -n "$(git -C "${box64_source}" status --porcelain --untracked-files=all)" ]]; then
    echo "Fonte Box64 tem alterações locais; preserve-as e resolva antes de compilar." >&2
    exit 1
fi
# Follow the official COMPILE.md Termux CHRoot/PRoot instructions. Keep the
# build at two jobs to avoid competing with the phone worker for memory.
cmake -S "${box64_source}" -B "${box64_build}" -DARM64=1 -DARM_DYNAREC=ON -DBAD_SIGNAL=ON -DCMAKE_C_COMPILER=gcc -DCMAKE_BUILD_TYPE=RelWithDebInfo -DPython3_EXECUTABLE=/usr/bin/python3
cmake --build "${box64_build}" --parallel 2
# Confirm that both emulated libraries really are 64-bit x86 ELF before copying.
for box64_library in libstdc++.so.6 libgcc_s.so.1; do
    box64_input="${box64_source}/x64lib/${box64_library}"
    if [[ ! -f "${box64_input}" || -L "${box64_input}" || ! -r "${box64_input}" ]]; then
        echo "Biblioteca x64 oficial ausente ou inválida: ${box64_library}" >&2
        exit 1
    fi
    box64_header="$(od -An -tx1 -N20 -- "${box64_input}" | tr -d ' \n')"
    if [[ "${box64_header:0:12}" != "7f454c460201" || "${box64_header:36:4}" != "3e00" ]]; then
        echo "Biblioteca do pin não é ELF x86_64: ${box64_library}" >&2
        exit 1
    fi
done
mkdir -p -- "${box64_directory}/bin" "${box64_directory}/lib/x86_64-linux-gnu"
install -m 0755 -- "${box64_build}/box64" "${box64_directory}/bin/box64"
for box64_library in libstdc++.so.6 libgcc_s.so.1; do
    install -m 0644 -- "${box64_source}/x64lib/${box64_library}" "${box64_directory}/lib/x86_64-linux-gnu/${box64_library}"
done
"${box64_directory}/bin/box64" --version
GUEST
then
    echo "Compilação ou verificação Box64 falhou; configuração e runtime QEMU foram preservados." >&2
    exit 1
fi
# Validate the installed binary in a separate login before publishing launchers.
if ! toolkit_box64_version="$(proot-distro login "${toolkit_container}" -- /usr/bin/env LANG=C LC_ALL=C /opt/voicepeak-box64/bin/box64 --version 2>&1)"; then
    echo "Box64 instalado não abriu; configuração Box64 não foi publicada." >&2
    exit 1
fi
if [[ "${toolkit_box64_version}" == *"uncaught target signal"* || "${toolkit_box64_version}" == *"terminated with signal"* ]]; then
    echo "Box64 reportou término por sinal; configuração Box64 não foi publicada." >&2
    exit 1
fi
if [[ ! "${toolkit_box64_version}" =~ [Bb]ox64.*[[:space:]]v?0\.4\.0([^0-9.]|$) ]]; then
    echo "A saída de Box64 não confirmou v0.4.0; configuração Box64 não foi publicada." >&2
    exit 1
fi

python - "${toolkit_source}" "${toolkit_directory}" "${toolkit_container}" <<'PY'
import json
from pathlib import Path
import sys
source, target, container = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
binary = target / "bin"
marker = "# Managed by voicepeak-termux setup-box64.sh."
wrappers = (("voicepeak-termux-box64", "launcher.py"), ("voicepeak-termux-box64-diagnostic", "diagnostic.py"))
# Refuse foreign commands before writing any alias or configuration.
for alias, _ in wrappers:
    path = binary / alias
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Alias existente preservado: " + alias)
        with path.open(encoding="utf-8") as stream:
            if marker not in stream.read(256):
                raise SystemExit("Alias existente preservado: " + alias)
configuration = target / "config-box64.json"
value = {"backend": "box64", "container": container, "guest_box64": "/opt/voicepeak-box64/bin/box64", "box64_library_path": "/opt/voicepeak-box64/lib/x86_64-linux-gnu", "guest_executable": "/opt/Voicepeak/voicepeak", "engine_directory": str(target / "engine/Voicepeak"), "display": ""}
legacy = target / "config.json"
if not configuration.exists() and legacy.is_file():
    # Retain the user's original persistent program/activation directory. Do
    # not copy tokens, change the QEMU selection, or parse arbitrary shell text.
    try:
        if legacy.stat().st_size > 16384:
            raise ValueError("configuração antiga excede 16 KiB")
        previous = json.loads(legacy.read_text(encoding="utf-8"))
        if isinstance(previous, dict):
            engine = previous.get("engine_directory", "")
            if isinstance(engine, str) and engine:
                if not Path(engine).is_absolute() or ":" in engine or any(ord(c) < 32 for c in engine):
                    raise ValueError("engine_directory antigo inválido")
                value["engine_directory"] = engine
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("Configuração antiga preservada: " + str(exc))
binary.mkdir(parents=True, exist_ok=True)
for name in ("launcher.py", "diagnostic.py"):
    destination = binary / name
    if destination.is_symlink():
        raise SystemExit("Arquivo existente preservado: " + name + " é um link")
    destination.write_text("#!" + sys.executable + "\n" + (source / name).read_text().split("\n", 1)[1], encoding="utf-8")
    destination.chmod(0o700)
for alias, filename in wrappers:
    wrapper = "#!" + sys.executable + "\n" + marker + "\nimport os, runpy\nfrom pathlib import Path\nos.environ['VOICEPEAK_TERMUX_CONFIG'] = str(Path.home() / '.voicepeak-termux/config-box64.json')\nrunpy.run_path(str(Path(__file__).resolve().with_name(" + repr(filename) + ")), run_name='__main__')\n"
    destination = binary / alias
    destination.write_text(wrapper, encoding="utf-8")
    destination.chmod(0o700)
if not configuration.exists():
    with configuration.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    configuration.chmod(0o600)
PY
echo "Runtime ARM64 + Box64 preparado; VOICEPEAK e voz Teto ainda não foram verificados."
echo "A configuração QEMU e seu container foram preservados."
echo "Launcher: ${toolkit_directory}/bin/voicepeak-termux-box64"
echo "Diagnóstico: ${toolkit_directory}/bin/voicepeak-termux-box64-diagnostic --probe-system"
