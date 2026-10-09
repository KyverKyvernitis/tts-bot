#!/data/data/com.termux/files/usr/bin/bash
# Build a separate pinned Box64 with the VOICEPEAK XPutPixel wrapper.
set -euo pipefail
case "${1:-}" in
    "") ;;
    --help|-h)
        echo "Uso: bash patch-box64-x11.sh"
        echo "Corrige XPutPixel em um Box64 separado; não reinstala Ubuntu ou VOICEPEAK."
        exit 0 ;;
    *) echo "Opção desconhecida: ${1}" >&2; exit 2 ;;
esac
if (( $# > 1 )); then echo "Uso: bash patch-box64-x11.sh" >&2; exit 2; fi
if [[ -z "${TERMUX_VERSION:-}" || -z "${PREFIX:-}" || "$(uname -m)" != aarch64 ]]; then
    echo "Execute no Termux nativo ARM64, fora do PRoot." >&2
    exit 2
fi
toolkit_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
toolkit_directory="${HOME}/.voicepeak-termux"
for toolkit_file in launcher.py diagnostic.py box64-xputpixel.py x11-probe-x86_64; do
    [[ -f "${toolkit_source}/${toolkit_file}" ]] || { echo "Toolkit incompleto: ${toolkit_file}" >&2; exit 2; }
done
command -v python >/dev/null || { echo "Python nativo do Termux ausente." >&2; exit 2; }
command -v proot-distro >/dev/null || { echo "PRoot-Distro ausente." >&2; exit 2; }
# Fail before rebuilding if the selected configuration or aliases are foreign.
toolkit_values="$(python - "${toolkit_source}" "${toolkit_directory}" <<'PY'
import json
import os
from pathlib import Path
import sys
source, target = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(source))
from launcher import load_config
if ":" in str(source) or any(ord(c) < 32 for c in str(source)):
    raise SystemExit("Caminho do toolkit incompatível com --bind")
base = target / "config-box64.json"
if base.is_symlink() or not base.is_file():
    raise SystemExit("config-box64.json ausente ou não regular; prepare o Box64 original primeiro")
os.environ["VOICEPEAK_TERMUX_CONFIG"] = str(base)
config = load_config()
if config.get("backend") != "box64" or config.get("guest_box64") != "/opt/voicepeak-box64/bin/box64":
    raise SystemExit("Configuração original preservada: esta correção exige o Box64 original")
expected = json.loads(base.read_text(encoding="utf-8"))
expected["guest_box64"] = "/opt/voicepeak-box64/bin/box64-x11fix-1"
fixed = target / "config-box64-x11fix.json"
if fixed.exists() or fixed.is_symlink():
    if fixed.is_symlink() or not fixed.is_file() or fixed.stat().st_size > 16384:
        raise SystemExit("Configuração X11 existente preservada")
    if json.loads(fixed.read_text(encoding="utf-8")) != expected:
        raise SystemExit("Configuração X11 existente difere da base; preservada sem sobrescrever")
marker = "# Managed by voicepeak-termux patch-box64-x11.sh."
for alias in ("voicepeak-termux-box64-x11fix", "voicepeak-termux-box64-x11fix-diagnostic"):
    path = target / "bin" / alias
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Alias existente preservado: " + alias)
        with path.open(encoding="utf-8") as stream:
            if marker not in stream.read(256):
                raise SystemExit("Alias existente preservado: " + alias)
for name in ("launcher.py", "diagnostic.py"):
    path = target / "bin" / name
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit("Arquivo existente preservado: " + name)
print(config["container"])
print(config.get("box64_library_path", ""))
PY
)"
mapfile -t toolkit_selection <<<"${toolkit_values}"
toolkit_container="${toolkit_selection[0]}"
toolkit_libraries="${toolkit_selection[1]:-}"
toolkit_existing=false
for toolkit_root in "${PREFIX}/var/lib/proot-distro/containers/${toolkit_container}/rootfs" "${PREFIX}/var/lib/proot-distro/installed-rootfs/${toolkit_container}"; do
    [[ ! -d "${toolkit_root}" ]] || toolkit_existing=true
