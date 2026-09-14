from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core


ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot.py"
UPDATER = caminho_fonte_core()


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def test_trigger_reports_real_systemctl_result_and_active_state() -> None:
    source = BOT.read_text(encoding="utf-8")
    state = _block(source, "    def _updater_service_state_sync", "\n    def _trigger_updater_service_sync")
    trigger = _block(source, "    def _trigger_updater_service_sync", "\n    async def _watch_updater_candidate_dispatch")

    assert '["systemctl", "is-active", "tts-bot-updater.service"]' in state
    assert 'before_state in {"active", "activating", "reloading"}' in trigger
    assert '["sudo", "-n", "systemctl", "start", "--no-block", "tts-bot-updater.service"]' in trigger
    assert '["systemctl", "start", "--no-block", "tts-bot-updater.service"]' in trigger
    assert '"dispatch do updater: comando=%s rc=%s estado_antes=%s detalhe=%s"' in trigger
    assert 'return False, f"timer aplicará depois ({detail})"' in trigger


def test_candidate_watchdog_retriggers_when_service_becomes_free() -> None:
    source = BOT.read_text(encoding="utf-8")
    watchdog = _block(source, "    async def _watch_updater_candidate_dispatch", "\n    async def _dispatch_updater_candidate")

    assert "deadline = started + 60.0" in watchdog
    assert 'if queue_state != "pending":' in watchdog
    assert 'service_state not in {"active", "activating", "reloading"}' in watchdog
    assert "self._trigger_updater_service_sync" in watchdog
    assert 'await asyncio.sleep(2.0 if elapsed < 15.0 else 3.0)' in watchdog
    assert "timer permanece como fallback" in watchdog


def test_zip_handler_uses_immediate_dispatch_watchdog() -> None:
    source = BOT.read_text(encoding="utf-8")
    handler = _block(source, "    async def _handle_zip_update_message", "\n    async def on_guild_join")
    dispatch = _block(source, "    async def _dispatch_updater_candidate", "\n    def _guess_repo_name")

    assert "await self._dispatch_updater_candidate(candidate_id, display_id)" in handler
    assert "asyncio.create_task(self._watch_updater_candidate_dispatch(candidate_id, display_id))" in dispatch
    assert '"dispatch inicial do updater para %s: ok=%s detalhe=%s"' in dispatch


def test_updater_records_queue_wait_after_claim() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    load = _block(source, "load_pending_local_candidate() {", "\nverify_local_candidate_integrity() {")

    assert 'json_field_from_file "$LOCAL_CANDIDATE_PENDING_FILE" created_at' in load
    assert "time.time() * 1000" in load
    assert "[timing] dispatch.queue_wait=%s (%sms)" in load
    assert 'logger -t "$LOG_TAG" "timing dispatch.queue_wait=${queue_wait_ms}ms"' in load


def test_updater_timer_and_exit_retrigger_remain_as_fallbacks() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    retrigger = _block(source, "trigger_updater_if_queue_pending() {", "\nrefresh_pending_queue_messages() {")
    assert "local_candidate_queue_has_pending" in retrigger
    assert 'systemctl start --no-block "$UPDATER_UNIT"' in retrigger
    assert source.count("trigger_updater_if_queue_pending") >= 2


def test_rollback_and_redo_share_immediate_dispatch_watchdog() -> None:
    source = BOT.read_text(encoding="utf-8")
    control = _block(source, "    async def _watch_updater_control_dispatch", "\n    async def _dispatch_updater_control_request")
    confirm = _block(source, "    async def _start_zip_update_rollback_flow", "\n    async def _handle_zip_update_message")

    assert 'if state != "pending":' in control
    assert 'service_state not in {"active", "activating", "reloading"}' in control
    assert "self._trigger_updater_service_sync" in control
    assert "await self._dispatch_updater_control_request(" in confirm
    assert 'action_label = "reaplicação" if mode == "redo" else "rollback"' in confirm
    assert "_rollback_start_watchdog" in confirm
