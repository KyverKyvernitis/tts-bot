from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "updater" / "core" / "atualizar.sh"


def _run_bash(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Updater Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def _worktree_harness_prefix(repo: Path, staging: Path) -> str:
    return f"""
source <(awk '/^discard_local_candidate_worktree[(][)]/{{flag=1}} /^refresh_changed_files_from_staged_diff[(][)]/{{flag=0}} flag' {UPDATER!s})
REPO_DIR={repo!s}
CANDIDATE_ROOT={staging!s}
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_ID='zip-test-worktree'
LOCAL_CANDIDATE_DISPLAY_ID='UPD-WORKTREE'
LOCAL_CANDIDATE_COMMIT_MESSAGE='update: worktree test'
LOCAL_CANDIDATE_SOURCE_AUTHOR_ID='123'
LOCAL_CANDIDATE_ZIP_SHA256='abc123'
LOCAL_CANDIDATE_WORKTREE_DIR=''
LOCAL_CANDIDATE_PREPARED_COMMIT=''
LOCAL_CANDIDATE_ALREADY_PROMOTED=0
LOCAL_CANDIDATE_PUBLISHED=0
LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY=0
UPDATE_APPLIED=0
UPDATE_RUNTIME_RUN_ID='test-run'
LOG_TAG='test-updater'
CURRENT_COMMIT=$(git -C "$REPO_DIR" rev-parse HEAD)
PREVIOUS_COMMIT="$CURRENT_COMMIT"
REMOTE_COMMIT="$CURRENT_COMMIT"
SHORT_FROM="${{CURRENT_COMMIT:0:7}}"
SHORT_TO='local'
CHANGED_STATUS_RAW=''
CHANGED_FILES_RAW=''
CHANGED_DIFF_NUMSTAT_RAW=''
repo_git() {{ git -C "$REPO_DIR" "$@"; }}
candidate_repo_dir() {{
  if [[ -n "${{LOCAL_CANDIDATE_WORKTREE_DIR:-}}" && -d "$LOCAL_CANDIDATE_WORKTREE_DIR" ]]; then
    printf '%s\\n' "$LOCAL_CANDIDATE_WORKTREE_DIR"
  else
    printf '%s\\n' "$REPO_DIR"
  fi
}}
candidate_git() {{ git -C "$(candidate_repo_dir)" "$@"; }}
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
sanitize_update_component() {{ printf '%s\\n' "$1" | tr -cs '[:alnum:]._-' '_'; }}
short_commit() {{ printf '%.7s' "$1"; }}
logger() {{ :; }}
write_local_candidate_state() {{ :; }}
collect_local_tracked_changes() {{ git -C "$REPO_DIR" status --short --untracked-files=no; }}
classify_changed_files() {{ :; }}
mark_deployment_committed() {{ :; }}
mark_update_timing() {{ :; }}
append_update_timing_ms() {{ :; }}
UPDATER_STEP_LAST=0
update_now_ms() {{ date +%s%3N; }}
log_update_operation_timing_ms() {{ :; }}
"""


def test_candidate_commit_is_built_offline_before_live_promotion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    staging = tmp_path / "staging"
    base = _init_repo(repo)
    harness = _worktree_harness_prefix(repo, staging) + r'''
run_preflight_checks_in_dir() { return 0; }
create_local_candidate_worktree
printf "VALUE = 'new'\n" > "$LOCAL_CANDIDATE_WORKTREE_DIR/app.py"
candidate_git add -- app.py
CHANGED_FILES_RAW='app.py'
CHANGED_STATUS_RAW=$'M\tapp.py'
prepare_local_candidate_commit_in_worktree
prepared="$LOCAL_CANDIDATE_PREPARED_COMMIT"
printf 'LIVE_BEFORE=%s\n' "$(cat "$REPO_DIR/app.py")"
printf 'HEAD_BEFORE=%s\n' "$(git -C "$REPO_DIR" rev-parse HEAD)"
printf 'PREPARED=%s\n' "$prepared"
promote_local_candidate_worktree_commit
printf 'LIVE_AFTER=%s\n' "$(cat "$REPO_DIR/app.py")"
printf 'HEAD_AFTER=%s\n' "$(git -C "$REPO_DIR" rev-parse HEAD)"
printf 'APPLIED=%s\n' "$UPDATE_APPLIED"
printf 'WORKTREE=%s\n' "$LOCAL_CANDIDATE_WORKTREE_DIR"
'''
    result = _run_bash(harness)
    assert "LIVE_BEFORE=VALUE = 'old'" in result.stdout
    assert f"HEAD_BEFORE={base}" in result.stdout
    prepared = next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("PREPARED="))
    assert prepared and prepared != base
    assert "LIVE_AFTER=VALUE = 'new'" in result.stdout
    assert f"HEAD_AFTER={prepared}" in result.stdout
    assert "APPLIED=1" in result.stdout
    assert "WORKTREE=" in result.stdout
    assert not any((staging / "worktrees").glob("*"))


