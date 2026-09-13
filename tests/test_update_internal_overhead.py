from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"
SNAPSHOT = ROOT / "utility" / "update_git_snapshot.py"


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
