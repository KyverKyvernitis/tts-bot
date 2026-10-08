#!/data/data/com.termux/files/usr/bin/bash
# Prepare only the open-source runtime. Install and activate VOICEPEAK separately.
set -euo pipefail

toolkit_prepare_only=false
case "${1:-}" in
    "") ;;
    --prepare-only) toolkit_prepare_only=true ;;
    --help|-h)
        echo "Uso: bash setup.sh [--prepare-only]"
        echo "--prepare-only: prepara launcher e diagnóstico sem executar apt/dpkg no guest."
        exit 0
        ;;
    *) echo "Opção desconhecida: ${1}" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    echo "Uso: bash setup.sh [--prepare-only]" >&2
    exit 2
fi

if [[ -z "${TERMUX_VERSION:-}" || -z "${PREFIX:-}" ]]; then
    echo "Execute este script no Termux nativo, fora do PRoot." >&2
    exit 2
fi
if [[ "$(uname -m)" != "aarch64" ]]; then
    echo "Este preparo exige Termux ARM64 (aarch64)." >&2
    exit 2
fi

toolkit_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
toolkit_directory="${HOME}/.voicepeak-termux"
for toolkit_file in launcher.py diagnostic.py; do
    [[ -f "${toolkit_source}/${toolkit_file}" ]] || { echo "Toolkit incompleto: ${toolkit_file}" >&2; exit 2; }
done
pkg install -y python proot-distro qemu-user-x86-64
toolkit_install_help="$(proot-distro install --help 2>&1)"
if [[ "${toolkit_install_help}" != *--architecture* ]]; then
    # Do not edit old distro plugins or upgrade every package on the phone.
    echo "PRoot-Distro antigo: atualize somente proot-distro para v5 ou posterior e execute novamente." >&2
    exit 2
fi

toolkit_container="voicepeak-x64"
if [[ -f "${toolkit_directory}/config.json" ]]; then
    toolkit_container="$(VOICEPEAK_TERMUX_CONFIG="${toolkit_directory}/config.json" python - "${toolkit_source}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from launcher import load_config
print(load_config()["container"])
PY
)"
fi
toolkit_existing=false
for toolkit_root in "${PREFIX}/var/lib/proot-distro/containers/${toolkit_container}/rootfs" "${PREFIX}/var/lib/proot-distro/installed-rootfs/${toolkit_container}"; do
    [[ ! -d "${toolkit_root}" ]] || toolkit_existing=true
done
if [[ "${toolkit_existing}" == false ]]; then
    proot-distro install ubuntu:22.04 --architecture x86_64 --name "${toolkit_container}"
fi
toolkit_architecture="$(proot-distro login "${toolkit_container}" -- /usr/bin/dpkg --print-architecture)"
if [[ "${toolkit_architecture}" != "amd64" ]]; then
    echo "O container existente não é amd64; ele foi preservado. Corrija a escolha do ambiente." >&2
    exit 2
fi
# Python writes the native Termux interpreter into the shebang. /usr/bin/env
# does not exist on Android, even though it exists inside the guest.
# Publish diagnostics before apt: a failed libc-bin trigger must not leave the
# native configuration missing or force the user to reinstall the container.
python - "${toolkit_source}" "${toolkit_directory}" "${toolkit_container}" <<'PY'
import json
from pathlib import Path
import sys
source, target, container = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
binary = target / "bin"
binary.mkdir(parents=True, exist_ok=True)
for name, alias in (("launcher.py", "voicepeak-termux"), ("diagnostic.py", "voicepeak-termux-diagnostic")):
    destination = binary / name
    destination.write_text("#!" + sys.executable + "\n" + (source / name).read_text().split("\n", 1)[1], encoding="utf-8")
    destination.chmod(0o700)
    link = binary / alias
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.readlink() != Path(name):
            raise SystemExit("Alias existente preservado: " + alias + "; resolva o conflito antes de continuar")
    else:
        link.symlink_to(name)
configuration = target / "config.json"
if not configuration.exists():
    configuration.write_text(json.dumps({"container": container, "guest_executable": "/opt/Voicepeak/voicepeak", "engine_directory": str(target / "engine/Voicepeak"), "display": ""}, indent=2) + "\n", encoding="utf-8")
    configuration.chmod(0o600)
PY
if [[ "${toolkit_prepare_only}" == true ]]; then
    echo "Launcher e diagnóstico preparados; bibliotecas do guest ainda não verificadas."
    echo "Diagnóstico: ${toolkit_directory}/bin/voicepeak-termux-diagnostic --probe-system"
    exit 0
fi
if ! proot-distro login "${toolkit_container}" -- /usr/bin/apt-get update; then
    echo "apt update falhou; o container existente e o diagnóstico foram preservados." >&2
    echo "Execute: ${toolkit_directory}/bin/voicepeak-termux-diagnostic --probe-system" >&2
    exit 1
fi
if ! proot-distro login "${toolkit_container}" -- /usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install -y --no-install-recommends ca-certificates libcurl4 libfreetype6 libstdc++6 libgcc-s1 libasound2 libx11-6 libxext6 libxrender1 libxrandr2 libxcursor1 libxinerama1 libxfixes3 fonts-noto-cjk; then
    echo "As bibliotecas do guest não foram confirmadas; não considere o runtime pronto." >&2
    echo "O container, o programa e as licenças existentes foram preservados." >&2
    echo "Execute: ${toolkit_directory}/bin/voicepeak-termux-diagnostic --probe-system" >&2
    exit 1
fi
echo "Runtime preparado; nenhum VOICEPEAK ou voicebank foi baixado."
echo "Teste opcional do programa oficial: python ${toolkit_source}/fetch-engine.py"
echo "A voz Teto continua exigindo instalação e ativação oficiais pela GUI."
echo "Diagnóstico: ${toolkit_directory}/bin/voicepeak-termux-diagnostic --probe-runtime"
