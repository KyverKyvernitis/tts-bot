from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"
START = ROOT / "start.sh"
SMOKE = ROOT / "utility" / "update_runtime_smoke.py"


def _python_runtime_functions(tmp_path: Path) -> Path:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("python_runtime_release_root_for_commit() {")
    end = source.index("\nrun_candidate_python_runtime_smoke() {", start)
    path = tmp_path / "python-runtime-functions.sh"
    path.write_text(source[start:end], encoding="utf-8")
    return path


def _common_harness(functions: Path, runtime_root: Path, repo: Path) -> str:
    return f'''
set -eu -o pipefail
source "{functions}"
PYTHON_RUNTIME_ROOT="{runtime_root}"
PYTHON_RUNTIME_CURRENT_LINK="$PYTHON_RUNTIME_ROOT/current"
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


def test_requirements_candidate_builds_real_isolated_venv_and_manifest(tmp_path: Path) -> None:
    functions = _python_runtime_functions(tmp_path)
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    runtime_root = tmp_path / "python-runtimes"
    repo.mkdir()
    candidate.mkdir()
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    (candidate / "requirements.txt").write_text("", encoding="utf-8")

    harness = _common_harness(functions, runtime_root, repo) + f'''
prepare_candidate_python_runtime "{candidate}" feedface
printf 'READY=%s\n' "$LOCAL_CANDIDATE_PYTHON_READY"
printf 'ROOT=%s\n' "$LOCAL_CANDIDATE_PYTHON_ARTIFACT"
verify_python_runtime_release "$LOCAL_CANDIDATE_PYTHON_ARTIFACT" feedface "{candidate}/requirements.txt"
"$LOCAL_CANDIDATE_PYTHON_ARTIFACT/venv/bin/python" -c 'import sys; print("PREFIX=" + sys.prefix)'
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    release = runtime_root / "feedface"
    assert "READY=1" in result.stdout
    assert f"ROOT={release}" in result.stdout
    assert (release / "python.json").is_file()
    assert (release / "venv" / "bin" / "python").exists()
    assert f"PREFIX={release / 'venv'}" in result.stdout


def test_python_runtime_activation_and_rollback_are_atomic_symlink_switches(tmp_path: Path) -> None:
    functions = _python_runtime_functions(tmp_path)
    repo = tmp_path / "repo"
    runtime_root = tmp_path / "python-runtimes"
    repo.mkdir()
    requirements = repo / "requirements.txt"
    requirements.write_text("", encoding="utf-8")

    harness = _common_harness(functions, runtime_root, repo) + f'''
for commit in old00001 new00002; do
  mkdir -p "$PYTHON_RUNTIME_ROOT/$commit/venv/bin"
  cat > "$PYTHON_RUNTIME_ROOT/$commit/venv/bin/python" <<'FAKEPY'
#!/usr/bin/env bash
if [[ "$1 $2 $3" == "-m pip freeze" ]]; then echo 'fakepkg==1.0'; exit 0; fi
if [[ "$1 $2" == "-m pip" && "${3:-}" == "check" ]]; then exit 0; fi
if [[ "$1" == "-c" ]]; then echo '3.10.0'; exit 0; fi
exit 0
FAKEPY
  chmod +x "$PYTHON_RUNTIME_ROOT/$commit/venv/bin/python"
  write_python_runtime_manifest "$PYTHON_RUNTIME_ROOT/$commit" "$commit" "{requirements}"
done
activate_python_runtime_release new00002
printf 'NEW=%s\n' "$(readlink -f "$PYTHON_RUNTIME_CURRENT_LINK")"
PREVIOUS_COMMIT=old00001
restore_python_runtime_release old00001
printf 'OLD=%s\n' "$(readlink -f "$PYTHON_RUNTIME_CURRENT_LINK")"
printf 'MUTATED=%s\n' "$PYTHON_RUNTIME_MUTATED"
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert f"NEW={runtime_root / 'new00002'}" in result.stdout
    assert f"OLD={runtime_root / 'old00001'}" in result.stdout
    assert "MUTATED=0" in result.stdout


def test_first_requirements_change_snapshots_existing_live_venv_without_pip_install(tmp_path: Path) -> None:
    functions = _python_runtime_functions(tmp_path)
    repo = tmp_path / "repo"
    runtime_root = tmp_path / "python-runtimes"
    repo.mkdir()
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    subprocess.run(["python3", "-m", "venv", str(repo / ".venv")], check=True, timeout=30)

    harness = _common_harness(functions, runtime_root, repo) + '''
