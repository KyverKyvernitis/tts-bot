from __future__ import annotations

import subprocess
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core


ROOT = Path(__file__).resolve().parents[2]
UPDATER = caminho_fonte_core()


def _function_block(source: str, start_name: str, end_name: str) -> str:
    start = source.index(f"{start_name}() {{")
    end = source.index(f"\n{end_name}() {{", start)
    return source[start:end]


def test_remote_commit_reaches_ready_before_runtime_snapshot_and_fast_forward() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("REMOTE_CANDIDATE_MODE=1")
    block = source[start:]

    preflight_at = block.index('validate_remote_commit_in_staging "$REMOTE_COMMIT"')
    ready_at = block.index("prepare_local_candidate_runtime_artifacts_in_worktree", preflight_at)
    snapshot_at = block.index('capture_runtime_release_snapshot "$PREVIOUS_COMMIT"', ready_at)
    merge_at = block.index('repo_git merge --ff-only "$REMOTE_COMMIT"', snapshot_at)

    assert preflight_at < ready_at < snapshot_at < merge_at
    assert "validação/build isolado falhou antes da promoção" in block[ready_at:snapshot_at]


def test_remote_frontend_and_backend_publish_use_ready_artifacts_without_live_build() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    frontend = _function_block(source, "deploy_frontend", "install_backend_prebuilt_artifact")
    backend = _function_block(source, "deploy_backend", "rollback_after_failure")

    assert "LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1" in frontend
    assert "LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1" in backend
    assert frontend.index("FRONTEND_PREBUILT_ARTIFACT_MISSING") < frontend.index("npm ci")
    assert backend.index("BACKEND_PREBUILT_ARTIFACT_MISSING") < backend.index("npm ci")


