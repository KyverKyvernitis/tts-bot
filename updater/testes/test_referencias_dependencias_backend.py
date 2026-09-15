from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core


ROOT = Path(__file__).resolve().parents[2]
UPDATER = caminho_fonte_core()


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _cache_functions(source: str) -> str:
    start = source.index("node_dependency_cache_key() {")
    end = source.index("\nprepare_local_candidate_runtime_artifacts_in_worktree() {", start)
    return source[start:end]


def _write_project(project: Path) -> None:
    project.mkdir(parents=True)
    (project / "package.json").write_text(
        '{"name":"backend","version":"1.0.0","private":true}\n', encoding="utf-8"
    )
    (project / "package-lock.json").write_text(
        '{"name":"backend","version":"1.0.0","lockfileVersion":3,"requires":true,"packages":{}}\n',
        encoding="utf-8",
    )


def test_live_backend_node_modules_is_adopted_by_rename_without_copy(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "cache-functions.sh"
    functions.write_text(_cache_functions(source), encoding="utf-8")
    project = tmp_path / "backend"
    cache = tmp_path / "cache"
    _write_project(project)
    module = project / "node_modules" / "pkg" / "index.js"
    module.parent.mkdir(parents=True)
    module.write_text("module", encoding="utf-8")
    inode_before = os.stat(module).st_ino

    harness = f'''
source "{functions}"
NODE_DEPENDENCY_CACHE_ROOT="{cache}"
UPDATE_RUNTIME_RUN_ID=test-run
LOG_TAG=test
logger() {{ :; }}
chown() {{ :; }}
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
sudo() {{
  if [[ "${{1:-}}" == -u ]]; then shift 2; fi
  [[ "${{1:-}}" == -H ]] && shift
  "$@"
}}
backend_live_dependency_layer "{project}"
printf 'KEY=%s\n' "$LAST_NODE_DEP_LAYER_KEY"
printf 'LAYER=%s\n' "$LAST_NODE_DEP_LAYER_PATH"
test -L "{project}/node_modules"
test -f "{project}/node_modules/pkg/index.js"
'''
    result = _run_bash(harness)
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    key = lines["KEY"]
    layer = Path(lines["LAYER"])
    assert len(key) == 64
    assert layer == cache / "backend" / "prod" / key
    assert (layer / "layer.json").is_file()
    assert os.stat(project / "node_modules" / "pkg" / "index.js").st_ino == inode_before
    assert (project / "node_modules").is_symlink()


def test_ready_and_runtime_release_reference_dependency_key_instead_of_node_modules() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    prepare = source[
        source.index("prepare_local_candidate_runtime_artifacts_in_worktree() {") :
        source.index("\npromote_local_candidate_worktree_commit() {", source.index("prepare_local_candidate_runtime_artifacts_in_worktree() {"))
    ]
    snapshot = source[
        source.index("capture_runtime_release_snapshot() {") :
        source.index("\nrestore_frontend_runtime_release() {", source.index("capture_runtime_release_snapshot() {"))
    ]
    installer = source[
        source.index("install_backend_prebuilt_artifact() {") :
        source.index("\ndeploy_backend() {", source.index("install_backend_prebuilt_artifact() {"))
    ]

    assert '"$back_artifact/deps.json"' in prepare
    assert 'cp -a -- "$backend_runtime_modules"' not in prepare
    assert 'cp -a -- "$BACK_DIR/node_modules"' not in snapshot
    assert '"$tmp/backend/deps.json"' in snapshot
    assert 'ln -s -- "$deps_layer/node_modules" "$temp_modules"' in installer
    assert 'cp -a -- "$source_dir/node_modules"' not in installer


def test_dependency_pruner_keeps_keys_referenced_by_rollback_release(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "cache-functions.sh"
    functions.write_text(_cache_functions(source), encoding="utf-8")
    cache = tmp_path / "cache"
    releases = tmp_path / "releases"
    candidates = tmp_path / "candidates"
    remote = tmp_path / "remote"
    group = cache / "backend" / "prod"

    keys = [f"{idx:064x}" for idx in range(1, 6)]
    for idx, key in enumerate(keys, start=1):
        layer = group / key
        (layer / "node_modules").mkdir(parents=True)
        (layer / "layer.json").write_text(
            json.dumps({"state": "ready", "key": key, "mode": "prod"}), encoding="utf-8"
        )
        subprocess.run(["touch", "-d", f"2026-01-0{idx} 00:00:00", str(layer)], check=True)

    protected = keys[0]
    release = releases / "deadbeef"
    release.mkdir(parents=True)
    (release / "release.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "backend": {"ready": True, "deps_key": protected, "sha256": "x"},
            }
        ),
        encoding="utf-8",
    )

    harness = f'''
source "{functions}"
NODE_DEPENDENCY_CACHE_ROOT="{cache}"
NODE_DEPENDENCY_CACHE_RETENTION=2
RUNTIME_RELEASE_ROOT="{releases}"
CANDIDATE_ROOT="{candidates}"
REMOTE_RUNTIME_ARTIFACT_ROOT="{remote}"
BACK_DIR="{tmp_path / 'backend-live'}"
prune_node_dependency_layers
find "{group}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
'''
    result = _run_bash(harness)
    remaining = set(result.stdout.splitlines())
    assert protected in remaining
    assert keys[-1] in remaining
    assert keys[-2] in remaining
    assert len(remaining) == 3


def test_backend_restart_wait_is_polling_not_fixed_three_seconds(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("wait_for_service_active() {")
    end = source.index("\nwait_for_health() {", start)
    functions = tmp_path / "wait-functions.sh"
    functions.write_text(source[start:end], encoding="utf-8")

    harness = f'''
source "{functions}"
CHECKS=0
SLEEPS=0
systemctl() {{
  if [[ "$1" == is-active ]]; then
    CHECKS=$((CHECKS + 1))
    (( CHECKS >= 3 )) && return 0
    return 1
  fi
  if [[ "$1" == is-failed ]]; then
    return 1
  fi
  return 0
}}
sleep() {{ SLEEPS=$((SLEEPS + 1)); }}
wait_for_service_active fake.service 8 0.25
printf 'CHECKS=%s SLEEPS=%s\n' "$CHECKS" "$SLEEPS"
'''
    result = _run_bash(harness)
    assert "CHECKS=3 SLEEPS=2" in result.stdout

    deploy = source[
        source.index("deploy_backend() {") : source.index("\nruntime_release_root_for_commit() {")
    ]
    restore = source[
        source.index("restore_backend_runtime_release() {") : source.index("\nprune_runtime_releases() {")
    ]
    assert "sleep 3" not in deploy
    assert "sleep 3" not in restore
    assert 'wait_for_service_active "$BACK_SERVICE" 20 0.25' in deploy
    assert 'wait_for_service_active "$BACK_SERVICE" 20 0.25' in restore
