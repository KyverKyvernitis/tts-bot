from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "updater" / "core" / "atualizar.sh"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _release_functions(source: str) -> str:
    start = source.index("frontend_release_root_for_key() {")
    end = source.index("\nrollback_after_failure() {", start)
    return source[start:end]


def test_runtime_snapshot_references_frontend_release_and_detects_tamper(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "release-functions.sh"
    functions.write_text(_release_functions(source), encoding="utf-8")

    front = tmp_path / "www" / "sinuca"
    front_releases = tmp_path / "www" / "sinuca-releases"
    back = tmp_path / "repo" / "dashboard" / "backend"
    releases = tmp_path / "runtime-releases"
    front.mkdir(parents=True)
    (front / "index.html").write_text("front-old", encoding="utf-8")
    (back / "dist").mkdir(parents=True)
    (back / "node_modules" / "pkg").mkdir(parents=True)
    (back / "node_modules" / ".bin").mkdir(parents=True)
    (back / "dist" / "index.js").write_text("server-old", encoding="utf-8")
    (back / "package.json").write_text('{"name":"backend"}', encoding="utf-8")
    (back / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    (back / "node_modules" / "pkg" / "index.js").write_text("module", encoding="utf-8")
    (back / "node_modules" / ".bin" / "pkg").symlink_to("../pkg/index.js")

    harness = f'''
source "{functions}"
RUNTIME_RELEASE_ROOT="{releases}"
FRONT_PUBLISH_DIR="{front}"
FRONT_RELEASE_ROOT="{front_releases}"
FRONT_RELEASE_RETENTION=4
BACK_DIR="{back}"
FRONT_CHANGED=1
BACK_CHANGED=1
REQUIREMENTS_CHANGED=0
PREVIOUS_COMMIT=deadbeef
RUNTIME_RELEASE_SNAPSHOT_READY=0
RUNTIME_RELEASE_SNAPSHOT_COMMIT=''
RUNTIME_RELEASE_SNAPSHOT_ROOT=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
LOG_TAG=test
BASELINE_KEY={'a'*64}
sanitize_commit_ref() {{ printf '%s\n' "$1"; }}
short_commit() {{ printf '%.7s' "$1"; }}
logger() {{ :; }}
backend_live_dependency_layer() {{ LAST_NODE_DEP_LAYER_KEY="${{BASELINE_KEY}}"; LAST_NODE_DEP_LAYER_PATH="{tmp_path / 'node-layer'}"; return 0; }}
node_dependency_layer_root() {{ printf '%s\n' "{tmp_path / 'node-layer'}"; }}
verify_node_dependency_layer() {{ return 0; }}
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
capture_runtime_release_snapshot "$PREVIOUS_COMMIT"
root="$(runtime_release_root_for_commit "$PREVIOUS_COMMIT")"
verify_runtime_release_component "$root" frontend
verify_runtime_release_component "$root" backend
printf 'READY=%s\n' "$RUNTIME_RELEASE_SNAPSHOT_READY"
printf 'FRONT=%s\n' "$(cat "$FRONT_PUBLISH_DIR/index.html")"
printf 'BACK=%s\n' "$(cat "$root/backend/dist/index.js")"
test -s "$root/frontend.json"
test ! -e "$root/frontend"
test -L "$FRONT_PUBLISH_DIR"
test -s "{front_releases}/deadbeef/.tts-release.json"
printf tamper >> "{front_releases}/deadbeef/index.html"
if verify_runtime_release_component "$root" frontend; then
  echo TAMPER_ACCEPTED
  exit 55
fi
printf 'TAMPER=blocked\n'
'''
    result = _run_bash(harness)
    assert "READY=1" in result.stdout
    assert "FRONT=front-old" in result.stdout
    assert "BACK=server-old" in result.stdout
    assert "TAMPER=blocked" in result.stdout
    assert "TAMPER_ACCEPTED" not in result.stdout


def test_local_candidate_preserves_runtime_before_live_promotion() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("prepare_local_candidate_update() {")
    end = source.index("\npublish_local_candidate_after_validation() {", start)
    block = source[start:end]

    ready_at = block.index("prepare_local_candidate_runtime_artifacts_in_worktree")
    snapshot_at = block.index('capture_runtime_release_snapshot "$PREVIOUS_COMMIT"', ready_at)
    promote_at = block.index("promote_local_candidate_worktree_commit", snapshot_at)
    assert ready_at < snapshot_at < promote_at


def test_remote_candidate_preserves_runtime_before_fast_forward() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    remote_start = source.index('REMOTE_CANDIDATE_MODE=1')
    block = source[remote_start:]
    snapshot_at = block.index('capture_runtime_release_snapshot "$PREVIOUS_COMMIT"')
    merge_at = block.index('repo_git merge --ff-only "$REMOTE_COMMIT"', snapshot_at)
    assert snapshot_at < merge_at


def test_rollback_never_rebuilds_frontend_or_backend() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("rollback_after_failure() {")
    end = source.index("\nhandle_post_deploy_failure() {", start)
    block = source[start:end]

    assert 'restore_frontend_runtime_release "$PREVIOUS_COMMIT"' in block
    assert 'restore_backend_runtime_release "$PREVIOUS_COMMIT"' in block
    assert "if deploy_frontend" not in block
    assert "if deploy_backend" not in block
    assert "npm ci" not in block
    assert "npm install" not in block
    assert "npm run build" not in block


def test_frontend_restore_switches_saved_release_without_build(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "release-functions.sh"
    functions.write_text(_release_functions(source), encoding="utf-8")
    runtime_root = tmp_path / "runtime-releases" / "deadbeef"
    runtime_root.mkdir(parents=True)
    front_source = tmp_path / "front-source"
    front_source.mkdir()
    (front_source / "index.html").write_text("old", encoding="utf-8")
    front_publish = tmp_path / "www" / "sinuca"
    front_releases = tmp_path / "www" / "sinuca-releases"

    harness = f'''
source "{functions}"
RUNTIME_RELEASE_ROOT="{tmp_path / 'runtime-releases'}"
FRONT_PUBLISH_DIR="{front_publish}"
FRONT_RELEASE_ROOT="{front_releases}"
FRONT_RELEASE_RETENTION=4
PREVIOUS_COMMIT=deadbeef
FRONT_STATUS=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
LOG_TAG=test
sanitize_commit_ref() {{ printf '%s\n' "$1"; }}
short_commit() {{ printf '%.7s' "$1"; }}
logger() {{ :; }}
prepare_frontend_release "{front_source}" frontkey >/dev/null
RUNTIME_MANIFEST="{runtime_root / 'release.json'}" python3 - <<'PYRUNTIME'
import json, os, pathlib
p = pathlib.Path(os.environ['RUNTIME_MANIFEST'])
p.write_text(json.dumps({{'state':'ready','commit':'deadbeef','frontend':{{'ready':True,'release_key':'frontkey','sha256':''}},'backend':{{'ready':False}}}}), encoding='utf-8')
PYRUNTIME
restore_frontend_runtime_release deadbeef
printf 'STATUS=%s\n' "$FRONT_STATUS"
printf 'ACTIVE=%s\n' "$(frontend_active_release_key)"
printf 'BODY=%s\n' "$(cat "$FRONT_PUBLISH_DIR/index.html")"
'''
    result = _run_bash(harness)
    assert "ACTIVE=frontkey" in result.stdout
    assert "BODY=old" in result.stdout
    assert "sem rebuild" in result.stdout


def test_backend_restore_consumes_saved_release_and_only_restarts(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "release-functions.sh"
    functions.write_text(_release_functions(source), encoding="utf-8")
    root = tmp_path / "releases" / "deadbeef"
    (root / "backend" / "dist").mkdir(parents=True)
    (root / "backend" / "dist" / "index.js").write_text("old", encoding="utf-8")
    (root / "backend" / "deps.json").write_text('{"mode":"prod","deps_key":"' + ('a' * 64) + '"}', encoding="utf-8")

    harness = f'''
source "{functions}"
RUNTIME_RELEASE_ROOT="{tmp_path / 'releases'}"
PREVIOUS_COMMIT=deadbeef
BACK_SERVICE=fake.service
BACK_HEALTH_URL=http://127.0.0.1:9/health
BACK_STATUS=''
ACTIVITY_HEALTHCHECK_STATUS=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
sanitize_commit_ref() {{ printf '%s\n' "$1"; }}
short_commit() {{ printf '%.7s' "$1"; }}
verify_runtime_release_component() {{ return 0; }}
install_backend_prebuilt_artifact() {{ printf 'INSTALL=%s\n' "$1"; }}
systemctl() {{ printf 'SYSTEMCTL=%s\n' "$*"; return 0; }}
wait_for_service_active() {{ return 0; }}
sleep() {{ :; }}
wait_for_health() {{ return 0; }}
journalctl() {{ :; }}
trim_alert_text() {{ cat; }}
restore_backend_runtime_release deadbeef
printf 'STATUS=%s\n' "$BACK_STATUS"
printf 'HEALTH=%s\n' "$ACTIVITY_HEALTHCHECK_STATUS"
'''
    result = _run_bash(harness)
    assert f"INSTALL={root / 'backend'}" in result.stdout
    assert "SYSTEMCTL=restart fake.service" in result.stdout
    assert "sem rebuild" in result.stdout
    assert "HEALTH=OK" in result.stdout


def test_runtime_mutation_flags_cover_published_frontend_and_backend_dependency_changes() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    frontend = source[source.index("deploy_frontend() {") : source.index("\ninstall_backend_prebuilt_artifact() {")]
    backend = source[source.index("deploy_backend() {") : source.index("\nruntime_release_root_for_commit() {")]
    assert frontend.count("FRONT_RUNTIME_MUTATED=1") >= 2
    assert "BACK_RUNTIME_MUTATED=1" in backend
    npm_at = backend.rindex('STAGE="dependências do backend"')
    mutation_at = backend.rindex("BACK_RUNTIME_MUTATED=1", 0, npm_at)
    assert mutation_at < npm_at
