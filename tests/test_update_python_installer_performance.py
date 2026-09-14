from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "updater" / "core" / "atualizar.sh"


def _runtime_functions(tmp_path: Path) -> Path:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("python_runtime_install_requirements_file() {")
    end = source.index("\nrun_candidate_python_runtime_smoke() {", start)
    path = tmp_path / "runtime-functions.sh"
    path.write_text(source[start:end], encoding="utf-8")
    return path


def _harness(functions: Path, runtime_root: Path, repo: Path, *, mode: str = "pip") -> str:
    return f'''
set -eu -o pipefail
source "{functions}"
CANDIDATE_ROOT="{runtime_root.parent}"
PYTHON_RUNTIME_ROOT="{runtime_root}"
PYTHON_RUNTIME_CURRENT_LINK="$PYTHON_RUNTIME_ROOT/current"
PYTHON_TOOL_ROOT="{runtime_root.parent}/python-tools"
PYTHON_UV_CACHE_ROOT="{runtime_root.parent}/uv-cache"
PYTHON_UV_VERSION=0.12.13
PYTHON_INSTALLER_MODE={mode}
PYTHON_AUTO_BOOTSTRAP_UV=0
PYTHON_INSTALLER_STATUS='não usado'
PYTHON_UV_BIN_RESOLVED=''
REPO_DIR="{repo}"
PREVIOUS_COMMIT=base0001
LOCAL_CANDIDATE_PYTHON_ARTIFACT=''
LOCAL_CANDIDATE_PYTHON_READY=0
PYTHON_RUNTIME_MUTATED=0
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
CURRENT_STAGE_COMMAND=''
LOG_TAG=test
sanitize_commit_ref() {{ printf '%s\n' "$1"; }}
short_commit() {{ printf '%.7s' "$1"; }}
logger() {{ :; }}
chown() {{ :; }}
sudo() {{
  if [[ "${{1:-}}" == -u ]]; then shift 2; fi
  [[ "${{1:-}}" == -H ]] && shift
  "$@"
}}
install() {{
  local -a args=()
  while (($#)); do
    case "$1" in
      -o|-g) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  command install "${{args[@]}}"
}}
zip_progress_run_as_ubuntu() {{
  CURRENT_STAGE_COMMAND="$3"
  eval "$3"
}}
register_error_context() {{ :; }}
'''


def test_requirements_lock_is_preferred_and_recorded_in_manifest(tmp_path: Path) -> None:
    functions = _runtime_functions(tmp_path)
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    runtime_root = tmp_path / "python-runtimes"
    repo.mkdir()
    candidate.mkdir()
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    # requirements.txt propositalmente impossível: a preparação só pode passar
    # se requirements.lock for realmente a fonte autoritativa.
    (candidate / "requirements.txt").write_text("package-that-must-never-be-resolved-xyz==0\n", encoding="utf-8")
    (candidate / "requirements.lock").write_text("# empty deterministic lock for test\n", encoding="utf-8")

    harness = _harness(functions, runtime_root, repo) + f'''
prepare_candidate_python_runtime "{candidate}" lock0001
printf 'INSTALLER=%s\n' "$PYTHON_INSTALLER_STATUS"
'''
    result = subprocess.run(["bash", "-c", harness], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    manifest = json.loads((runtime_root / "lock0001" / "python.json").read_text(encoding="utf-8"))
    assert manifest["requirements_file"] == "requirements.lock"
    assert manifest["installer"] == "pip"
    assert "INSTALLER=pip" in result.stdout


def test_auto_mode_falls_back_to_pip_when_uv_is_not_available(tmp_path: Path) -> None:
    functions = _runtime_functions(tmp_path)
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    runtime_root = tmp_path / "python-runtimes"
    repo.mkdir()
    candidate.mkdir()
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    (candidate / "requirements.txt").write_text("", encoding="utf-8")

    harness = _harness(functions, runtime_root, repo, mode="auto") + f'''
PYTHON_AUTO_BOOTSTRAP_UV=0
prepare_candidate_python_runtime "{candidate}" auto0001
printf 'INSTALLER=%s\n' "$PYTHON_INSTALLER_STATUS"
'''
    result = subprocess.run(["bash", "-c", harness], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "INSTALLER=pip" in result.stdout


def test_uv_mode_uses_pinned_uv_and_persistent_cache(tmp_path: Path) -> None:
    functions = _runtime_functions(tmp_path)
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    runtime_root = tmp_path / "python-runtimes"
    fake_uv = tmp_path / "uv"
    repo.mkdir()
    candidate.mkdir()
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    (candidate / "requirements.txt").write_text("", encoding="utf-8")
    fake_uv.write_text(
        r'''#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "--version" ]]; then echo 'uv 0.12.13'; exit 0; fi
if [[ "${1:-}" == "venv" ]]; then
  shift
  base=''
  dest=''
  while (($#)); do
    case "$1" in
      --python) base="$2"; shift 2 ;;
      --seed) shift ;;
      *) dest="$1"; shift ;;
    esac
  done
  "$base" -m venv "$dest"
  exit 0
fi
if [[ "${1:-}" == "pip" && "${2:-}" == "install" ]]; then
  shift 2
  py=''
  req=''
  while (($#)); do
    case "$1" in
      --python) py="$2"; shift 2 ;;
      -r|--requirements) req="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  "$py" -m pip install --disable-pip-version-check --no-input --progress-bar off -r "$req"
  exit 0
fi
exit 91
''',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    harness = _harness(functions, runtime_root, repo, mode="uv") + f'''
TTS_BOT_UV_BIN="{fake_uv}"
prepare_candidate_python_runtime "{candidate}" uv000001
printf 'INSTALLER=%s\n' "$PYTHON_INSTALLER_STATUS"
test -d "$PYTHON_UV_CACHE_ROOT"
'''
    result = subprocess.run(["bash", "-c", harness], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "INSTALLER=uv 0.12.13" in result.stdout
    manifest = json.loads((runtime_root / "uv000001" / "python.json").read_text(encoding="utf-8"))
    assert manifest["installer"] == "uv 0.12.13"


def test_updater_pins_uv_and_keeps_safe_pip_fallback() -> None:
    text = UPDATER.read_text(encoding="utf-8")
    assert 'PYTHON_UV_VERSION="${TTS_BOT_UV_VERSION:-0.12.13}"' in text
    assert 'PYTHON_INSTALLER_MODE="${TTS_BOT_PYTHON_INSTALLER:-auto}"' in text
    assert '"uv==${PYTHON_UV_VERSION:-0.12.13}"' in text
    assert "UV_LINK_MODE=clone" in text
    assert "UV_CACHE_DIR=" in text
    assert "--no-python-downloads" not in text  # supplied by env: UV_NO_PYTHON_DOWNLOADS=1
    assert "prepare_python_runtime_with_pip" in text
    assert "--prefer-binary" in text
    assert "--progress-bar off" in text


def test_requirements_lock_counts_as_python_dependency_change() -> None:
    text = UPDATER.read_text(encoding="utf-8")
    assert "requirements.txt|requirements.lock" in text
    assert '[[ "$file" == "requirements.txt" || "$file" == "requirements.lock" ]] && REQUIREMENTS_CHANGED=1' in text


def test_success_card_reports_python_installer_path() -> None:
    text = UPDATER.read_text(encoding="utf-8")
    assert "Python ${PYTHON_INSTALLER_STATUS:-não usado}" in text