done
if [[ "${toolkit_existing}" == false ]]; then
    echo "Container Box64 existente não encontrado; nenhum ambiente foi instalado." >&2
    exit 2
fi
toolkit_architecture="$(proot-distro login "${toolkit_container}" -- /usr/bin/dpkg --print-architecture)"
if [[ "${toolkit_architecture}" != arm64 ]]; then
    echo "O guest não confirmou arm64; ambiente original preservado." >&2
    exit 2
fi
toolkit_log="$(mktemp)"
trap 'rm -f -- "${toolkit_log}"' EXIT
if ! proot-distro login "${toolkit_container}" --bind "${toolkit_source}:/opt/voicepeak-termux-kit" -- /usr/bin/env LANG=C LC_ALL=C PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -s -- "${toolkit_libraries}" <<'GUEST' 2>&1 | tee "${toolkit_log}"
set -euo pipefail
box64_library_path="$1"
box64_directory=/opt/voicepeak-box64
box64_source="${box64_directory}/src"
box64_target="${box64_directory}/src-x11fix-1"
box64_build="${box64_directory}/build-x11fix-1"
box64_candidate="${box64_directory}/bin/box64-x11fix-1"
box64_toolkit=/opt/voicepeak-termux-kit
if [[ -L "${box64_source}" || ! -d "${box64_source}/.git" || "$(git -C "${box64_source}" rev-parse HEAD)" != dae0917c47b4edd8956f314210417a20fd225c4b ]]; then
    echo "Fonte original não confirmou o pin v0.4.0; preservada." >&2
    exit 1
fi
if [[ -n "$(git -C "${box64_source}" status --porcelain --untracked-files=all)" ]]; then
    echo "Fonte original modificada; preservada sem reset." >&2
    exit 1
fi
if [[ -L "${box64_build}" || -L "${box64_candidate}" ]]; then
    echo "Destino da correção é um link; preservado sem sobrescrever." >&2
    exit 1
fi
/usr/bin/python3 "${box64_toolkit}/box64-xputpixel.py" --source "${box64_source}" --target "${box64_target}"
cmake -S "${box64_target}" -B "${box64_build}" -DARM64=1 -DARM_DYNAREC=ON -DBAD_SIGNAL=ON -DNOGIT=ON -DCMAKE_C_COMPILER=gcc -DCMAKE_BUILD_TYPE=RelWithDebInfo -DPython3_EXECUTABLE=/usr/bin/python3
cmake --build "${box64_build}" --parallel 2
# Validate a separate file even on retries: existing aliases may already use
# box64_candidate, so a failed rebuild must not replace that working binary.
box64_staging="$(mktemp "${box64_directory}/bin/.box64-x11fix-1.XXXXXX")"
trap 'rm -f -- "${box64_staging}"' EXIT
install -m 0755 -- "${box64_build}/box64" "${box64_staging}"
if ! box64_version="$("${box64_staging}" --version 2>&1)"; then
    echo "Candidato Box64 não abriu; configuração original preservada." >&2
    exit 1
fi
if [[ "${box64_version}" == *"terminated with signal"* || "${box64_version}" == *"uncaught target signal"* || ! "${box64_version}" =~ [Bb]ox64.*[[:space:]]v?0\.4\.0([^0-9.]|$) ]]; then
    echo "Candidato não confirmou Box64 v0.4.0 sem falha de sinal." >&2
    exit 1
fi
box64_header="$(od -An -tx1 -N20 -- "${box64_toolkit}/x11-probe-x86_64" | tr -d ' \n')"
if [[ "${box64_header:0:12}" != 7f454c460201 || "${box64_header:36:4}" != 3e00 ]]; then
    echo "O teste X11 não confirmou ELF x86_64." >&2
    exit 1
fi
if ! box64_probe="$(BOX64_LOG=0 BOX64_NOBANNER=1 BOX64_LD_LIBRARY_PATH="${box64_library_path}" "${box64_staging}" "${box64_toolkit}/x11-probe-x86_64" --symbols-only 2>&1)"; then
    echo "O teste dos símbolos X11 falhou; aliases não foram publicados." >&2
    printf '%s\n' "${box64_probe}" >&2
    exit 1