def test_remote_runtime_artifacts_are_built_from_remote_worktree(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("local_candidate_artifact_root_for_commit() {")
    end = source.index("\npromote_local_candidate_worktree_commit() {", start)
    functions = tmp_path / "ready-functions.sh"
    functions.write_text(source[start:end], encoding="utf-8")

    worktree = tmp_path / "remote-worktree"
    artifact_base = tmp_path / "remote-artifacts"
    for side in ("frontend", "backend"):
        root = worktree / "dashboard" / side
        root.mkdir(parents=True)
        (root / "package.json").write_text('{"name":"fake"}', encoding="utf-8")
        (root / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    back_fixture = worktree / "dashboard" / "backend"
    (back_fixture / "src").mkdir()
    (back_fixture / "scripts").mkdir()
    (back_fixture / "src" / "index.ts").write_text("export const ready = true;\n", encoding="utf-8")
    (back_fixture / "scripts" / "copy-command-catalog.mjs").write_text("// fixture\n", encoding="utf-8")
    (back_fixture / "tsconfig.json").write_text(
        '{"compilerOptions":{"outDir":"dist","rootDir":"src"},"include":["src"]}',
        encoding="utf-8",
    )

    harness = f'''
set -eu -o pipefail
source "{functions}"
LOCAL_CANDIDATE_MODE=0
REMOTE_CANDIDATE_MODE=1
REMOTE_WORKTREE_DIR="{worktree}"
REMOTE_COMMIT=feedface
REMOTE_RUNTIME_ARTIFACT_ROOT="{artifact_base}"
NODE_DEPENDENCY_CACHE_ROOT="{tmp_path / 'node-cache'}"
TYPESCRIPT_CACHE_ROOT="{tmp_path / 'typescript-cache'}"
LOCAL_CANDIDATE_RUNTIME_READY=0
LOCAL_CANDIDATE_ARTIFACT_ROOT=''
LOCAL_CANDIDATE_ARTIFACT_COMMIT=''
LOCAL_CANDIDATE_FRONTEND_ARTIFACT=''
LOCAL_CANDIDATE_BACKEND_ARTIFACT=''
REMOTE_CANDIDATE_ARTIFACT_ROOT=''
FRONT_CHANGED=1
BACK_CHANGED=1
FRONT_TESTS_REQUIRED=1
FRONT_TYPECHECK_REQUIRED=0
FRONT_STATUS=''
BACK_STATUS=''
STAGE=''
CURRENT_STAGE_COMMAND=''
CURRENT_STAGE_LOG_FILE=''
CURRENT_STAGE_LOG_STAGE=''
LOG_TAG=test

short_commit() {{ printf '%s' "${{1:0:7}}"; }}
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
chown() {{ :; }}
logger() {{ :; }}
node() {{ :; }}
sudo() {{
  if [[ "${{1:-}}" == -u ]]; then shift 2; fi
  [[ "${{1:-}}" == -H ]] && shift
  "$@"
}}
worktree_git() {{ return 0; }}
npm() {{
  case "$1 ${{2:-}}" in
    'ci '*| 'install '*)
      mkdir -p node_modules/.bin node_modules/pkg
      printf module > node_modules/pkg/index.js
      ln -sfn ../pkg/index.js node_modules/.bin/pkg
      if [[ "$PWD" == */frontend ]]; then
        cat > node_modules/.bin/vite <<'SHVITE'
#!/bin/sh
mkdir -p dist
printf '<html>remote-ready</html>' > dist/index.html
SHVITE
        chmod +x node_modules/.bin/vite
      else
        cat > node_modules/.bin/tsc <<'SHTSC'
#!/bin/sh
set -eu
info=''
prev=''
for arg in "$@"; do
  if [ "$prev" = '--tsBuildInfoFile' ]; then info="$arg"; fi
  prev="$arg"
done
mkdir -p dist "$(dirname "$info")"
printf 'console.log("remote-ready")' > dist/index.js
printf '{{"program":"ok"}}' > "$info"
SHTSC
        chmod +x node_modules/.bin/tsc
      fi
      ;;
    'test ')
      :
      ;;
    'run build')
      mkdir -p dist
      if [[ "$PWD" == */frontend ]]; then
        printf '<html>remote-ready</html>' > dist/index.html
      else
        printf 'console.log("remote-ready")' > dist/index.js
      fi
      ;;
    'prune --omit=dev'*)
      :
      ;;
    *)
      echo "unexpected npm args: $*" >&2
      return 70
      ;;
  esac
}}
zip_progress_run_as_ubuntu() {{
  CURRENT_STAGE_COMMAND="$3"
  eval "$3"
}}
register_error_context() {{ return 99; }}
write_local_candidate_state() {{ echo STATE_SHOULD_NOT_BE_WRITTEN; return 91; }}
prepare_local_candidate_runtime_artifacts_in_worktree
printf 'READY=%s\n' "$LOCAL_CANDIDATE_RUNTIME_READY"
printf 'ROOT=%s\n' "$REMOTE_CANDIDATE_ARTIFACT_ROOT"
printf 'FRONT=%s\n' "$LOCAL_CANDIDATE_FRONTEND_ARTIFACT"
printf 'BACK=%s\n' "$LOCAL_CANDIDATE_BACKEND_ARTIFACT"
verify_local_candidate_artifact_integrity frontend
verify_local_candidate_artifact_integrity backend
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    artifact_root = artifact_base / "feedface"
    assert "READY=1" in result.stdout
    assert f"ROOT={artifact_root}" in result.stdout
    assert "STATE_SHOULD_NOT_BE_WRITTEN" not in result.stdout
    assert (artifact_root / "frontend" / "dist" / "index.html").is_file()
    assert (artifact_root / "backend" / "dist" / "index.js").is_file()
    assert (artifact_root / "ready.json").is_file()


def test_runtime_validation_rejects_tracked_source_mutation(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("local_candidate_artifact_root_for_commit() {")
    end = source.index("\npromote_local_candidate_worktree_commit() {", start)
    functions = tmp_path / "ready-functions.sh"
    functions.write_text(source[start:end], encoding="utf-8")

    worktree = tmp_path / "remote-worktree"
    worktree.mkdir()
    artifact_base = tmp_path / "remote-artifacts"

    harness = f'''
set -eu -o pipefail
source "{functions}"
LOCAL_CANDIDATE_MODE=0
REMOTE_CANDIDATE_MODE=1
REMOTE_WORKTREE_DIR="{worktree}"
REMOTE_COMMIT=deadbeef
REMOTE_RUNTIME_ARTIFACT_ROOT="{artifact_base}"
LOCAL_CANDIDATE_RUNTIME_READY=0
REMOTE_CANDIDATE_ARTIFACT_ROOT=''
FRONT_CHANGED=0
BACK_CHANGED=0
FRONT_STATUS=''
BACK_STATUS=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
LOG_TAG=test
short_commit() {{ printf '%s' "${{1:0:7}}"; }}
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
chown() {{ :; }}
logger() {{ :; }}
worktree_git() {{ printf ' M dashboard/frontend/package-lock.json\n'; }}
if prepare_local_candidate_runtime_artifacts_in_worktree; then
  echo MUTATION_ACCEPTED
  exit 42
fi
printf 'CODE=%s\n' "$LAST_ERROR_CODE"
printf 'ERROR=%s\n' "$LAST_ERROR_STDERR"
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "MUTATION_ACCEPTED" not in result.stdout
    assert "CODE=VALIDATOR_MUTATED_SOURCE_TREE" in result.stdout
    assert "package-lock.json" in result.stdout


def test_remote_ready_artifacts_keep_ready_cache_but_prune_partial_and_old_entries() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    cleanup = _function_block(source, "cleanup_runtime_artifacts", "trim_alert_text")
    prune = _function_block(source, "prune_update_artifacts", "git_add_changed_files_or_reject")

    assert '[[ ! -s "$REMOTE_CANDIDATE_ARTIFACT_ROOT/ready.json" ]]' in cleanup
    assert 'rm -rf -- "$REMOTE_CANDIDATE_ARTIFACT_ROOT"' in cleanup
    assert 'prune_archive_root "$REMOTE_RUNTIME_ARTIFACT_ROOT" 1 2' in prune
