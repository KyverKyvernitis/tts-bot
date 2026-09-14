from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core
from updater.testes.fonte_discord import ler_fonte_discord


ROOT = Path(__file__).resolve().parents[1]
UPDATER = caminho_fonte_core()
BOT = ROOT / "bot.py"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    (repo / "cogs").mkdir(parents=True)
    (repo / "cogs" / "obsolete.py").write_text("OLD = 1\n", encoding="utf-8")
    (repo / "cogs" / "before.py").write_text("MOVE = 1\n", encoding="utf-8")
    (repo / "cogs" / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Updater Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)


def test_schema_v3_applies_delete_move_add_update_and_stages_exact_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    files = candidate / "files" / "cogs"
    _init_repo(repo)
    files.mkdir(parents=True)
    (files / "keep.py").write_text("VALUE = 2\n", encoding="utf-8")
    (files / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    operations = [
        {"op": "delete", "path": "cogs/obsolete.py"},
        {"op": "move", "from": "cogs/before.py", "to": "cogs/after.py"},
        {"op": "update", "path": "cogs/keep.py"},
        {"op": "add", "path": "cogs/new.py"},
    ]
    changed = ["cogs/obsolete.py", "cogs/before.py", "cogs/after.py", "cogs/keep.py", "cogs/new.py"]
    (candidate / "manifest.json").write_text(
        json.dumps({"schema_version": 3, "changed_files": changed, "operations": operations}),
        encoding="utf-8",
    )

    harness = f"""
source <(awk '/^refresh_changed_files_from_staged_diff[(][)]/{{flag=1}} /^apply_local_candidate_patch_diff[(][)]/{{flag=0}} flag' {UPDATER!s})
source <(awk '/^apply_local_candidate_operations[(][)]/{{flag=1}} /^copy_local_candidate_files[(][)]/{{flag=0}} flag' {UPDATER!s})
source <(awk '/^copy_local_candidate_files[(][)]/{{flag=1}} /^normalize_changed_file_permissions[(][)]/{{flag=0}} flag' {UPDATER!s})
source <(awk '/^git_add_changed_files[(][)]/{{flag=1}} /^prepare_local_candidate_update[(][)]/{{flag=0}} flag' {UPDATER!s})
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
REPO_DIR={repo!s}
LOCAL_CANDIDATE_SCHEMA_VERSION=3
LOCAL_CANDIDATE_DIR={candidate!s}
LOCAL_CANDIDATE_FILES_DIR={candidate / 'files'!s}
CHANGED_FILES_RAW=$'cogs/obsolete.py\\ncogs/before.py\\ncogs/after.py\\ncogs/keep.py\\ncogs/new.py'
CHANGED_STATUS_RAW=''
CHANGED_DIFF_NUMSTAT_RAW=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
LOG_TAG='test-updater'
repo_git() {{ git -C "$REPO_DIR" "$@"; }}
candidate_repo_dir() {{ printf '%s\n' "$REPO_DIR"; }}
candidate_git() {{ git -C "$(candidate_repo_dir)" "$@"; }}
apply_local_candidate_operations
# Retomar o mesmo candidato deve ser idempotente para delete/move já staged.
apply_local_candidate_operations
copy_local_candidate_files
git_add_changed_files
refresh_changed_files_from_staged_diff
printf '%s\\n' "$CHANGED_STATUS_RAW"
printf '%s\\n' '---FILES---'
printf '%s\\n' "$CHANGED_FILES_RAW"
"""
    result = _run_bash(harness)
    output = result.stdout
    assert "D\tcogs/obsolete.py" in output
    assert "D\tcogs/before.py" in output
    assert "A\tcogs/after.py" in output
    assert "M\tcogs/keep.py" in output
    assert "A\tcogs/new.py" in output
    assert not (repo / "cogs" / "obsolete.py").exists()
    assert not (repo / "cogs" / "before.py").exists()
    assert (repo / "cogs" / "after.py").read_text(encoding="utf-8") == "MOVE = 1\n"
    assert (repo / "cogs" / "keep.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_new_candidates_do_not_trigger_filename_magic_migration() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    prepare = source[source.index("prepare_local_candidate_update() {") : source.index("publish_local_candidate_after_validation() {")]
    assert '(( LOCAL_CANDIDATE_SCHEMA_VERSION < 3 )) && [[ -f "$apply_repo/scripts/migrate-dashboard-layout.sh" ]]' in prepare
    assert 'REPO_DIR="$apply_repo" bash "$apply_repo/scripts/migrate-dashboard-layout.sh"' in prepare
    assert "apply_local_candidate_operations" in prepare


def test_bot_emits_schema_v3_and_reserves_control_manifest() -> None:
    source = ler_fonte_discord()
    assert 'UPDATE_CONTROL_MANIFEST_NAME' in source
    assert 'allowed_ops={"delete", "move"}' in source
    assert '"schema_version": 3' in source
    assert '"operations": normalized_operations' in source
    assert '["git", "add", "-A", "--", *changed_files]' in source
    assert '"--numstat", "--no-renames"' in source


def test_resuming_active_candidate_does_not_burn_attempt() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    load = source[source.index("load_pending_local_candidate() {") : source.index("verify_local_candidate_integrity() {")]
    assert "resuming_active=1" in load
    assert "resuming = os.environ.get('RESUMING_ACTIVE') == '1'" in load
    assert "attempt = max(1, previous_attempt) if resuming else previous_attempt + 1" in load
    assert "'resume_count': resume_count" in load


def test_schema_v3_recovers_partial_filesystem_delete_and_move(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    _init_repo(repo)
    candidate.mkdir(parents=True)
    # Simula interrupção entre mutação do worktree e stage.
    (repo / "cogs" / "obsolete.py").unlink()
    (repo / "cogs" / "before.py").rename(repo / "cogs" / "after.py")
    operations = [
        {"op": "delete", "path": "cogs/obsolete.py"},
        {"op": "move", "from": "cogs/before.py", "to": "cogs/after.py"},
    ]
    (candidate / "manifest.json").write_text(
        json.dumps({"schema_version": 3, "operations": operations}),
        encoding="utf-8",
    )
    harness = f"""
source <(awk '/^apply_local_candidate_operations[(][)]/{{flag=1}} /^copy_local_candidate_files[(][)]/{{flag=0}} flag' {UPDATER!s})
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
REPO_DIR={repo!s}
LOCAL_CANDIDATE_SCHEMA_VERSION=3
LOCAL_CANDIDATE_DIR={candidate!s}
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
repo_git() {{ git -C "$REPO_DIR" "$@"; }}
candidate_repo_dir() {{ printf '%s\n' "$REPO_DIR"; }}
candidate_git() {{ git -C "$(candidate_repo_dir)" "$@"; }}
apply_local_candidate_operations
git -C "$REPO_DIR" diff --cached --name-status --no-renames
"""
    result = _run_bash(harness)
    assert "D\tcogs/obsolete.py" in result.stdout
    assert "D\tcogs/before.py" in result.stdout
    assert "A\tcogs/after.py" in result.stdout
