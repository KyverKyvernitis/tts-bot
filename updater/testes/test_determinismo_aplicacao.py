from __future__ import annotations

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
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )


def test_repo_git_is_the_single_checkout_git_gateway() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    helper = source[source.index("repo_git() {") : source.index("repo_python_as_ubuntu() {")]
    assert 'sudo -u ubuntu -H git -C "$REPO_DIR" "$@"' in helper

    # Outside the helper, checkout Git must go through repo_git. This prevents
    # root-owned index/object files and dubious-ownership failures.
    without_helper = source.replace(helper, "")
    assert "sudo -u ubuntu -H git " not in without_helper
    assert "sudo -u ubuntu -H git\n" not in without_helper


def test_candidate_copy_and_legacy_migration_run_as_ubuntu() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    copy_fn = source[source.index("copy_local_candidate_files() {") : source.index("normalize_changed_file_permissions() {")]
    assert 'sudo -u ubuntu -H env MANIFEST_PATH=' in copy_fn
    assert 'FILES_DIR="$LOCAL_CANDIDATE_FILES_DIR" python3 -' in copy_fn

    prepare_fn = source[source.index("prepare_local_candidate_update() {") : source.index("publish_local_candidate_after_validation() {")]
    assert 'apply_repo="$(candidate_repo_dir)"' in prepare_fn
    assert 'sudo -u ubuntu -H env REPO_DIR="$apply_repo" bash "$apply_repo/scripts/migrate-dashboard-layout.sh" --apply --stage' in prepare_fn


def test_staged_diff_is_authoritative_and_keeps_deleted_python(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "cogs").mkdir(parents=True)
    (repo / "cogs" / "obsolete.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Updater Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    (repo / "cogs" / "obsolete.py").unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

    harness = f"""
source <(awk '/^refresh_changed_files_from_staged_diff[(][)]/{{flag=1}} /^apply_local_candidate_patch_diff[(][)]/{{flag=0}} flag' {UPDATER!s})
source <(awk '/^classify_changed_files[(][)]/{{flag=1}} /^fast_reload_modules_for_changed_files[(][)]/{{flag=0}} flag' {UPDATER!s})
REPO_DIR={repo!s}
repo_git() {{ git -C "$REPO_DIR" "$@"; }}
candidate_git() {{ git -C "$REPO_DIR" "$@"; }}
LAST_ERROR_STDERR=''
CHANGED_STATUS_RAW=''
CHANGED_FILES_RAW='manifest-was-wrong.txt'
CHANGED_DIFF_NUMSTAT_RAW=''
refresh_changed_files_from_staged_diff
classify_changed_files
printf 'STATUS=%s\n' "$CHANGED_STATUS_RAW"
printf 'FILES=%s\n' "$CHANGED_FILES_RAW"
printf 'BOT=%s\n' "$BOT_CHANGED"
printf 'COMMANDS=%s\n' "$APP_COMMANDS_MAY_HAVE_CHANGED"
"""
    result = _run_bash(harness)
    assert "STATUS=D\tcogs/obsolete.py" in result.stdout
    assert "FILES=cogs/obsolete.py" in result.stdout
    assert "BOT=1" in result.stdout
    assert "COMMANDS=1" in result.stdout
    assert "manifest-was-wrong" not in result.stdout


def test_preflight_reports_deleted_python_and_cog_instead_of_no_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "cogs").mkdir(parents=True)

    harness = f"""
source <(awk '/^run_preflight_checks[(][)]/{{flag=1}} /^verify_bot_after_restart[(][)]/{{flag=0}} flag' {UPDATER!s})
logger() {{ :; }}
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
REPO_DIR={repo!s}
CHANGED_FILES_RAW='cogs/obsolete.py'
CHANGED_STATUS_RAW=$'D\tcogs/obsolete.py'
PREFLIGHT_PY_STATUS=''
PREFLIGHT_BASH_STATUS=''
PREFLIGHT_COG_IMPORT_STATUS=''
UPDATE_HAS_WARNINGS=0
LOG_TAG='test-updater'
run_preflight_checks
printf 'PY=%s\n' "$PREFLIGHT_PY_STATUS"
printf 'COGS=%s\n' "$PREFLIGHT_COG_IMPORT_STATUS"
"""
    result = _run_bash(harness)
    assert "1 deleção(ões) Python reconhecida(s) no diff" in result.stdout
    assert "1 deleção(ões) de cog reconhecida(s) no diff" in result.stdout
    assert "sem arquivos Python alterados" not in result.stdout
    assert "sem cogs Python alteradas" not in result.stdout


def test_permission_preflight_happens_before_candidate_application() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    prepare_fn = source[source.index("prepare_local_candidate_update() {") : source.index("publish_local_candidate_after_validation() {")]
    preflight_at = prepare_fn.index("preflight_local_candidate_permissions")
    apply_at = prepare_fn.index('STAGE="aplicação isolada do candidato"')
    assert preflight_at < apply_at
    assert 'LAST_ERROR_CODE="CANDIDATE_PERMISSION_DENIED"' in source


def test_candidate_permission_preflight_validates_destination_before_apply(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    files = candidate / "files"
    (repo / ".git").mkdir(parents=True)
    (repo / "cogs").mkdir()
    (files / "cogs").mkdir(parents=True)
    (files / "cogs" / "safe.py").write_text("VALUE = 2\n", encoding="utf-8")
    manifest = candidate / "manifest.json"
    manifest.write_text('{"changed_files":["cogs/safe.py"]}\n', encoding="utf-8")

    common = f"""
source <(awk '/^preflight_local_candidate_permissions[(][)]/{{flag=1}} /^refresh_changed_files_from_staged_diff[(][)]/{{flag=0}} flag' {UPDATER!s})
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_DIR={candidate!s}
LOCAL_CANDIDATE_FILES_DIR={files!s}
REPO_DIR={repo!s}
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
LAST_ERROR_COMMAND=''
"""
    result = _run_bash(common + "preflight_local_candidate_permissions\nprintf 'PASS=1\\n'\n")
    assert "PASS=1" in result.stdout

    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    (repo / "cogs" / "safe.py").symlink_to(outside)
    bad = _run_bash(
        common
        + "set +e\npreflight_local_candidate_permissions\nrc=$?\nset -e\n"
        + "printf 'RC=%s\\n' \"$rc\"\nprintf 'ERR=%s\\n' \"$LAST_ERROR_STDERR\"\n"
    )
    assert "RC=1" in bad.stdout
    assert "resolve para fora do repositório" in bad.stdout
