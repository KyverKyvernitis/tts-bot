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
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )


def test_smart_error_excerpt_preserves_typescript_error_before_long_tail(tmp_path: Path) -> None:
    log = tmp_path / "frontend-build.log"
    lines = [f"PASS test {index}" for index in range(80)]
    lines += [
        "src/components/message-editor/useMessageEditorDialogEnvironment.ts:58:20 - error TS2540: Cannot assign to 'current' because it is a read-only property.",
        "58 ref.current = value",
    ]
    lines += [f"post-error noise {index}" for index in range(120)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    harness = f"""
source <(awk '/^collect_smart_error_excerpt[(][)]/{{flag=1}} /^collect_run_log_excerpt[(][)]/{{flag=0}} flag' {UPDATER!s})
collect_smart_error_excerpt {log!s}
"""
    result = _run_bash(harness)

    assert "error TS2540" in result.stdout
    assert "Cannot assign to 'current'" in result.stdout
    assert "--- final do log ---" in result.stdout
    assert "post-error noise 119" in result.stdout


def test_failure_code_classifies_frontend_and_candidate_failures() -> None:
    harness = f"""
source <(awk '/^classify_failure_code[(][)]/{{flag=1}} /^persist_primary_failure[(][)]/{{flag=0}} flag' {UPDATER!s})
printf 'ts=%s\n' "$(classify_failure_code 'build do frontend' 'npm run build' 'error TS2540: readonly')"
printf 'npm=%s\n' "$(classify_failure_code 'dependências do frontend' 'npm ci' 'npm ERR! EUSAGE')"
printf 'perm=%s\n' "$(classify_failure_code 'aplicação local do candidato' 'git add' 'Permission denied')"
printf 'dirty=%s\n' "$(classify_failure_code 'verificação pós-build do repositório' 'return 1' '')"
"""
    result = _run_bash(harness)

    assert "ts=FRONTEND_TSC_FAILED" in result.stdout
    assert "npm=FRONTEND_NPM_CI_FAILED" in result.stdout
    assert "perm=CANDIDATE_PERMISSION_DENIED" in result.stdout
    assert "dirty=DIRTY_WORKTREE_AFTER_STAGE" in result.stdout


def test_primary_failure_is_write_once_and_rollback_failure_is_separate(tmp_path: Path) -> None:
    incident_root = tmp_path / "updates"
    run_log = tmp_path / "runtime.log"
    stage_log = tmp_path / "frontend-build.log"
    run_log.write_text("runtime output\n", encoding="utf-8")
    stage_log.write_text("error TS2540: original failure\n", encoding="utf-8")

    harness = f"""
source <(awk '/^sanitize_update_component[(][)]/{{flag=1}} /^service_unit_for_stage[(][)]/{{flag=0}} flag' {UPDATER!s})
logger() {{ :; }}
LOCAL_CANDIDATE_DISPLAY_ID='UPD-TEST1234'
ROLLBACK_REQUEST_ID=''
REMOTE_COMMIT=''
CURRENT_COMMIT='base123'
BRANCH='main'
UPDATE_RUNTIME_RUN_ID='20260912-180000-123-1'
UPDATE_INCIDENT_ROOT={incident_root!s}
UPDATE_INCIDENT_DIR=''
UPDATE_FAILURE_FILE=''
UPDATE_ROLLBACK_FAILURE_FILE=''
UPDATE_PRIMARY_FAILURE_WRITTEN=0
RUN_LOG_FILE={run_log!s}
CURRENT_STAGE_LOG_FILE={stage_log!s}
CURRENT_STAGE_LOG_STAGE='build do frontend'
STAGE='build do frontend'
FAILED_STAGE='build do frontend'
LAST_ERROR_CODE='FRONTEND_TSC_FAILED'
LAST_ERROR_COMMAND='npm run build'
LAST_ERROR_EXIT_CODE=2
LAST_ERROR_SERVICE_UNIT='bot-updater.service'
LAST_ERROR_STDERR='error TS2540: original failure'
LAST_ERROR_LOGS='journal original'
UPDATER_UNIT='bot-updater.service'
LOG_TAG='test-updater'
persist_primary_failure 58 deploy_frontend
printf 'FAILURE=%s\n' "$UPDATE_FAILURE_FILE"
# Simula erro secundário posterior; a evidência primária não pode ser regravada.
UPDATE_PRIMARY_FAILURE_WRITTEN=0
LAST_ERROR_CODE='FRONTEND_BUILD_FAILED'
LAST_ERROR_COMMAND='secondary command'
LAST_ERROR_STDERR='secondary failure'
persist_primary_failure 999 rollback_after_failure
printf 'ROLLBACK=%s\n' "$UPDATE_ROLLBACK_FAILURE_FILE"
persist_rollback_failure 'git ok' 'frontend rollback failed' 'backend ok' 'bot ok' 'health ok' {tmp_path / 'rollback.log'!s}
"""
    result = _run_bash(harness)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    failure_path = Path(values["FAILURE"])
    rollback_path = Path(values["ROLLBACK"])
    primary = json.loads(failure_path.read_text(encoding="utf-8"))
    rollback = json.loads(rollback_path.read_text(encoding="utf-8"))

    assert primary["kind"] == "primary_failure"
    assert primary["failure_code"] == "FRONTEND_TSC_FAILED"
    assert primary["command"] == "npm run build"
    assert primary["line"] == "58"
    assert "original failure" in primary["stderr_excerpt"]
    assert "secondary failure" not in failure_path.read_text(encoding="utf-8")
    assert primary["stdout_path"] == str(stage_log)
    assert primary["stream_mode"] == "merged"

    assert rollback["kind"] == "rollback_failure"
    assert rollback["primary_failure_path"] == str(failure_path)
    assert rollback["frontend"] == "frontend rollback failed"
    assert rollback_path != failure_path


def test_updater_persists_primary_failure_before_rollback() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    on_error_at = source.index("on_error() {")
    persist_at = source.index('persist_primary_failure "$failed_line" "$failed_function"', on_error_at)
    rollback_at = source.index('rollback_after_failure "$exit_code" "$failed_command"', on_error_at)
    assert persist_at < rollback_at
    rollback_fn = source[source.index("rollback_after_failure() {") : on_error_at]
    assert 'register_error_context "$exit_code" "$failed_command"' not in rollback_fn


def test_progress_command_keeps_stage_log_and_original_exit_code(tmp_path: Path) -> None:
    stage_log = tmp_path / "frontend-test.log"
    harness = f"""
source <(awk '/^zip_progress_run_as_ubuntu[(][)]/{{flag=1}} /^zip_progress_only_site_changed[(][)]/{{flag=0}} flag' {UPDATER!s})
zip_progress_publish() {{ :; }}
zip_progress_heartbeat_seconds() {{ printf '5'; }}
update_now_ms() {{ printf '1000'; }}
format_update_duration_ms() {{ printf '1s'; }}
stage_log_file_for() {{ printf '%s' {stage_log!s}; }}
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
STAGE='testes do frontend'
CURRENT_STAGE_LOG_FILE=''
CURRENT_STAGE_LOG_STAGE=''
set +e
zip_progress_run_as_ubuntu 'Validando interface' 'Executando testes' 'printf "needle-stage-log\\n"; exit 7'
rc=$?
set -e
printf 'RC=%s\\n' "$rc"
printf 'CURRENT=%s\\n' "$CURRENT_STAGE_LOG_FILE"
"""
    result = _run_bash(harness)

    assert "RC=7" in result.stdout
    assert f"CURRENT={stage_log}" in result.stdout
    assert stage_log.read_text(encoding="utf-8") == "needle-stage-log\n"


def test_rollback_persists_recovery_outcome_and_archives_local_candidate() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("rollback_after_failure() {")
    end = source.index("\nhandle_post_deploy_failure() {", start)
    block = source[start:end]

    state_at = block.index("write_local_candidate_recovery_state")
    notify_at = block.index('notify_zip_status_message "error"', state_at)
    log_at = block.index('send_error "$title" "$body"', notify_at)
    archive_at = block.index('archive_local_candidate "failed"', log_at)
    exit_at = block.index('exit "$exit_code"', archive_at)

    assert state_at < notify_at < log_at < archive_at < exit_at
    assert '"$rollback_bool" "$head_after_reset" "$REMOTE_COMMIT"' in block
    assert '"$recovery_duration" "$rollback_bot_status"' in block


def test_recovery_state_keeps_fields_needed_by_post_restart_reconciliation() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("write_local_candidate_recovery_state() {")
    end = source.index("\nsend_update_status_payload() {", start)
    block = source[start:end]

    for field in (
        '"rollback_ok": rollback_ok',
        '"recovery_state": "restored" if rollback_ok else "incomplete"',
        '"failure_code":',
        '"failed_stage":',
        '"recovery_duration":',
        '"bot_health":',
        '"target_commit":',
    ):
        assert field in block
    assert "os.replace(tmp, path)" in block