def test_invalid_python_fails_in_worktree_without_touching_live_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    staging = tmp_path / "staging"
    base = _init_repo(repo)
    harness = _worktree_harness_prefix(repo, staging) + f'''
source <(awk '/^run_preflight_checks_in_dir[(][)]/{{flag=1}} /^validate_remote_commit_in_staging[(][)]/{{flag=0}} flag' {UPDATER!s})
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
create_local_candidate_worktree
printf 'def broken(:\\n' > "$LOCAL_CANDIDATE_WORKTREE_DIR/app.py"
candidate_git add -- app.py
CHANGED_FILES_RAW='app.py'
CHANGED_STATUS_RAW=$'M\\tapp.py'
set +e
prepare_local_candidate_commit_in_worktree
rc=$?
set -e
printf 'RC=%s\\n' "$rc"
printf 'LIVE=%s\\n' "$(cat "$REPO_DIR/app.py")"
printf 'HEAD=%s\\n' "$(git -C "$REPO_DIR" rev-parse HEAD)"
printf 'APPLIED=%s\\n' "$UPDATE_APPLIED"
'''
    result = _run_bash(harness)
    assert "RC=1" in result.stdout
    assert "LIVE=VALUE = 'old'" in result.stdout
    assert f"HEAD={base}" in result.stdout
    assert "APPLIED=0" in result.stdout


def test_promoted_candidate_is_resumable_before_push(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    staging = tmp_path / "staging"
    base = _init_repo(repo)
    (repo / "app.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "-qm",
            "candidate",
            "-m",
            "Candidate-ID: zip-test-worktree",
        ],
        check=True,
    )
    candidate = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    harness = _worktree_harness_prefix(repo, staging) + f'''
REMOTE_COMMIT={base!s}
CURRENT_COMMIT={candidate!s}
PREVIOUS_COMMIT="$CURRENT_COMMIT"
local_live_head_candidate_state
printf 'CURRENT=%s\\n' "$CURRENT_COMMIT"
printf 'PREVIOUS=%s\\n' "$PREVIOUS_COMMIT"
printf 'REMOTE=%s\\n' "$REMOTE_COMMIT"
printf 'PREPARED=%s\\n' "$LOCAL_CANDIDATE_PREPARED_COMMIT"
printf 'APPLIED=%s\\n' "$UPDATE_APPLIED"
printf 'FILES=%s\\n' "$CHANGED_FILES_RAW"
'''
    result = _run_bash(harness)
    assert f"CURRENT={base}" in result.stdout
    assert f"PREVIOUS={base}" in result.stdout
    assert f"REMOTE={candidate}" in result.stdout
    assert f"PREPARED={candidate}" in result.stdout
    assert "APPLIED=1" in result.stdout
    assert "FILES=app.py" in result.stdout


def test_cleanup_runtime_removes_local_candidate_worktree() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    cleanup = source[source.index("cleanup_runtime_artifacts() {") : source.index("trim_alert_text() {")]
    assert 'LOCAL_CANDIDATE_WORKTREE_DIR' in cleanup
    assert 'repo_git worktree remove --force "$LOCAL_CANDIDATE_WORKTREE_DIR"' in cleanup


def test_prepromotion_cleanup_never_removes_live_untracked_candidate_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    untracked = repo / "candidate-new.py"
    untracked.write_text("LOCAL = True\n", encoding="utf-8")
    harness = f'''
source <(awk '/^cleanup_local_candidate_new_files_after_reset[(][)]/{{flag=1}} /^git_add_changed_files[(][)]/{{flag=0}} flag' {UPDATER!s})
LOCAL_CANDIDATE_MODE=1
UPDATE_APPLIED=0
PREVIOUS_COMMIT={base!s}
REPO_DIR={repo!s}
CHANGED_FILES_RAW='candidate-new.py'
cleanup_local_candidate_new_files_after_reset
printf 'EXISTS=%s\\n' "$([[ -f "$REPO_DIR/candidate-new.py" ]] && echo yes || echo no)"
'''
    result = _run_bash(harness)
    assert "EXISTS=yes" in result.stdout
    assert untracked.read_text(encoding="utf-8") == "LOCAL = True\n"