fi
if [[ "${box64_probe}" == *"terminated with signal"* || "${box64_probe}" == *"uncaught target signal"* ]] || ! grep -Fxq VOICEPEAK_X11_SYMBOLS_OK <<<"${box64_probe}"; then
    echo "O teste não confirmou todos os símbolos X11; configuração original preservada." >&2
    printf '%s\n' "${box64_probe}" >&2
    exit 1
fi
if [[ -L "${box64_candidate}" || ( -e "${box64_candidate}" && ! -f "${box64_candidate}" ) ]]; then
    echo "Destino da correção mudou durante a compilação; preservado." >&2
    exit 1
fi
mv -T -- "${box64_staging}" "${box64_candidate}"
printf '%s\n' VOICEPEAK_BOX64_X11FIX_READY
GUEST
then
    echo "A correção X11 não foi confirmada; Box64 e configuração originais continuam disponíveis." >&2
    exit 1
fi
# Some PRoot versions return zero after the guest itself dies. Require a final
# guest marker as well as a clean pipeline before publishing the new selection.
if grep -Fq "terminated with signal" "${toolkit_log}" || grep -Fq "uncaught target signal" "${toolkit_log}" || ! grep -Fxq VOICEPEAK_BOX64_X11FIX_READY "${toolkit_log}"; then
    echo "O guest não confirmou o fim da correção sem falha de sinal; configuração original preservada." >&2
    exit 1
fi
python - "${toolkit_source}" "${toolkit_directory}" <<'PY'
import json
from pathlib import Path
import sys
source, target = Path(sys.argv[1]), Path(sys.argv[2])
base = target / "config-box64.json"
value = json.loads(base.read_text(encoding="utf-8"))
value["guest_box64"] = "/opt/voicepeak-box64/bin/box64-x11fix-1"
configuration = target / "config-box64-x11fix.json"
marker = "# Managed by voicepeak-termux patch-box64-x11.sh."
wrappers = (("voicepeak-termux-box64-x11fix", "launcher.py"), ("voicepeak-termux-box64-x11fix-diagnostic", "diagnostic.py"))
binary = target / "bin"
binary.mkdir(parents=True, exist_ok=True)
# Check again after compilation, preserving edits made while the build ran.
if configuration.exists() or configuration.is_symlink():
    if configuration.is_symlink() or not configuration.is_file() or configuration.stat().st_size > 16384 or json.loads(configuration.read_text(encoding="utf-8")) != value:
        raise SystemExit("Configuração X11 existente preservada")
for alias, _ in wrappers:
    path = binary / alias
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Alias existente preservado: " + alias)
        with path.open(encoding="utf-8") as stream:
            if marker not in stream.read(256):
                raise SystemExit("Alias existente preservado: " + alias)
for name in ("launcher.py", "diagnostic.py"):
    destination = binary / name
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise SystemExit("Arquivo existente preservado: " + name)
    destination.write_text("#!" + sys.executable + "\n" + (source / name).read_text().split("\n", 1)[1], encoding="utf-8")
    destination.chmod(0o700)
for alias, filename in wrappers:
    wrapper = "#!" + sys.executable + "\n" + marker + "\nimport os, runpy\nfrom pathlib import Path\nos.environ['VOICEPEAK_TERMUX_CONFIG'] = str(Path.home() / '.voicepeak-termux/config-box64-x11fix.json')\nrunpy.run_path(str(Path(__file__).resolve().with_name(" + repr(filename) + ")), run_name='__main__')\n"
    destination = binary / alias
    destination.write_text(wrapper, encoding="utf-8")
    destination.chmod(0o700)
if not configuration.exists():
    with configuration.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    configuration.chmod(0o600)
PY
echo "Correção XPutPixel compilada; todos os símbolos X11 exigidos foram confirmados."
echo "Isto ainda não confirma abertura da GUI, ativação ou síntese da Teto."
echo "Launcher separado: ${toolkit_directory}/bin/voicepeak-termux-box64-x11fix"