REQUIREMENTS_CHANGED=1
current_bot_python_bin() { printf '%s\n' "$REPO_DIR/.venv/bin/python"; }
capture_python_runtime_release_snapshot base0001
verify_python_runtime_release "$PYTHON_RUNTIME_ROOT/base0001" base0001 "$REPO_DIR/requirements.txt"
printf 'SNAPSHOT=%s\n' "$PYTHON_RUNTIME_ROOT/base0001"
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert f"SNAPSHOT={runtime_root / 'base0001'}" in result.stdout
    assert (runtime_root / "base0001" / "venv" / "bin" / "python").exists()



def test_invalid_active_python_runtime_is_never_deleted_during_reprepare(tmp_path: Path) -> None:
    functions = _python_runtime_functions(tmp_path)
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    runtime_root = tmp_path / "python-runtimes"
    repo.mkdir()
    candidate.mkdir()
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    (candidate / "requirements.txt").write_text("", encoding="utf-8")
    active = runtime_root / "feedface"
    active.mkdir(parents=True)
    marker = active / "do-not-delete.txt"
    marker.write_text("keep", encoding="utf-8")
    (runtime_root / "current").symlink_to(active)

    harness = _common_harness(functions, runtime_root, repo) + f"""
if prepare_candidate_python_runtime \"{candidate}\" feedface; then
  echo ACTIVE_INVALID_ACCEPTED
  exit 44
fi
printf 'CODE=%s\\n' \"$LAST_ERROR_CODE\"
test -f \"{marker}\"
printf 'MARKER=kept\\n'
"""
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert "ACTIVE_INVALID_ACCEPTED" not in result.stdout
    assert "CODE=PYTHON_RUNTIME_ACTIVE_INVALID" in result.stdout
    assert "MARKER=kept" in result.stdout

def test_start_script_executes_versioned_python_directly_without_activate() -> None:
    text = START.read_text(encoding="utf-8")
    assert "python-runtimes" in text
    assert "/current" in text
    assert 'exec "$PYTHON_BIN" -u bot.py' in text
    assert 'export VIRTUAL_ENV="$PYTHON_ENV"' in text
    assert 'export PATH="$PYTHON_ENV/bin:$PATH"' in text
    assert "source /home/ubuntu/bot/.venv/bin/activate" not in text


def test_rollback_restores_python_release_before_bot_restart_and_never_reinstalls() -> None:
    text = UPDATER.read_text(encoding="utf-8")
    start = text.index("rollback_after_failure() {")
    end = text.index("\nhandle_post_deploy_failure() {", start)
    block = text[start:end]
    restore_at = block.index('restore_python_runtime_release "$PREVIOUS_COMMIT"')
    deploy_at = block.index("if deploy_bot;")
    assert restore_at < deploy_at

    deploy_start = text.index("deploy_bot() {")
    deploy_end = text.index("\nfrontend_publication_is_healthy() {", deploy_start)
    deploy = text[deploy_start:deploy_end]
    assert "activate_python_runtime_release" in deploy
    transactional = deploy[deploy.index("if (( REQUIREMENTS_CHANGED == 1") : deploy.index("if (( BOT_CHANGED == 1")]
    assert "$REPO_DIR/.venv/bin/pip" not in transactional
    assert "ROLLBACK_IN_PROGRESS == 0" in transactional
