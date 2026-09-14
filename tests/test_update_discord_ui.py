from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot.py"
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"
WEBSERVER = ROOT / "webserver.py"
ALERT = ROOT / "alert.sh"


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_public_update_card_uses_custom_emojis_and_existing_progress_emoji() -> None:
    source = BOT.read_text(encoding="utf-8")
    expected = {
        'UPDATE_EMOJI_CHECK = "<:checkmark:1548838297806311445>"',
        'UPDATE_EMOJI_ERROR = "<:x_mark:1548838423169605654>"',
        'UPDATE_EMOJI_DATABASE = "<:Database:1548838603059105872>"',
        'UPDATE_EMOJI_CLOUD = "<:Cloud:1548838660915462254>"',
        'UPDATE_EMOJI_FILES = "<:Files:1548838468665475193>"',
        'UPDATE_EMOJI_GITHUB = "<:Github:1548838545500807239>"',
        'UPDATE_EMOJI_PROGRESS = "<a:loading:1510065277868445796>"',
    }
    for line in expected:
        assert line in source


def test_progress_card_has_stable_macro_stages_and_one_dynamic_microstep() -> None:
    source = BOT.read_text(encoding="utf-8")
    block = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    assert '("Preparação", "Validação", "Release", "Promoção", "Verificação", "GitHub")' in block
    assert 'lines.append(f"{UPDATE_EMOJI_PROGRESS} **{label}**")' in block
    assert 'lines.append(f"-# {micro[:220]}")' in block
    assert 'lines.append(f"{UPDATE_EMOJI_CHECK} {label}")' in block
    assert 'lines.append(f"○ {label}")' in block


def test_final_card_is_compact_and_moves_technical_data_to_buttons() -> None:
    source = BOT.read_text(encoding="utf-8")
    renderer = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    view = _block(source, "    def _make_zip_update_view", "\n    def _zip_update_alert_receipt_save_sync")
    assert "file_count_text" in renderer
    assert "diff_summary" in renderer
    assert "impact" in renderer
    assert "duration" in renderer
    assert "GitHub sincronizado" in renderer
    assert 'label="Detalhes"' in view
    assert 'label="Arquivos"' in view
    assert "UPDATE_EMOJI_DATABASE" in view
    assert "UPDATE_EMOJI_FILES" in view
    # Timings/checks/cache/test plan belong to the ephemeral Details view, not
    # to the compact renderer itself.
    for technical in ("checks_text", "timings_text", "cache_text", "tests_text"):
        assert technical not in renderer


def test_final_info_buttons_survive_even_without_rollback_control() -> None:
    source = BOT.read_text(encoding="utf-8")
    edit = _block(source, "    async def _edit_zip_status_from_update", "\n    def _zip_update_find_candidate_sync")
    assert 'str(presentation.get("kind") or "").strip().lower() == "final"' in edit
    assert '"mode": "info"' in edit
    assert '"presentation": presentation' in edit
    assert 'info_token = str(state_record.get("token") or "")' in edit


def test_info_buttons_reply_ephemerally() -> None:
    source = BOT.read_text(encoding="utf-8")
    block = _block(source, "    async def _on_zip_update_info_click", "\n    async def _on_zip_update_control_click")
    assert "ephemeral=True" in block
    assert 'kind == "files"' in block
    assert '"**Verificações**"' in block
    assert '"**Tempos**"' in block


def test_webhook_is_not_a_delivery_transport_anymore() -> None:
    source = ALERT.read_text(encoding="utf-8")
    assert "ALERT_WEBHOOK_URL" not in source
    assert "curl" not in source
    assert "urllib" not in source
    assert '"delivery": "discord_bot"' in source

    bot = BOT.read_text(encoding="utf-8")
    resolver = _block(bot, "    def _zip_update_log_channel_id_sync", "\n    def _zip_update_claim_log_jobs_sync")
    # Legacy webhook is metadata-only migration: discover channel_id once, then
    # persist it. There is no webhook execute/send path.
    assert 'os.getenv("ALERT_WEBHOOK_URL"' in resolver
    assert 'payload.get("channel_id")' in resolver
    assert '"source": "legacy_webhook_metadata"' in resolver


def test_raw_log_channel_is_sent_by_bot_and_receipted_after_discord_confirmation() -> None:
    source = BOT.read_text(encoding="utf-8")
    block = _block(source, "    async def _zip_update_flush_raw_logs_once", "\n    def _zip_update_current_head_sync")
    send_at = block.index("await channel.send")
    receipt_at = block.index("_zip_update_alert_receipt_save_sync", send_at)
    remove_at = block.index("claim.unlink", receipt_at)
    assert send_at < receipt_at < remove_at
    assert "discord.File" in block
    assert "allowed_mentions=discord.AllowedMentions.none()" in block


def test_shell_only_wakes_bot_for_raw_logs() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _block(source, "flush_update_alert_outbox() {", "\nnotify_zip_status_message() {")
    assert "/internal/update/flush-logs" in block
    assert "alert.sh" not in block
    assert "curl" not in block


def test_webserver_exposes_bot_owned_raw_log_flush_endpoint() -> None:
    source = WEBSERVER.read_text(encoding="utf-8")
    assert '@app.post("/internal/update/flush-logs")' in source
    assert '_dispatch_internal_update_action("flush_logs")' in source


def test_final_failures_also_use_structured_single_message_renderer() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _block(source, "notify_zip_status_message() {", "\npost_direct_update_message() {")
    assert "Todo estado final usa o mesmo renderer compacto do bot" in block
    assert '"kind": "final"' in block
    assert '"headline": headline' in block
    assert "generated_fallback_ui" in block


def test_success_path_queues_the_raw_updater_log_for_technical_channel() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    marker = 'FINAL_ALERT_EVENT_ID="${UPDATE_DISPLAY_ID:-update}-final-${SHORT_TO:-unknown}"'
    start = source.index(marker)
    end = source.index("flush_update_alert_outbox || true", start)
    block = source[start:end]
    assert 'FINAL_RAW_LOG="$RUN_LOG_FILE"' in block
    assert '"$FINAL_RAW_LOG" "tts-bot-updater.log"' in block
