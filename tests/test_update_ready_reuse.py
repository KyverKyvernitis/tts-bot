from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"


def test_ready_artifact_is_reused_without_repeating_validation_commands(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("local_candidate_artifact_root_for_commit() {")
    end = source.index("\npromote_local_candidate_worktree_commit() {", start)
    functions = tmp_path / "ready-functions.sh"
    functions.write_text(source[start:end], encoding="utf-8")

    candidate = tmp_path / "candidate"
    worktree = tmp_path / "worktree"
    for side in ("frontend", "backend"):
        root = worktree / "dashboard" / side
        root.mkdir(parents=True)
        (root / "package.json").write_text('{"name":"fake"}', encoding="utf-8")
        (root / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")

    harness = f'''
set -eu -o pipefail
source "{functions}"
LOCAL_CANDIDATE_MODE=1
REMOTE_CANDIDATE_MODE=0
LOCAL_CANDIDATE_DIR="{candidate}"
LOCAL_CANDIDATE_WORKTREE_DIR="{worktree}"
LOCAL_CANDIDATE_PREPARED_COMMIT=feedface
LOCAL_CANDIDATE_ID=zip-test
NODE_DEPENDENCY_CACHE_ROOT="{tmp_path / 'node-cache'}"
LOCAL_CANDIDATE_RUNTIME_READY=0
LOCAL_CANDIDATE_ARTIFACT_ROOT=''
LOCAL_CANDIDATE_ARTIFACT_COMMIT=''
LOCAL_CANDIDATE_FRONTEND_ARTIFACT=''
LOCAL_CANDIDATE_BACKEND_ARTIFACT=''
FRONT_CHANGED=1
BACK_CHANGED=1
FRONT_TESTS_CHANGED=0
FRONT_TESTS_REQUIRED=1
FRONT_TYPECHECK_REQUIRED=0
BACK_TESTS_CHANGED=0
BOT_CHANGED=0
REQUIREMENTS_CHANGED=0
FRONT_STATUS=''
BACK_STATUS=''
PREFLIGHT_RUNTIME_STATUS=''
STAGE=''
CURRENT_STAGE_COMMAND=''
LOG_TAG=test
RUNS=0
install() {{
  local -a a=()
  while (($#)); do
    case "$1" in
      -o|-g) shift 2 ;;
      *) a+=("$1"); shift ;;
    esac
  done
  command install "${{a[@]}}"
}}
chown() {{ :; }}
logger() {{ :; }}
node() {{ :; }}
sudo() {{
  if [[ "${{1:-}}" == -u ]]; then shift 2; fi
  [[ "${{1:-}}" == -H ]] && shift
  "$@"
}}
frontend_publication_is_healthy() {{ return 0; }}
npm() {{
  case "$1 ${{2:-}}" in
    'ci '*|'install '*)
      mkdir -p node_modules/.bin node_modules/pkg
      printf module > node_modules/pkg/index.js
      if [[ "$PWD" == */frontend ]]; then
        cat > node_modules/.bin/vite <<'SHVITE'
#!/bin/sh
mkdir -p dist
printf '<html>ok</html>' > dist/index.html
SHVITE
        chmod +x node_modules/.bin/vite
      fi
      ;;
    'test ')
      :
      ;;
    'run build')
      mkdir -p dist
      if [[ "$PWD" == */frontend ]]; then
        printf '<html>ok</html>' > dist/index.html
      else
        printf 'ok' > dist/index.js
      fi
      ;;
    *)
      echo "unexpected npm: $*" >&2
      return 70
      ;;
  esac
}}
zip_progress_run_as_ubuntu() {{
  RUNS=$((RUNS + 1))
  CURRENT_STAGE_COMMAND="$3"
  eval "$3"
}}
register_error_context() {{ return 99; }}
write_local_candidate_state() {{ :; }}
worktree_git() {{ return 0; }}
prepare_local_candidate_runtime_artifacts_in_worktree
first="$RUNS"
prepare_local_candidate_runtime_artifacts_in_worktree
second="$RUNS"
printf 'FIRST=%s SECOND=%s FRONT=%s BACK=%s\n' "$first" "$second" "$FRONT_STATUS" "$BACK_STATUS"
[[ "$first" -gt 0 ]]
[[ "$second" == "$first" ]]
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "READY reutilizado sem rebuild" in result.stdout
    first = next(int(item.split("=", 1)[1]) for item in result.stdout.split() if item.startswith("FIRST="))
    second = next(int(item.split("=", 1)[1]) for item in result.stdout.split() if item.startswith("SECOND="))
    assert second == first
