from __future__ import annotations

import os
import subprocess
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core


ROOT = Path(__file__).resolve().parents[2]
UPDATER = caminho_fonte_core()
SNAPSHOT = ROOT / "updater" / "utilitarios" / "estado_git.py"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Updater Test"], check=True)


def test_git_diff_snapshot_collects_status_files_and_numstat_in_one_git_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "with space.txt").write_text("one\n", encoding="utf-8")
    (repo / "obsolete.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)

    (repo / "with space.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "obsolete.py").unlink()
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

    result = _run_bash(
        f'''eval "$(python3 {SNAPSHOT!s} diff --repo {repo!s} --cached)"
printf '%s\n--FILES--\n%s\n--NUMSTAT--\n%s\n' "$GIT_DIFF_STATUS_RAW" "$GIT_DIFF_FILES_RAW" "$GIT_DIFF_NUMSTAT_RAW"
'''
    )
    assert "M\twith space.txt" in result.stdout
    assert "D\tobsolete.py" in result.stdout
    assert "A\tnew.txt" in result.stdout
    files_block = result.stdout.split("--FILES--\n", 1)[1].split("\n--NUMSTAT--", 1)[0].splitlines()
    assert set(files_block) == {"with space.txt", "obsolete.py", "new.txt"}
    assert "1\t0\twith space.txt" in result.stdout

    source = SNAPSHOT.read_text(encoding="utf-8")
    assert '"--raw", "--numstat", "--no-renames", "-z"' in source


def test_git_status_snapshot_separates_staged_and_unstaged_without_extra_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    (repo / "a.txt").write_text("3\n", encoding="utf-8")

    result = _run_bash(
        f'''eval "$(python3 {SNAPSHOT!s} status --repo {repo!s})"
printf 'STATUS=%s\nSTAGED=%s\nUNSTAGED=%s\n' "$GIT_STATUS_RAW" "$GIT_STATUS_STAGED_FILES_RAW" "$GIT_STATUS_UNSTAGED_FILES_RAW"
'''
    )
    assert "STATUS=MM a.txt" in result.stdout
    assert "STAGED=a.txt" in result.stdout
    assert "UNSTAGED=a.txt" in result.stdout


def test_impact_classification_is_single_shell_pass_without_grep_processes() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    fn = source[source.index("classify_changed_files() {") : source.index("\nfast_reload_modules_for_changed_files() {", source.index("classify_changed_files() {"))]
    assert "grep " not in fn
    harness = f'''
source <(awk '/^classify_changed_files[(][)]/{{flag=1}} /^fast_reload_modules_for_changed_files[(][)]/{{flag=0}} flag' {UPDATER!s})
grep() {{ echo 'grep should not run' >&2; return 97; }}
CHANGED_FILES_RAW=$'dashboard/frontend/src/App.tsx\ndashboard/backend/tests/api.test.ts\ncogs/tts/cog.py\nrequirements.txt'
classify_changed_files
printf 'FRONT=%s FTEST=%s FTYPE=%s BTEST=%s BOT=%s REQ=%s CMD=%s\n' "$FRONT_CHANGED" "$FRONT_TESTS_REQUIRED" "$FRONT_TYPECHECK_REQUIRED" "$BACK_TESTS_CHANGED" "$BOT_CHANGED" "$REQUIREMENTS_CHANGED" "$APP_COMMANDS_MAY_HAVE_CHANGED"
'''
    result = _run_bash(harness)
    assert "FRONT=1 FTEST=1 FTYPE=1 BTEST=1 BOT=1 REQ=1 CMD=1" in result.stdout


def test_node_toolchain_identity_is_cached_for_the_run(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("load_node_toolchain_identity() {")
    end = source.index("\n\nnode_dependency_cache_key() {", start)
    fn = source[start:end]
    helper = tmp_path / "toolchain.sh"
    helper.write_text(fn, encoding="utf-8")
    counter = tmp_path / "sudo-count"
    result = _run_bash(
        f'''source {helper!s}
NODE_TOOLCHAIN_NODE_VERSION=''
NODE_TOOLCHAIN_NPM_VERSION=''
NODE_TOOLCHAIN_PLATFORM=''
sudo() {{ printf '1\n' >> {counter!s}; printf 'v22.0.0\n10.9.0\n'; }}
load_node_toolchain_identity
load_node_toolchain_identity
printf 'NODE=%s NPM=%s COUNT=%s\n' "$NODE_TOOLCHAIN_NODE_VERSION" "$NODE_TOOLCHAIN_NPM_VERSION" "$(wc -l < {counter!s})"
'''
    )
    assert "NODE=v22.0.0 NPM=10.9.0 COUNT=1" in result.stdout


def test_updater_uses_snapshot_helpers_for_hot_diff_and_repo_status() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    refresh = source[source.index("refresh_changed_files_from_staged_diff() {") : source.index("\napply_local_candidate_patch_diff() {")]
    assert 'load_git_diff_snapshot "$root" --cached' in refresh
    assert "GIT_STATUS_UNSTAGED_FILES_RAW" in source
    assert 'load_repo_ref_snapshot "$BRANCH"' in source


def test_local_candidate_preflight_reuses_single_repo_status_snapshot() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    prepare_start = source.index("prepare_local_candidate_update() {")
    start = source.index('STAGE="verificação de alterações locais"', prepare_start)
    end = source.index('zip_progress_done_and_publish "Estado local validado"', start)
    block = source[start:end]

    assert block.count("load_repo_status_snapshot") == 1
    assert "clear_local_changes_marker_if_clean 1" in block
    assert "candidate_local_changes_are_expected 1" in block
    assert 'log_update_operation_timing_ms "preflight.git_status"' in block
    assert 'log_update_operation_timing_ms "preflight.local_changes_check"' in block
    assert 'log_update_operation_timing_ms "preflight.git_pull"' in block


def test_status_snapshot_reuse_is_explicit_and_does_not_change_default_fresh_reads() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    clear_start = source.index("clear_local_changes_marker_if_clean() {")
    clear_end = source.index("\ncollect_local_tracked_changes() {", clear_start)
    clear_fn = source[clear_start:clear_end]
    candidate_start = source.index("candidate_local_changes_are_expected() {")
    candidate_end = source.index("\nensure_no_unstaged_tracked_changes() {", candidate_start)
    candidate_fn = source[candidate_start:candidate_end]

    assert 'local reuse_snapshot="${1:-0}"' in clear_fn
    assert '"${GIT_STATUS_SNAPSHOT_READY:-0}" == "1"' in clear_fn
    assert 'local reuse_snapshot="${1:-0}"' in candidate_fn
    assert 'if [[ "$reuse_snapshot" != "1" || "${GIT_STATUS_SNAPSHOT_READY:-0}" != "1" ]]; then' in candidate_fn
    assert "load_repo_status_snapshot || return 2" in candidate_fn


def test_preloaded_status_snapshot_is_reused_without_second_git_scan(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    marker.write_text("dirty", encoding="utf-8")
    counter = tmp_path / "status-count"
    harness = f'''
source <(awk '/^clear_local_changes_marker_if_clean[(][)]/{{flag=1}} /^ensure_no_unstaged_tracked_changes[(][)]/{{flag=0}} flag' {UPDATER!s})
trim_alert_text() {{ cat; }}
load_repo_status_snapshot() {{
  printf '1\\n' >> {counter!s}
  GIT_STATUS_RAW=' M bot.py'
  GIT_STATUS_FILES_RAW='bot.py'
  GIT_STATUS_STAGED_FILES_RAW=''
  GIT_STATUS_UNSTAGED_FILES_RAW='bot.py'
  GIT_STATUS_SNAPSHOT_READY=1
}}
LOCAL_CHANGES_MARKER_FILE={marker!s}
LOCAL_CANDIDATE_MODE=1
CHANGED_FILES_RAW='bot.py'
load_repo_status_snapshot
clear_local_changes_marker_if_clean 1
candidate_local_changes_are_expected 1
printf 'COUNT=%s MARKER=%s\\n' "$(wc -l < {counter!s})" "$([[ -f {marker!s} ]] && echo yes || echo no)"
'''
    result = _run_bash(harness)
    assert "COUNT=1" in result.stdout
    assert "MARKER=yes" in result.stdout



def test_local_candidate_claim_precedes_maintenance_and_outbox_flushes() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    main = source[source.index("SECONDS=0") :]
    claim = main.index("if load_pending_rollback_request; then")
    local_claim = main.index("elif load_pending_local_candidate; then")
    maintenance = main.index("prune_update_artifacts || true")
    status_flush = main.index("flush_update_status_outbox || true")
    alert_flush = main.index("flush_update_alert_outbox || true")
    queue_refresh = main.index("refresh_pending_queue_messages || true")

    assert claim < local_claim < maintenance
    assert local_claim < status_flush
    assert local_claim < alert_flush
    assert local_claim < queue_refresh


def test_queue_claim_does_not_recursive_chown_history() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("load_pending_local_candidate() {")
    end = source.index("\nverify_local_candidate_integrity() {", start)
    block = source[start:end]

    assert 'chown -R ubuntu:ubuntu "$CANDIDATE_QUEUE_ROOT"' not in block
    assert "install -d -o ubuntu -g ubuntu -m 0775" in block


def test_worktree_prune_is_maintenance_only_not_candidate_creation() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    create_start = source.index("create_local_candidate_worktree() {")
    create_end = source.index("\nensure_candidate_worktree_staged_clean() {", create_start)
    create_block = source[create_start:create_end]
    prune_start = source.index("prune_updater_runtime_orphans() {")
    prune_end = source.index("\nprune_rejected_remote_commits() {", prune_start)
    prune_block = source[prune_start:prune_end]

    assert "repo_git worktree prune --expire=now" not in create_block
    assert "worktree prune --expire=now" in prune_block


def test_preparation_path_has_fine_grained_timing_labels() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    required = {
        "startup.disk_guard",
        "startup.queue_claim",
        "preparation.worktree_create",
        "preparation.apply_patch",
        "preparation.operations",
        "preparation.copy_files",
        "preparation.git_add",
        "preparation.staged_diff",
        "preparation.static_preflight",
        "preparation.worktree_clean_check",
        "preparation.commit",
        "preparation.commit.command_and_head",
        "preparation.commit.state_write",
        "preparation.runtime_artifacts",
        "preparation.rollback_snapshot",
    }
    for label in required:
        assert f'log_update_operation_timing_ms "{label}"' in source
