from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"
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


def test_alert_outbox_replays_once_and_writes_receipt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_sudo.chmod(0o755)
    fake_alert = repo / "alert.sh"
    fake_alert.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$REPLAY_LOG\"\n", encoding="utf-8")
    fake_alert.chmod(0o755)
    status_outbox = tmp_path / "status"
    alert_outbox = tmp_path / "alerts"
    receipts = tmp_path / "receipts"
    replay_log = tmp_path / "replayed.log"

    queue_harness = f"""
source <(awk '/^prepare_update_delivery_dirs[(][)]/{{flag=1}} /^human_duration[(][)]/{{flag=0}} flag' {UPDATER!s})
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={status_outbox!s}
UPDATE_ALERT_OUTBOX_DIR={alert_outbox!s}
UPDATE_DELIVERY_RECEIPTS_DIR={receipts!s}
LOG_TAG=test-updater
send_alert_reliably success '✅ Atualização concluída' 'Resumo: ok' '' '' 'UPD-TEST-final'
"""
    _run_bash(queue_harness, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    jobs = list(alert_outbox.glob("*.json"))
    assert len(jobs) == 1

    fake_sudo.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = '-u' ]; then shift 2; fi\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    flush_harness = f"""
source <(awk '/^flush_update_alert_outbox[(][)]/{{flag=1}} /^notify_zip_status_message[(][)]/{{flag=0}} flag' {UPDATER!s})
prepare_update_delivery_dirs() {{ mkdir -p "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR"; }}
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={status_outbox!s}
UPDATE_ALERT_OUTBOX_DIR={alert_outbox!s}
UPDATE_DELIVERY_RECEIPTS_DIR={receipts!s}
flush_update_alert_outbox
"""
    _run_bash(
        flush_harness,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REPLAY_LOG": str(replay_log),
        },
    )
    assert not list(alert_outbox.glob("*.json"))
    assert (receipts / "UPD-TEST-final.alert.done").is_file()
    assert "Atualização concluída" in replay_log.read_text(encoding="utf-8")


def test_discord_status_state_is_saved_only_after_successful_edit() -> None:
    source = BOT.read_text(encoding="utf-8")
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



def test_stale_alert_claim_is_recovered_after_interrupted_flush(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
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
    fake_alert = repo / "alert.sh"
    replay_log = tmp_path / "stale-replayed.log"
    fake_alert.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$REPLAY_LOG\"\n", encoding="utf-8")
    fake_alert.chmod(0o755)

    alert_outbox = tmp_path / "alerts"
    receipts = tmp_path / "receipts"
    status_outbox = tmp_path / "status"
    alert_outbox.mkdir()
    stale = alert_outbox / ".sending.999.UPD-STALE.json"
    stale.write_text(
        json.dumps(
            {
                "event_id": "UPD-STALE",
                "type": "success",
                "title": "Atualização recuperada",
                "body": "Resumo: ok",
                "attempts": 0,
            }
        ),
        encoding="utf-8",
    )
    os.utime(stale, (1, 1))

    harness = f"""
source <(awk '/^flush_update_alert_outbox[(][)]/{{flag=1}} /^notify_zip_status_message[(][)]/{{flag=0}} flag' {UPDATER!s})
prepare_update_delivery_dirs() {{ mkdir -p "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR"; }}
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={status_outbox!s}
UPDATE_ALERT_OUTBOX_DIR={alert_outbox!s}
UPDATE_DELIVERY_RECEIPTS_DIR={receipts!s}
flush_update_alert_outbox
"""
    _run_bash(
        harness,
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "REPLAY_LOG": str(replay_log)},
    )
    assert not list(alert_outbox.glob("*.json"))
    assert not list(alert_outbox.glob(".sending.*.json"))
    assert (receipts / "UPD-STALE.alert.done").is_file()
    assert "Atualização recuperada" in replay_log.read_text(encoding="utf-8")


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
    source = BOT.read_text(encoding="utf-8")
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
    no_control_at = edit_block.index("view=self._make_zip_update_view(title, description, color)", failure_at)
    delivered_at = edit_block.index('"warning": "controle de rollback indisponível"', no_control_at)
    assert failure_at < no_control_at < delivered_at


def test_alert_outbox_never_deletes_attachment_outside_its_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    fake_sudo.chmod(0o755)
    (repo / "alert.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (repo / "alert.sh").chmod(0o755)

    alert_outbox = tmp_path / "alerts"
    receipts = tmp_path / "receipts"
    status_outbox = tmp_path / "status"
    alert_outbox.mkdir()
    external = tmp_path / "must-not-delete.txt"
    external.write_text("preserve", encoding="utf-8")
    (alert_outbox / "unsafe.json").write_text(
        json.dumps(
            {
                "event_id": "unsafe",
                "type": "error",
                "title": "Teste",
                "body": "Teste",
                "attachment": str(external),
                "attempts": 0,
            }
        ),
        encoding="utf-8",
    )

    harness = f"""
source <(awk '/^flush_update_alert_outbox[(][)]/{{flag=1}} /^notify_zip_status_message[(][)]/{{flag=0}} flag' {UPDATER!s})
prepare_update_delivery_dirs() {{ mkdir -p "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR"; }}
REPO_DIR={repo!s}
UPDATE_STATUS_OUTBOX_DIR={status_outbox!s}
UPDATE_ALERT_OUTBOX_DIR={alert_outbox!s}
UPDATE_DELIVERY_RECEIPTS_DIR={receipts!s}
flush_update_alert_outbox
"""
    _run_bash(harness, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert external.read_text(encoding="utf-8") == "preserve"
    dead = list((alert_outbox / "failed").glob("unsafe.json"))
    assert len(dead) == 1
    payload = json.loads(dead[0].read_text(encoding="utf-8"))
    assert "fora do outbox" in payload["last_error"]


def test_recovery_does_not_replace_latest_rollback_control_with_old_update() -> None:
    source = BOT.read_text(encoding="utf-8")
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
