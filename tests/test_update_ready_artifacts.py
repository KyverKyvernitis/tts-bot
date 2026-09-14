from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "updater" / "core" / "atualizar.sh"


def _function_block(source: str, start_name: str, end_name: str) -> str:
    start = source.index(f"{start_name}() {{")
    end = source.index(f"\n{end_name}() {{", start)
    return source[start:end]


def test_local_candidate_reaches_ready_before_live_promotion() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    prepare = source[source.index("prepare_local_candidate_update() {") : source.index("\npublish_local_candidate_after_validation() {")]

    commit_at = prepare.index("prepare_local_candidate_commit_in_worktree")
    artifacts_at = prepare.index("prepare_local_candidate_runtime_artifacts_in_worktree", commit_at)
    ready_copy_at = prepare.index('"Candidato READY em isolamento"', artifacts_at)
    promote_at = prepare.index("promote_local_candidate_worktree_commit", artifacts_at)

    assert commit_at < artifacts_at < ready_copy_at < promote_at
    assert "Nenhuma promoção foi realizada" in prepare[artifacts_at:promote_at]


def test_isolated_runtime_build_uses_worktree_and_keeps_npm_cache() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(
        source,
        "prepare_local_candidate_runtime_artifacts_in_worktree",
        "promote_local_candidate_worktree_commit",
    )

    assert 'front_dir="$LOCAL_CANDIDATE_WORKTREE_DIR/dashboard/frontend"' in block
    assert 'back_dir="$LOCAL_CANDIDATE_WORKTREE_DIR/dashboard/backend"' in block
    assert 'cd \\\"$front_dir\\\"' in block
    assert 'cd \\\"$back_dir\\\"' in block
    assert "npm ci" in block
    assert "npm test" in block
    assert 'run_backend_incremental_build "$back_dir"' in block
    assert "npm prune --omit=dev" not in block
    assert "BACKEND_LOCKFILE_REQUIRED" in block
    assert "npm cache clean --force" not in block
    assert 'write_local_candidate_state "ready"' in block


