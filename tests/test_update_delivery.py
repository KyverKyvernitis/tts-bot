from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core
from updater.testes.fonte_discord import ler_fonte_discord


ROOT = Path(__file__).resolve().parents[1]
UPDATER = caminho_fonte_core()
BOT = ROOT / "bot.py"


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env=merged,
        check=True,
        capture_output=True,
        text=True,
    )




def test_final_status_reaches_bot_with_valid_json(tmp_path: Path) -> None:
    received: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - nome exigido por BaseHTTPRequestHandler
            size = int(self.headers.get("Content-Length") or "0")
            received.append(json.loads(self.rfile.read(size).decode("utf-8")))
            body = json.dumps({"ok": True, "delivered": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        outbox = tmp_path / "status-outbox"
        repo = tmp_path / "repo"
        repo.mkdir()
        payload = {
            "channel_id": "123",
            "message_id": "456",
            "candidate_id": "zip-direct",
            "status": "success",
            "title": "Atualização concluída",
            "description": "ok",
            "event_at": "2026-07-22T13:00:00+00:00",
        }
        harness = f"""
source <(awk '/^send_update_status_payload[(][)]/{{flag=1}} /^flush_update_status_outbox[(][)]/{{flag=0}} flag' {UPDATER!s})
prepare_update_delivery_dirs() {{ mkdir -p "$UPDATE_STATUS_OUTBOX_DIR"; }}
BOT_HEALTH_URL='http://127.0.0.1:{server.server_port}/health'
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={outbox!s}
export DISCORD_AUTO_UPDATE_DELIVERY_ATTEMPTS=1
export DISCORD_AUTO_UPDATE_DELIVERY_RETRY_DELAY_SECONDS=0
export DISCORD_AUTO_UPDATE_DELIVERY_TIMEOUT_SECONDS=2
send_update_status_payload {json.dumps(json.dumps(payload))} 1
"""
        _run_bash(harness)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert len(received) == 1
    assert received[0]["candidate_id"] == "zip-direct"
    assert received[0]["delivery_id"]
    assert not list(outbox.glob("*.json"))


def test_final_status_is_persisted_when_bot_endpoint_is_unavailable(tmp_path: Path) -> None:
    outbox = tmp_path / "status-outbox"
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = {
        "channel_id": "123",
        "message_id": "456",
        "candidate_id": "zip-test",
        "display_id": "UPD-TEST",
        "status": "success",
        "title": "✅ Atualização concluída",
        "description": "ok",
        "event_at": "2026-07-22T13:00:00+00:00",
    }
    harness = f"""
source <(awk '/^send_update_status_payload[(][)]/{{flag=1}} /^flush_update_status_outbox[(][)]/{{flag=0}} flag' {UPDATER!s})
prepare_update_delivery_dirs() {{ mkdir -p "$UPDATE_STATUS_OUTBOX_DIR"; }}
BOT_HEALTH_URL='http://127.0.0.1:9/health'
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={outbox!s}
LOG_TAG=test-updater
export DISCORD_AUTO_UPDATE_DELIVERY_ATTEMPTS=1
export DISCORD_AUTO_UPDATE_DELIVERY_RETRY_DELAY_SECONDS=0
export DISCORD_AUTO_UPDATE_DELIVERY_TIMEOUT_SECONDS=1
send_update_status_payload {json.dumps(json.dumps(payload))} 1
"""
    _run_bash(harness)
    jobs = list(outbox.glob("*.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert job["payload"]["candidate_id"] == "zip-test"
    assert job["payload"]["delivery_id"]
    assert job["attempts"] == 0
    assert job["last_error"]


def test_alert_outbox_is_owned_by_bot_and_shell_flush_only_wakes_bot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "alert.sh").write_text((ROOT / "alert.sh").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "alert.sh").chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = '-u' ]; then shift 2; fi\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    status_outbox = tmp_path / "status"
    alert_outbox = tmp_path / "alerts"
    receipts = tmp_path / "receipts"

    queue_harness = f"""
source <(awk '/^prepare_update_delivery_dirs[(][)]/{{flag=1}} /^human_duration[(][)]/{{flag=0}} flag' {UPDATER!s})
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={status_outbox!s}
UPDATE_ALERT_OUTBOX_DIR={alert_outbox!s}
UPDATE_DELIVERY_RECEIPTS_DIR={receipts!s}
LOG_TAG=test-updater
send_alert_reliably success 'Atualização concluída' 'Resumo: ok' '' '' 'UPD-TEST-final'
"""
    _run_bash(queue_harness, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    jobs = list(alert_outbox.glob("*.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert job["event_id"] == "UPD-TEST-final"
    assert job["delivery"] == "discord_bot"
    assert not (receipts / "UPD-TEST-final.alert.done").exists()

    received_paths: list[str] = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received_paths.append(self.path)
            size = int(self.headers.get("Content-Length") or "0")
            self.rfile.read(size)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        flush_harness = f"""
source <(awk '/^flush_update_alert_outbox[(][)]/{{flag=1}} /^notify_zip_status_message[(][)]/{{flag=0}} flag' {UPDATER!s})
prepare_update_delivery_dirs() {{ :; }}
BOT_HEALTH_URL='http://127.0.0.1:{server.server_port}/health'
REPO_DIR={repo!s}
flush_update_alert_outbox
"""
        _run_bash(flush_harness)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert received_paths == ["/internal/update/flush-logs"]
    assert jobs[0].is_file(), "o shell não deve consumir a fila pertencente ao bot"
    assert not (receipts / "UPD-TEST-final.alert.done").exists()

def test_discord_status_state_is_saved_only_after_successful_edit() -> None:
    source = ler_fonte_discord()
    start = source.index("    async def _edit_zip_status_from_update")
    end = source.index("\n    def _zip_update_find_candidate_sync", start)
    block = source[start:end]

    assert "incoming_event < previous_event" in block
    edit_at = block.index("await status_message.edit")
    save_at = block.index('self._zip_update_state_save({"latest": state_record})')
    clear_at = block.index("await self._zip_update_clear_previous_control", edit_at)
    assert edit_at < clear_at < save_at
    assert '"delivery_state"] = "delivered"' in block


def test_final_delivery_is_durable_before_candidate_archive() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    final_at = source.index('write_local_candidate_state "finalizing_delivery"')
    status_at = source.index('notify_zip_status_message "$ALERT_TYPE"', final_at)
    alert_at = source.index('send_alert_reliably "$ALERT_TYPE"', status_at)
    scheduled_at = source.index('write_local_candidate_state "delivery_scheduled"', alert_at)
    archive_at = source.index('archive_local_candidate "done"', scheduled_at)
    assert final_at < status_at < alert_at < scheduled_at < archive_at
    assert 'write_local_candidate_state "notified"' not in source



def test_bot_recovers_stale_raw_log_claims_after_restart() -> None:
    source = ler_fonte_discord()
    start = source.index("    def _zip_update_claim_log_jobs_sync")
    end = source.index("\n    def _zip_update_requeue_log_job_sync", start)
    block = source[start:end]
    assert 'root.glob(".sending.*.json")' in block
    assert "now - stale.stat().st_mtime < 120" in block
    assert "os.replace(stale, target)" in block
    assert 'root.glob("*.json")' in block

def test_malformed_status_job_goes_to_dead_letter_instead_of_disappearing(tmp_path: Path) -> None:
    outbox = tmp_path / "status"
    failed = outbox / "failed"
    repo = tmp_path / "repo"
    outbox.mkdir()
    repo.mkdir()
    job = outbox / "broken.json"
    job.write_text("{not-json", encoding="utf-8")

    harness = f"""
source <(awk '/^flush_update_status_outbox[(][)]/{{flag=1}} /^flush_update_alert_outbox[(][)]/{{flag=0}} flag' {UPDATER!s})
prepare_update_delivery_dirs() {{ mkdir -p "$UPDATE_STATUS_OUTBOX_DIR"; }}
BOT_HEALTH_URL='http://127.0.0.1:9/health'
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={outbox!s}
flush_update_status_outbox
"""
    _run_bash(harness)
    dead = list(failed.glob("broken.json"))
    assert len(dead) == 1
    payload = json.loads(dead[0].read_text(encoding="utf-8"))
    assert payload["attempts"] == 20
    assert "JSONDecodeError" in payload["last_error"]


def test_rollback_control_is_removed_when_persistent_state_cannot_be_saved() -> None:
    source = ler_fonte_discord()
    state_start = source.index("    def _zip_update_state_save")
    state_end = source.index("\n    def _zip_update_component_text", state_start)
    state_block = source[state_start:state_end]
    assert "-> bool" in state_block
    assert "for attempt in range(3)" in state_block
    assert "return False" in state_block

    edit_start = source.index("    async def _edit_zip_status_from_update")
    edit_end = source.index("\n    def _zip_update_find_candidate_sync", edit_start)
    edit_block = source[edit_start:edit_end]
    failure_at = edit_block.index('if not self._zip_update_state_save({"latest": state_record})')
    no_control_at = edit_block.index("view=self._make_zip_update_view(", failure_at)
    delivered_at = edit_block.index('"warning": "controle de rollback indisponível"', no_control_at)
    assert "presentation=presentation" in edit_block[no_control_at:delivered_at]
    assert failure_at < no_control_at < delivered_at


def test_bot_raw_log_attachment_is_confined_to_outbox() -> None:
    source = ler_fonte_discord()
    start = source.index("    def _zip_update_log_attachment_path")
    end = source.index("\n    async def _zip_update_flush_raw_logs_once", start)
    block = source[start:end]
    assert "candidate.relative_to(root)" in block
    assert 'raise ValueError("anexo do log fora do outbox")' in block
    flush_start = source.index("    async def _zip_update_flush_raw_logs_once")
    flush_end = source.index("\n    def _zip_update_current_head_sync", flush_start)
    flush = source[flush_start:flush_end]
    assert "self._zip_update_log_attachment_path" in flush

def test_recovery_does_not_replace_latest_rollback_control_with_old_update() -> None:
    source = ler_fonte_discord()
    reconcile_start = source.index("    async def _zip_update_reconcile_archived_messages_once")
    reconcile_end = source.index("\n    async def _zip_update_reconcile_loop", reconcile_start)
    reconcile = source[reconcile_start:reconcile_end]
    assert "applied_commit == live_head" in reconcile
    assert '"preserve_existing_control": control is None' in reconcile

    edit_start = source.index("    async def _edit_zip_status_from_update")
    edit_end = source.index("\n    def _zip_update_find_candidate_sync", edit_start)
    edit = source[edit_start:edit_end]
    assert 'clear_previous_control = bool(payload.get("clear_previous_control"))' in edit
    assert 'preserve_existing_control = bool(payload.get("preserve_existing_control"))' in edit
    assert "and clear_previous_control" in edit


def test_candidate_delivery_state_is_written_atomically(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    harness = f"""
source <(awk '/^write_local_candidate_state[(][)]/{{flag=1}} /^send_update_status_payload[(][)]/{{flag=0}} flag' {UPDATER!s})
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_DIR={candidate!s}
write_local_candidate_state delivery_scheduled abcdef123456
"""
    _run_bash(harness)
    state = json.loads((candidate / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "delivery_scheduled"
    assert state["commit"] == "abcdef123456"
    assert state["updated_at"]
    assert not list(candidate.glob(".state.json.*.tmp"))


def test_final_status_markdown_is_built_without_command_substitution(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    harness = f"""
source <(awk '/^build_final_status_description[(][)]/{{flag=1}} /^deploy_bot[(][)]/{{flag=0}} flag' {UPDATER!s})
ZIP_STATUS_DESCRIPTION=''
build_final_status_description \\
  'Atualização aplicada.' \\
  '`touch {marker!s}`' \\
  '1111111' \\
  '2222222' \\
  '1' \\
  '+10 -2' \\
  'reinício completo' \\
  '12s' \\
  'OK'
printf '%s' "$ZIP_STATUS_DESCRIPTION"
"""
    result = _run_bash(harness)
    assert not marker.exists()
    assert "Atualização ``touch" in result.stdout
    assert "1 arquivo alterado" in result.stdout
    assert "`1111111` → `2222222`" in result.stdout


def test_post_deploy_error_is_handled_before_any_rollback() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("on_error() {")
    end = source.index("\ntrap 'cleanup_runtime_artifacts' EXIT", start)
    block = source[start:end]

    committed_at = block.index("if (( DEPLOYMENT_COMMITTED == 1 ))")
    preserve_at = block.index("handle_post_deploy_failure", committed_at)
    rollback_at = block.index("rollback_after_failure", preserve_at)
    assert committed_at < preserve_at < rollback_at
    assert "git reset" not in source[source.index("handle_post_deploy_failure() {"):start]


def test_deployment_is_committed_before_final_status_formatting() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    regular_publish = source.index("publish_local_candidate_after_validation")
    committed = source.index("mark_deployment_committed", regular_publish)
    delivery = source.index("build_final_status_description", committed)
    assert regular_publish < committed < delivery
    assert 'write_local_candidate_state "deployment_completed"' in source


def test_resume_after_published_candidate_does_not_restart_bot() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert "LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY=1" in source
    branch_start = source.rindex("if (( LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY == 1 )); then")
    branch_end = source.index("\nelse", branch_start)
    branch = source[branch_start:branch_end]
    assert "mark_deployment_committed" in branch
    assert "refresh_bot_health_status" in branch
    assert "deploy_bot" not in branch
    assert "systemctl restart" not in branch


def test_bot_restart_budget_allows_only_one_attempt_per_phase(tmp_path: Path) -> None:
    calls = tmp_path / "systemctl-calls"
    harness = f"""
source <(awk '/^restart_bot_service_once[(][)]/{{flag=1}} /^build_final_status_description[(][)]/{{flag=0}} flag' {UPDATER!s})
ROLLBACK_IN_PROGRESS=0
BOT_RESTARTS_DEPLOY=0
BOT_RESTARTS_ROLLBACK=0
SERVICE=tts-bot
LOG_TAG=test
LAST_ERROR_STDERR=''
systemctl() {{ printf '%s\\n' "$*" >> {calls!s}; return 0; }}
logger() {{ :; }}
restart_bot_service_once
set +e
restart_bot_service_once
rc=$?
set -e
printf 'RC=%s DEPLOY=%s ROLLBACK=%s\\n' "$rc" "$BOT_RESTARTS_DEPLOY" "$BOT_RESTARTS_ROLLBACK"
"""
    result = _run_bash(harness)
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert lines.count("restart tts-bot") == 1
    assert "RC=75 DEPLOY=1 ROLLBACK=0" in result.stdout


def test_reconciler_skips_active_updater_and_never_confirms_mismatched_head() -> None:
    source = ler_fonte_discord()
    assert "DISCORD_AUTO_UPDATE_RECONCILE_MAX_AGE_SECONDS\", \"1800" in source
    reconcile_start = source.index("    async def _zip_update_reconcile_archived_messages_once")
    reconcile_end = source.index("\n    async def _zip_update_reconcile_loop", reconcile_start)
    block = source[reconcile_start:reconcile_end]
    assert "_zip_update_updater_active_sync" in block
    assert "applied_commit == live_head" in block
    assert "if not current_matches" in block
    assert "Estado da atualização divergente" in block
    assert 'status = "warn"' in block


def test_systemd_installer_preserves_disabled_updater_timer_during_update() -> None:
    installer = (ROOT / "updater" / "sistema" / "instalar.sh").read_text(encoding="utf-8")
    assert "capture_updater_timer_state" in installer
    assert '"$FROM_UPDATER" == "1" && "$UPDATER_TIMER_WAS_ENABLED" != "1"' in installer
    assert 'action "tts-bot-updater.timer/path permaneceram desativados"' in installer


def test_post_deploy_failure_path_preserves_code_and_archives_candidate(tmp_path: Path) -> None:
    trace = tmp_path / "trace.log"
    source = UPDATER.read_text(encoding="utf-8")
    block_start = source.index("handle_post_deploy_failure() {")
    block_end = source.index("\ntrap 'cleanup_runtime_artifacts' EXIT", block_start)
    functions = source[block_start:block_end]
    harness = functions + f"""
DEPLOYMENT_COMMITTED=1
MANUAL_FAILURE_ALERT_SENT=0
UPDATE_APPLIED=1
PREVIOUS_COMMIT=1111111111111111111111111111111111111111
CURRENT_COMMIT="$PREVIOUS_COMMIT"
REMOTE_COMMIT=2222222222222222222222222222222222222222
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_DISPLAY_ID=UPD-TEST
ROLLBACK_CONTROL_MODE=0
BRANCH=main
STAGE='mensagem final'
LOG_TAG=test
LAST_ERROR_STDERR='erro visual'
BOT_RESTARTS_DEPLOY=1
BOT_RESTARTS_ROLLBACK=0
ROLLBACK_STATUS='não foi necessário'
UPDATER_UNIT='tts-bot-updater.service'
HOSTNAME=test-host
short_commit() {{ printf '%s' "${{1:0:7}}"; }}
register_error_context() {{ :; }}
write_local_candidate_state() {{ printf 'STATE:%s\n' "$1" >> {trace!s}; }}
notify_zip_status_message() {{ printf 'STATUS:%s:%s\n' "$1" "$2" >> {trace!s}; }}
send_alert_reliably() {{ printf 'ALERT:%s:%s\n' "$1" "$2" >> {trace!s}; }}
flush_update_status_outbox() {{ :; }}
flush_update_alert_outbox() {{ :; }}
archive_local_candidate() {{ printf 'ARCHIVE:%s\n' "$1" >> {trace!s}; }}
trigger_updater_if_queue_pending() {{ :; }}
logger() {{ :; }}
rollback_after_failure() {{ printf 'ROLLBACK\n' >> {trace!s}; exit 91; }}
cleanup_local_candidate_new_files_after_reset() {{ :; }}
update_local_candidate_heartbeat() {{ :; }}
collect_local_tracked_changes() {{ :; }}
send_error() {{ :; }}
false
on_error 999 main
"""
    result = subprocess.run(
        ["bash", "-u", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = trace.read_text(encoding="utf-8").splitlines()
    assert "STATE:delivery_degraded" in lines
    assert "ARCHIVE:done" in lines
    assert any(line.startswith("STATUS:success:") for line in lines)
    assert "ROLLBACK" not in lines


def test_raw_log_receipt_is_written_only_after_bot_sends_to_discord() -> None:
    source = ler_fonte_discord()
    flush_start = source.index("    async def _zip_update_flush_raw_logs_once")
    flush_end = source.index("\n    def _zip_update_current_head_sync", flush_start)
    flush = source[flush_start:flush_end]
    send_at = flush.index("await channel.send")
    receipt_at = flush.index("_zip_update_alert_receipt_save_sync", send_at)
    unlink_at = flush.index("claim.unlink", receipt_at)
    assert send_at < receipt_at < unlink_at

    reconcile_start = source.index("    async def _zip_update_reconcile_archived_messages_once")
    reconcile_end = source.index("\n    async def _zip_update_reconcile_loop", reconcile_start)
    reconcile = source[reconcile_start:reconcile_end]
    assert "if alert_receipt.is_file()" in reconcile
    assert 'receipt["log_delivered"] = True' in reconcile
    assert "_zip_update_flush_raw_logs_once" in source

def test_updater_timer_waits_until_previous_run_is_inactive() -> None:
    path = ROOT / "updater" / "sistema" / "tts-bot-updater.timer"
    text = path.read_text(encoding="utf-8")
    assert "OnUnitInactiveSec=1min" in text
    assert "OnUnitActiveSec=" not in text
    assert "Persistent=false" in text


def test_systemd_installer_change_does_not_restart_unrelated_subsystems() -> None:
    harness = f"""
source <(awk '/^classify_changed_files[(][)]/{{flag=1}} /^fast_reload_modules_for_changed_files[(][)]/{{flag=0}} flag' {UPDATER!s})
CHANGED_FILES_RAW='updater/sistema/instalar.sh'
classify_changed_files
printf '%s %s %s %s %s\n' \
  "$VPS_SYSTEMD_UNITS_CHANGED" \
  "$AUDIO_SYSTEMD_CHANGED" \
  "$CLEANUP_CHANGED" \
  "$PHONE_LAVALINK_WATCH_CHANGED" \
  "$PHONE_WORKER_WATCH_CHANGED"
"""
    result = _run_bash(harness)
    assert result.stdout.strip() == "1 0 0 0 0"


def test_terminal_refuses_self_stop_without_spawning_a_shutdown_process() -> None:
    source = (ROOT / "cogs" / "terminal_cmd.py").read_text(encoding="utf-8")
    start = source.index("    async def _refuse_primary_bot_stop")
    end = source.index("\n    @commands.command", start)
    block = source[start:end]
    assert "Parar o bot pelo Discord está desativado" in block
    assert "create_subprocess_shell" not in block
    assert "systemctl stop" not in block


def test_game_bot_filter_uses_member_metadata_instead_of_decoding_tokens() -> None:
    source = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
    assert "def _is_bot_member" in source
    assert 'getattr(member, "bot", False)' in source
    assert "urlsafe_b64decode" not in source


def test_installer_dynamically_keeps_disabled_updater_timer_disabled(tmp_path: Path) -> None:
    installer = ROOT / "updater" / "sistema" / "instalar.sh"
    calls = tmp_path / "systemctl.log"
    harness = f"""
source <(awk '/^capture_updater_timer_state[(][)]/{{flag=1}} /^write_status[(][)]/{{flag=0}} flag' {installer!s})
DRY_RUN=0
FROM_UPDATER=1
UPDATER_TIMER_WAS_ENABLED=0
UPDATER_TIMER_WAS_ACTIVE=0
ACTIONS=()
action() {{ :; }}
truthy_env() {{ return 1; }}
systemctl() {{
  if [[ "${{1:-}}" == "is-enabled" || "${{1:-}}" == "is-active" ]]; then
    return 1
  fi
  printf '%s\n' "$*" >> {calls!s}
  return 0
}}
capture_updater_timer_state
apply_service_policy
"""
    _run_bash(harness)
    logged = calls.read_text(encoding="utf-8").splitlines()
    assert "disable --now tts-bot-updater.timer tts-bot-updater.path" in logged
    assert "enable tts-bot-updater.timer" not in logged
    assert "enable --now tts-bot-updater.path" not in logged
    assert "start tts-bot-updater.timer" not in logged


def _run_candidate_suspicion_check(tmp_path: Path, changed_files: list[str]) -> str:
    candidate = tmp_path / f"candidate-{abs(hash(tuple(changed_files)))}"
    candidate.mkdir()
    (candidate / "manifest.json").write_text(
        json.dumps({"zip_name": "patch.zip", "changed_files": changed_files}),
        encoding="utf-8",
    )
    harness = f"""
source <(awk '/^local_candidate_suspicion_reason[(][)]/{{flag=1}} /^reject_local_candidate_safely[(][)]/{{flag=0}} flag' {UPDATER!s})
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_DIR={candidate!s}
DISCORD_AUTO_UPDATE_ALLOW_FULL_REPO_ZIP=0
local_candidate_suspicion_reason
"""
    return _run_bash(harness).stdout.strip()


def test_candidate_suspicion_allows_only_safe_env_templates(tmp_path: Path) -> None:
    assert _run_candidate_suspicion_check(
        tmp_path,
        ["dashboard/backend/.env.example"],
    ) == ""

    blocked = _run_candidate_suspicion_check(
        tmp_path,
        ["dashboard/backend/.env.production"],
    )
    assert "caminho protegido" in blocked


def test_candidate_suspicion_rejects_legacy_directory_with_trailing_space(tmp_path: Path) -> None:
    blocked = _run_candidate_suspicion_check(
        tmp_path,
        ["activity /sinuca/index.html"],
    )
    assert "caminho suspeito ou inválido" in blocked


def test_first_candidate_keeps_preparation_microsteps_instead_of_fake_queue() -> None:
    source = ler_fonte_discord()
    start = source.index("                    queue_position = max(1")
    end = source.index("                    await self._dispatch_updater_candidate(candidate_id, display_id)", start)
    block = source[start:end]

    first_at = block.index("if queue_position <= 1:")
    direct_at = block.index("**Iniciando atualização**", first_at)
    queue_at = block.index('"📦 Atualização na fila"', direct_at)
    assert first_at < direct_at < queue_at
    first_block = block[first_at:queue_at]
    assert '"kind": "progress"' in first_block
    assert '"stage": "Iniciando atualização"' in first_block
    assert '"macro_index": 0' in first_block

def test_queue_refresher_preserves_new_first_candidate_animation() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("refresh_pending_queue_messages() {")
    end = source.index("\nprune_update_artifacts() {", start)
    block = source[start:end]

    assert "position_at_enqueue" in block
    assert "if position == 1 and active_count == 0 and position_at_enqueue <= 1:" in block
    assert "continue" in block
    assert "⚙️ Iniciando atualização" in block
    assert "📦 Atualização na fila" in block


def test_update_presence_and_short_user_notice_are_connected_to_runtime_state() -> None:
    bot_source = ler_fonte_discord()
    presence_source = (ROOT / "utility" / "application_presence.py").read_text(encoding="utf-8")

    assert '"candidates" / "runtime-state.json"' in bot_source
    assert '"⚠️ Atualização em andamento. A resposta pode demorar."' in bot_source
    assert "UPDATE_NOTICE_COOLDOWN_SECONDS" in bot_source
    assert 'await self._apply_presence("Atualizando", discord.Status.idle' in presence_source
    assert "self._build_custom_activity(text)" in presence_source
    assert "discord.Status.idle" in presence_source
    assert "heartbeat_epoch" in presence_source
    assert "APPLICATION_PRESENCE_UPDATE_STALE_SECONDS" in presence_source


def test_updater_service_has_lower_cpu_and_io_priority() -> None:
    path = ROOT / "updater" / "sistema" / "tts-bot-updater.service"
    text = path.read_text(encoding="utf-8")
    assert "Nice=10" in text
    assert "CPUWeight=20" in text
    assert "IOWeight=20" in text


def test_bot_persists_preparation_progress_before_enqueuing_candidate() -> None:
    source = ler_fonte_discord()
    writer_start = source.index("    def _write_local_update_candidate_sync(")
    writer_end = source.index("    def _trigger_updater_service_sync", writer_start)
    writer = source[writer_start:writer_end]
    process_start = source.index("    def _process_zip_update_sync(")
    process_end = source.index("    def handle_internal_update_action", process_start)
    process = source[process_start:process_end]

    assert '"progress_handoff": {' in writer
    assert '"completed_steps": handoff_steps' in writer
    assert '"preparation_total_ms": preparation_total_ms' in writer
    assert writer.index('"progress_handoff": {') < writer.index("os.replace(manifest_tmp")
    assert writer.index("os.replace(manifest_tmp") < writer.index("os.replace(tmp_pending")
    assert "preparation_steps=list(preparation_steps)" in process
    assert 'publish_progress("Finalizando preparação", "Candidato seguro preparado", elapsed)' in process


def test_local_candidate_load_hydrates_progress_before_first_updater_stage() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    load_start = source.index("load_pending_local_candidate() {")
    load_end = source.index("\nverify_local_candidate_integrity() {", load_start)
    load_block = source[load_start:load_end]
    prepare_start = source.index("prepare_local_candidate_update() {")
    prepare_end = source.index("\npublish_local_candidate_after_validation() {", prepare_start)
    prepare_block = source[prepare_start:prepare_end]

    assert "zip_progress_hydrate_from_candidate || true" in load_block
    assert 'zip_progress_publish "Conferindo ZIP"' in prepare_block


def test_rollback_preserves_original_failure_diagnostics() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("rollback_after_failure() {")
    end = source.index("\nhandle_post_deploy_failure() {", start)
    block = source[start:end]

    code_capture_at = block.index('local original_error_code="$LAST_ERROR_CODE"')
    capture_at = block.index('local original_error_stderr="$LAST_ERROR_STDERR"', code_capture_at)
    rollback_front_at = block.index('restore_frontend_runtime_release "$PREVIOUS_COMMIT"', capture_at)
    rollback_back_at = block.index('restore_backend_runtime_release "$PREVIOUS_COMMIT"', rollback_front_at)
    restore_at = block.index('LAST_ERROR_STDERR="$original_error_stderr"', rollback_back_at)
    body_at = block.index('body="Resumo:', restore_at)

    assert code_capture_at < capture_at < rollback_front_at < rollback_back_at < restore_at < body_at
    assert "if deploy_frontend" not in block
    assert "if deploy_backend" not in block
    assert 'local original_error_logs="$LAST_ERROR_LOGS"' in block
    assert 'LAST_ERROR_CODE="$original_error_code"' in block
    assert 'LAST_ERROR_LOGS="$original_error_logs"' in block
    assert 'LAST_ERROR_SERVICE_UNIT="$original_error_service_unit"' in block
