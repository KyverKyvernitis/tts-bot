from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"


def _run(source: str, *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-Eeuo", "pipefail", "-c", source],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_local_ready_resume_is_checked_before_worktree_creation() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("prepare_local_candidate_update() {")
    end = source.index("\npublish_local_candidate_after_validation() {", start)
    block = source[start:end]
    assert block.index("resume_local_ready_before_worktree") < block.index("create_local_candidate_worktree")
    assert "READY anterior confirmado" in block


def test_remote_ready_resume_is_checked_before_remote_worktree_preflight() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index('STAGE="reutilização READY do commit remoto"')
    end = source.index('STAGE="preservação do runtime anterior"', start)
    block = source[start:end]
    assert block.index('reuse_ready_artifacts_for_commit "$REMOTE_COMMIT"') < block.index("validate_remote_commit_in_staging")
    assert "reutilizou READY antes de criar worktree" in block


def test_ready_state_can_resume_dangling_candidate_commit_without_worktree(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("local_candidate_ready_commit_from_state() {")
    end = source.index("\nprepare_local_candidate_runtime_artifacts_in_worktree() {", start)
    funcs = tmp_path / "funcs.sh"
    funcs.write_text(source[start:end], encoding="utf-8")

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    commit = "a" * 40
    parent = "b" * 40
    (candidate / "state.json").write_text(
        '{"state":"ready","commit":"' + commit + '"}', encoding="utf-8"
    )

    harness = f'''
source "{funcs}"
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_DIR="{candidate}"
LOCAL_CANDIDATE_ID=zip-fast
CURRENT_COMMIT={parent}
LOCAL_CANDIDATE_PREPARED_COMMIT=''
CHANGED_FILES_RAW=''
CHANGED_STATUS_RAW=''
CHANGED_DIFF_NUMSTAT_RAW=''
REPO_DIR="/mnt/data/work_wave17"
LOG_TAG=test
repo_git() {{
  case "$1" in
    cat-file) return 0 ;;
    rev-parse) printf '%s\\n' '{parent}' ;;
    log) printf 'update\\n\\nCandidate-ID: zip-fast\\n' ;;
    *) echo "unexpected repo_git: $*" >&2; return 90 ;;
  esac
}}
load_git_diff_snapshot() {{
  CHANGED_FILES_RAW='dashboard/frontend/src/App.tsx'
  CHANGED_STATUS_RAW=$'M\\tdashboard/frontend/src/App.tsx'
  return 0
}}
classify_changed_files() {{ FRONT_CHANGED=1; BACK_CHANGED=0; REQUIREMENTS_CHANGED=0; BOT_CHANGED=0; FRONT_TESTS_CHANGED=0; BACK_TESTS_CHANGED=0; }}
reuse_ready_artifacts_for_commit() {{ [[ "$1" == '{commit}' ]]; READY_FAST_PATH_USED=1; return 0; }}
write_local_candidate_state() {{ printf 'STATE=%s COMMIT=%s\\n' "$1" "$2"; }}
logger() {{ :; }}
short_commit() {{ printf '%.7s' "$1"; }}
resume_local_ready_before_worktree
printf 'PREPARED=%s FAST=%s\\n' "$LOCAL_CANDIDATE_PREPARED_COMMIT" "$READY_FAST_PATH_USED"
'''
    result = _run(harness)
    assert f"PREPARED={commit}" in result.stdout
    assert "FAST=1" in result.stdout
    assert f"STATE=ready COMMIT={commit}" in result.stdout


def test_heavy_maintenance_is_throttled_by_stamp(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("updater_heavy_maintenance_due() {")
    end = source.index("\nprune_update_artifacts() {", start)
    funcs = tmp_path / "maintenance.sh"
    funcs.write_text(source[start:end], encoding="utf-8")
    root = tmp_path / "candidates"
    root.mkdir()
    stamp = root / ".heavy-maintenance.stamp"

    harness = f'''
source "{funcs}"
CANDIDATE_ROOT="{root}"
UPDATE_HEAVY_MAINTENANCE_INTERVAL_SECONDS=3600
install() {{ command install "$@"; }}
chown() {{ :; }}
if updater_heavy_maintenance_due; then echo FIRST=due; else echo FIRST=skip; fi
mark_updater_heavy_maintenance
if updater_heavy_maintenance_due; then echo SECOND=due; else echo SECOND=skip; fi
'''
    result = _run(harness)
    assert "FIRST=due" in result.stdout
    assert "SECOND=skip" in result.stdout
    assert stamp.exists()

    old = time.time() - 7200
    os.utime(stamp, (old, old))
    result = _run(
        f'''source "{funcs}"; CANDIDATE_ROOT="{root}"; UPDATE_HEAVY_MAINTENANCE_INTERVAL_SECONDS=3600; updater_heavy_maintenance_due && echo DUE'''
    )
    assert "DUE" in result.stdout


def test_pruning_and_ready_finalize_avoid_recursive_permission_walks() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    node_start = source.index("prune_node_dependency_layers() {")
    node_end = source.index("\nfrontend_typescript_cache_key() {", node_start)
    node = source[node_start:node_end]
    ts_start = source.index("prune_typescript_cache() {")
    ts_end = source.index("\nselect_node_test_plan() {", ts_start)
    ts = source[ts_start:ts_end]
    prep_start = source.index("prepare_local_candidate_runtime_artifacts_in_worktree() {")
    prep_end = source.index("\npromote_local_candidate_worktree_commit() {", prep_start)
    prep = source[prep_start:prep_end]

    assert "chmod -R" not in node
    assert "chmod -R" not in ts
    assert 'chown -R ubuntu:ubuntu "$root"' not in prep
    assert 'chown ubuntu:ubuntu "$root/ready.json"' in prep
    assert 'find "$root" -type d -exec chmod' not in prep



def test_python_and_runtime_release_finalize_without_recursive_chown() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    py_start = source.index("prepare_candidate_python_runtime() {")
    py_end = source.index("\ncapture_python_runtime_release_snapshot() {", py_start)
    py_block = source[py_start:py_end]
    snap_start = source.index("capture_python_runtime_release_snapshot() {")
    snap_end = source.index("\nactivate_python_runtime_release() {", snap_start)
    snap_block = source[snap_start:snap_end]
    runtime_start = source.index("capture_runtime_release_snapshot() {")
    runtime_end = source.index("\nrestore_frontend_runtime_release() {", runtime_start)
    runtime_block = source[runtime_start:runtime_end]

    assert 'chown -R ubuntu:ubuntu "$root"' not in py_block
    assert 'chown ubuntu:ubuntu "$root/python.json"' in py_block
    assert 'chown -R ubuntu:ubuntu "$root"' not in snap_block
    assert 'chown ubuntu:ubuntu "$root/python.json"' in snap_block
    assert 'chown -R ubuntu:ubuntu "$root"' not in runtime_block
    assert 'find "$root" -type d -exec chmod' not in runtime_block

def test_cache_summary_surfaces_ready_fast_path() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert 'CACHE_TEXT+=" · READY hit"' in source