def test_ready_artifact_manifest_is_bound_to_commit_and_content(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("local_candidate_artifact_root_for_commit() {")
    end = source.index("\nprepare_local_candidate_runtime_artifacts_in_worktree() {", start)
    functions = tmp_path / "artifact-functions.sh"
    functions.write_text(source[start:end], encoding="utf-8")

    candidate = tmp_path / "candidate"
    front = candidate / "runtime-artifacts" / "deadbeef" / "frontend" / "dist"
    back = candidate / "runtime-artifacts" / "deadbeef" / "backend"
    (front / "assets").mkdir(parents=True)
    (back / "dist").mkdir(parents=True)
    dep_key = "a" * 64
    cache = tmp_path / "node-cache"
    layer = cache / "backend" / "prod" / dep_key
    (layer / "node_modules" / "pkg").mkdir(parents=True)
    (front / "index.html").write_text("front-v1", encoding="utf-8")
    (front / "assets" / "app.js").write_text("asset", encoding="utf-8")
    (back / "dist" / "index.js").write_text("server-v1", encoding="utf-8")
    (back / "deps.json").write_text(f'{{"mode":"prod","deps_key":"{dep_key}"}}', encoding="utf-8")
    (layer / "node_modules" / "pkg" / "index.js").write_text("module", encoding="utf-8")
    (layer / "layer.json").write_text(f'{{"state":"ready","key":"{dep_key}","mode":"prod"}}', encoding="utf-8")

    harness = f'''
set -eu -o pipefail
source "{functions}"
LOCAL_CANDIDATE_DIR="{candidate}"
LOCAL_CANDIDATE_PREPARED_COMMIT=deadbeef
NODE_DEPENDENCY_CACHE_ROOT="{cache}"
root="$(local_candidate_artifact_root_for_commit deadbeef)"
write_local_candidate_artifact_ready_manifest "$root" deadbeef 1 1
hydrate_local_candidate_runtime_artifacts deadbeef
verify_local_candidate_artifact_integrity frontend
verify_local_candidate_artifact_integrity backend
printf 'READY=%s\n' "$LOCAL_CANDIDATE_RUNTIME_READY"
printf 'FRONT=%s\n' "$LOCAL_CANDIDATE_FRONTEND_ARTIFACT"
printf 'BACK=%s\n' "$LOCAL_CANDIDATE_BACKEND_ARTIFACT"
printf tamper >> "$LOCAL_CANDIDATE_FRONTEND_ARTIFACT/index.html"
if verify_local_candidate_artifact_integrity frontend; then
  echo 'TAMPER_ACCEPTED'
  exit 44
fi
printf 'TAMPER=blocked\n'
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "READY=1" in result.stdout
    assert f"FRONT={front}" in result.stdout
    assert f"BACK={back}" in result.stdout
    assert "TAMPER=blocked" in result.stdout
    assert "TAMPER_ACCEPTED" not in result.stdout


def test_local_frontend_publish_consumes_ready_artifact_without_rebuild(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(source, "deploy_frontend", "install_backend_prebuilt_artifact")
    functions = tmp_path / "frontend-deploy.sh"
    functions.write_text(block, encoding="utf-8")
    artifact = tmp_path / "frontend-dist"
    artifact.mkdir()
    (artifact / "index.html").write_text("ok", encoding="utf-8")

    harness = f'''
set -eu -o pipefail
source "{functions}"
LOCAL_CANDIDATE_MODE=1
ROLLBACK_IN_PROGRESS=0
LOCAL_CANDIDATE_RUNTIME_READY=1
LOCAL_CANDIDATE_PREPARED_COMMIT=deadbeef
LOCAL_CANDIDATE_FRONTEND_ARTIFACT="{artifact}"
FRONT_CHANGED=1
FRONT_DIR=/definitely/not/used
FRONT_STATUS=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
hydrate_local_candidate_runtime_artifacts() {{ return 99; }}
verify_local_candidate_artifact_integrity() {{ test "$1" = frontend; }}
publish_frontend_atomically() {{ printf 'PUBLISH=%s\n' "$1"; }}
zip_progress_publish() {{ :; }}
zip_progress_done() {{ :; }}
zip_progress_done_and_publish() {{ :; }}
zip_progress_run_as_ubuntu() {{ echo REBUILD_CALLED; return 88; }}
repo_git() {{ echo deadbeef; }}
deploy_frontend
printf 'STATUS=%s\n' "$FRONT_STATUS"
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"PUBLISH={artifact}" in result.stdout
    assert "artefato READY" in result.stdout
    assert "REBUILD_CALLED" not in result.stdout


def test_local_backend_publish_consumes_ready_artifact_without_rebuild(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(source, "deploy_backend", "rollback_after_failure")
    functions = tmp_path / "backend-deploy.sh"
    functions.write_text(block, encoding="utf-8")
    artifact = tmp_path / "backend"
    (artifact / "dist").mkdir(parents=True)
    (artifact / "dist" / "index.js").write_text("ok", encoding="utf-8")
    (artifact / "deps.json").write_text('{"mode":"prod","deps_key":"' + ('a' * 64) + '"}', encoding="utf-8")

    harness = f'''
set -eu -o pipefail
source "{functions}"
LOCAL_CANDIDATE_MODE=1
ROLLBACK_IN_PROGRESS=0
LOCAL_CANDIDATE_RUNTIME_READY=1
LOCAL_CANDIDATE_PREPARED_COMMIT=deadbeef
LOCAL_CANDIDATE_BACKEND_ARTIFACT="{artifact}"
BACK_CHANGED=1
FRONT_CHANGED=0
BACK_DIR=/definitely/not/used
BACK_SERVICE=fake.service
BACK_HEALTH_URL=http://127.0.0.1:9/health
BACK_PORT=8787
BACK_STATUS=''
ACTIVITY_HEALTHCHECK_STATUS=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
hydrate_local_candidate_runtime_artifacts() {{ return 99; }}
verify_local_candidate_artifact_integrity() {{ test "$1" = backend; }}
install_backend_prebuilt_artifact() {{ printf 'INSTALL=%s\n' "$1"; }}
zip_progress_publish() {{ :; }}
zip_progress_done() {{ :; }}
zip_progress_done_and_publish() {{ :; }}
zip_progress_run_as_ubuntu() {{ echo REBUILD_CALLED; return 88; }}
repo_git() {{ echo deadbeef; }}
systemctl() {{ return 0; }}
wait_for_service_active() {{ return 0; }}
sleep() {{ :; }}
wait_for_health() {{ return 0; }}
trim_alert_text() {{ cat; }}
deploy_backend
printf 'STATUS=%s\n' "$BACK_STATUS"
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"INSTALL={artifact}" in result.stdout
    assert "artefato READY" in result.stdout
    assert "REBUILD_CALLED" not in result.stdout


def test_local_publish_fails_closed_when_ready_artifact_is_missing() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    frontend = _function_block(source, "deploy_frontend", "install_backend_prebuilt_artifact")
    backend = _function_block(source, "deploy_backend", "rollback_after_failure")

    assert "FRONTEND_PREBUILT_ARTIFACT_MISSING" in frontend
    assert "recusando rebuild no checkout live" in frontend
    assert "BACKEND_PREBUILT_ARTIFACT_MISSING" in backend
    assert "recusando rebuild no checkout live" in backend
    assert "ROLLBACK_IN_PROGRESS == 0" in frontend
    assert "ROLLBACK_IN_PROGRESS == 0" in backend


def test_prepare_runtime_artifacts_builds_both_sides_before_promotion(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("local_candidate_artifact_root_for_commit() {")
    end = source.index("\npromote_local_candidate_worktree_commit() {", start)
    functions = tmp_path / "ready-functions.sh"
    functions.write_text(source[start:end], encoding="utf-8")

    candidate = tmp_path / "candidate"
    worktree = tmp_path / "worktree"
    (worktree / "dashboard" / "frontend").mkdir(parents=True)
    (worktree / "dashboard" / "backend").mkdir(parents=True)
    for side in ("frontend", "backend"):
        root = worktree / "dashboard" / side
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
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_DIR="{candidate}"
LOCAL_CANDIDATE_WORKTREE_DIR="{worktree}"
LOCAL_CANDIDATE_PREPARED_COMMIT=feedface
LOCAL_CANDIDATE_ID=zip-test
NODE_DEPENDENCY_CACHE_ROOT="{tmp_path / 'node-cache'}"
TYPESCRIPT_CACHE_ROOT="{tmp_path / 'typescript-cache'}"
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
printf '<html>ready</html>' > dist/index.html
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
printf 'console.log("ready")' > dist/index.js
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
        printf '<html>ready</html>' > dist/index.html
      else
        printf 'console.log("ready")' > dist/index.js
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
write_local_candidate_state() {{ printf 'STATE=%s:%s\n' "$1" "$2"; }}
prepare_local_candidate_runtime_artifacts_in_worktree
printf 'READY=%s\n' "$LOCAL_CANDIDATE_RUNTIME_READY"
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
    artifact_root = candidate / "runtime-artifacts" / "feedface"
    assert "STATE=ready:feedface" in result.stdout
    assert "READY=1" in result.stdout
    assert (artifact_root / "frontend" / "dist" / "index.html").is_file()
    assert (artifact_root / "backend" / "dist" / "index.js").is_file()
    assert (artifact_root / "backend" / "deps.json").is_file()
    assert not (artifact_root / "backend" / "node_modules").exists()
    assert (artifact_root / "ready.json").is_file()


def test_archive_discards_large_runtime_artifacts_before_history_move() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(source, "archive_local_candidate", "local_candidate_suspicion_reason")
    remove_at = block.index('rm -rf -- "$LOCAL_CANDIDATE_DIR/runtime-artifacts"')
    move_at = block.index('mv "$LOCAL_CANDIDATE_DIR" "$archived_candidate_dir"')
    assert remove_at < move_at


def test_backend_prebuilt_installer_replaces_runtime_tree_without_build(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(source, "install_backend_prebuilt_artifact", "deploy_backend")
    functions = tmp_path / "backend-install.sh"
    functions.write_text(block, encoding="utf-8")

    live = tmp_path / "live-backend"
    artifact = tmp_path / "artifact-backend"
    dep_key = "b" * 64
    layer = tmp_path / "layer"
    (live / "dist").mkdir(parents=True)
    (live / "node_modules" / "oldpkg").mkdir(parents=True)
    (artifact / "dist").mkdir(parents=True)
    (layer / "node_modules" / "newpkg").mkdir(parents=True)
    (live / "dist" / "index.js").write_text("old", encoding="utf-8")
    (live / "node_modules" / "oldpkg" / "index.js").write_text("old", encoding="utf-8")
    (artifact / "dist" / "index.js").write_text("new", encoding="utf-8")
    (artifact / "deps.json").write_text(f'{{"mode":"prod","deps_key":"{dep_key}"}}', encoding="utf-8")
    (layer / "node_modules" / "newpkg" / "index.js").write_text("new", encoding="utf-8")

    harness = f'''
set -eu -o pipefail
source "{functions}"
BACK_DIR="{live}"
UPDATE_RUNTIME_RUN_ID=test-run
LAST_ERROR_STDERR=''
node_dependency_layer_root() {{ printf '%s\n' "{layer}"; }}
verify_node_dependency_layer() {{ return 0; }}
sudo() {{
  if [[ "${{1:-}}" == -u ]]; then shift 2; fi
  [[ "${{1:-}}" == -H ]] && shift
  "$@"
}}
install_backend_prebuilt_artifact "{artifact}"
printf 'DIST=%s\n' "$(cat "$BACK_DIR/dist/index.js")"
printf 'MODULE=%s\n' "$(cat "$BACK_DIR/node_modules/newpkg/index.js")"
test ! -e "$BACK_DIR/node_modules/oldpkg"
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "DIST=new" in result.stdout
    assert "MODULE=new" in result.stdout
