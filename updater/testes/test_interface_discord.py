from __future__ import annotations

from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core
from updater.testes.fonte_discord import ler_fonte_discord
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "bot.py"
UPDATER = caminho_fonte_core()
WEBSERVER = ROOT / "webserver.py"
ALERT = ROOT / "alert.sh"


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_public_update_card_uses_custom_emojis_and_separate_title_progress_emoji() -> None:
    source = ler_fonte_discord()
    expected = {
        'UPDATE_EMOJI_CHECK = "<:checkmark:1548838297806311445>"',
        'UPDATE_EMOJI_ERROR = "<:x_mark:1548838423169605654>"',
        'UPDATE_EMOJI_DATABASE = "<:Database:1548838603059105872>"',
        'UPDATE_EMOJI_CLOUD = "<:Cloud:1548838660915462254>"',
        'UPDATE_EMOJI_FILES = "<:Files:1548838468665475193>"',
        'UPDATE_EMOJI_GITHUB = "<:Github:1548838545500807239>"',
        'UPDATE_EMOJI_PROGRESS = "<a:loading:1510065277868445796>"',
        'UPDATE_EMOJI_PROGRESS_TITLE = "<a:areia:1496606578395189473>"',
    }
    for line in expected:
        assert line in source


def test_progress_card_reveals_macro_stages_only_when_reached() -> None:
    source = ler_fonte_discord()
    renderer = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    block = renderer[renderer.index('if kind == "progress":'):renderer.index('if kind == "recovery":')]
    assert '("Pacote", "Segurança", "Preparação", "Isolamento", "Validação", "Release", "Promoção", "Aplicação", "Verificação", "GitHub")' in block
    assert 'lines = [f"# {UPDATE_EMOJI_PROGRESS_TITLE} {headline}"]' in block
    assert 'enumerate(macros[: current + 1])' in block
    assert 'lines.append(f"{UPDATE_EMOJI_PROGRESS} **{label}{duration_suffix}**")' in block
    assert 'lines.append(f"-# {micro[:240]}")' in block
    assert 'lines.append(f"{UPDATE_EMOJI_CHECK} {label}{duration_suffix}")' in block
    assert 'lines.append(f"○ {label}")' not in block


def test_final_card_is_compact_and_moves_technical_data_to_buttons() -> None:
    source = ler_fonte_discord()
    renderer = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    view = _block(source, "    def _make_zip_update_view", "\n    def _zip_update_alert_receipt_save_sync")
    assert "file_count_text" in renderer
    assert "diff_summary" in renderer
    assert "impact" in renderer
    assert "duration" in renderer
    assert "total_duration" in renderer
    assert 'lines.append(f"`{identifier}` · `{branch}`")' in renderer
    assert "final_duration = total_duration or duration or recovery_duration" in renderer
    assert 'lines.append(f"⏱ **{final_duration}**")' in renderer
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
    source = ler_fonte_discord()
    edit = _block(source, "    async def _edit_zip_status_from_update", "\n    def _zip_update_find_candidate_sync")
    assert 'str(presentation.get("kind") or "").strip().lower() == "final"' in edit
    assert '"mode": "info"' in edit
    assert '"presentation": presentation' in edit
    assert 'info_token = str(state_record.get("token") or "")' in edit


def test_info_buttons_reply_ephemerally() -> None:
    source = ler_fonte_discord()
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

    bot = ler_fonte_discord()
    resolver = _block(bot, "    def _zip_update_log_channel_id_sync", "\n    def _zip_update_claim_log_jobs_sync")
    # Legacy webhook is metadata-only migration: discover channel_id once, then
    # persist it. There is no webhook execute/send path.
    assert 'os.getenv("ALERT_WEBHOOK_URL"' in resolver
    assert 'payload.get("channel_id")' in resolver
    assert '"source": "legacy_webhook_metadata"' in resolver


def test_raw_log_channel_is_sent_by_bot_and_receipted_after_discord_confirmation() -> None:
    source = ler_fonte_discord()
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
    assert '"$FINAL_RAW_LOG" "bot-updater.log"' in block

def test_progress_card_persists_completed_macro_durations_and_keeps_detail_plain() -> None:
    source = ler_fonte_discord()
    renderer = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    updater = UPDATER.read_text(encoding="utf-8")
    progress = renderer[renderer.index('if kind == "progress":'):renderer.index('if kind == "recovery":')]
    assert 'raw_macro_durations = presentation.get("macro_durations")' in progress
    assert 'macro_durations.setdefault(completed_macro, completed_duration)' in progress
    assert 'duration_text = macro_durations.get(index, "") if index < current else ""' in progress
    assert 'lines.append(f"{UPDATE_EMOJI_CHECK} {label}{duration_suffix}")' in progress
    assert 'lines.append(f"{UPDATE_EMOJI_PROGRESS} **{label}{duration_suffix}**")' in progress
    assert 'lines.append(f"-# {completed_stage}"[:240])' in progress
    assert 'f"-# {UPDATE_EMOJI_CHECK} {completed_stage}' not in progress
    assert 'line="-# $done_label"' in updater
    assert 'zip_progress_add_macro_duration "$completed_macro_index" "$elapsed_ms"' in updater
    assert '"macro_durations": macro_durations' in updater
    assert 'preparation_history.append(f"-# {completed}")' in source

def test_progress_completed_microstep_can_stay_visible_when_macro_advances() -> None:
    source = ler_fonte_discord()
    renderer = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    progress = renderer[renderer.index('if kind == "progress":'):renderer.index('if kind == "recovery":')]
    assert 'if completed_stage and completed_macro == index:' in progress
    assert 'if index == current:' in progress


def test_final_card_shows_only_one_unlabelled_total_timer() -> None:
    source = ler_fonte_discord()
    renderer = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    final = renderer[renderer.index('if kind == "final":'):]
    assert 'final_duration = total_duration or duration or recovery_duration' in final
    assert 'lines.append(f"⏱ **{final_duration}**")' in final
    assert 'desde o envio' not in final
    assert 'execução **' not in final
    assert 'recuperação **' not in final


def test_progress_edits_are_coalesced_but_macro_and_recovery_transitions_are_immediate() -> None:
    source = ler_fonte_discord()
    block = _block(source, "    def _zip_update_progress_should_render", "\n    def _zip_update_progress_mark_rendered")
    assert 'kind not in {"progress", "recovery"}' in block
    assert 'age < 1.0' in block
    assert 'age < 8.0' in block
    assert 'same_phase' in block
    assert 'kind == "progress"' in block
    # Recovery is deliberately excluded from the same-phase 1s throttle.
    assert 'kind == "recovery" and same_phase' not in block


def test_automatic_failure_uses_recovery_timeline_before_final_card() -> None:
    bot = ler_fonte_discord()
    updater = UPDATER.read_text(encoding="utf-8")
    renderer = _block(bot, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    assert 'if kind == "recovery":' in renderer
    assert '("Código", "Runtimes", "Verificação")' in renderer
    assert 'failure_code = str(presentation.get("failure_code")' in renderer
    assert 'zip_recovery_publish "Restaurando código"' in updater
    assert 'zip_recovery_publish "Restaurando runtimes"' in updater
    assert 'zip_recovery_publish "Verificando versão anterior"' in updater
    assert 'notify_zip_status_message "recovering"' in updater


def test_final_failure_card_distinguishes_successful_and_failed_rollback() -> None:
    source = ler_fonte_discord()
    renderer = _block(source, "    def _zip_update_render_card_text", "\n    def _make_zip_update_view")
    assert 'rollback_ok = presentation.get("rollback_ok")' in renderer
    assert 'Versão anterior restaurada' in renderer
    assert 'Rollback incompleto · verificação manual necessária' in renderer
    assert 'recovery_duration' in renderer


def test_raw_log_card_is_explicitly_technical_and_receipt_deduplicated() -> None:
    source = ler_fonte_discord()
    block = _block(source, "    async def _zip_update_flush_raw_logs_once", "\n    def _zip_update_current_head_sync")
    assert 'resumo técnico · log anexado' in block
    assert 're.sub(r"^[^\\wÀ-ÿ<]+\\s*", "", title, count=1)' in block
    assert 'if receipt.is_file()' in block
    assert '_zip_update_alert_receipt_save_sync' in block


def test_details_prettify_internal_timing_names() -> None:
    source = ler_fonte_discord()
    block = _block(source, "    async def _on_zip_update_info_click", "\n    async def _on_zip_update_control_click")
    assert '"receive_to_updater": "Recebido → updater"' in block
    assert '"candidate_apply": "Aplicação isolada"' in block
    assert '"candidate_promote": "Promoção"' in block
    assert '"push": "GitHub"' in block
    assert '"execution": "Execução updater"' in block
    assert '"total": "Total desde envio"' in block
    assert '"```text\\n"' in block

def test_macro_classifier_matches_the_visible_ten_stage_timeline() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _block(source, "zip_progress_macro_index() {", "\nzip_progress_status() {")
    cases = {
        "Conferindo ZIP": "0",
        "Analisando segurança": "1",
        "Validando permissões": "1",
        "Validando estado local": "2",
        "Aplicando em área isolada": "3",
        "Validando runtime em isolamento": "4",
        "TypeScript frontend": "4",
        "Candidato READY em isolamento": "5",
        "Promovendo para a VPS": "6",
        "Reiniciando processo: bot": "7",
        "Verificando comandos": "8",
        "Publicando no GitHub...": "9",
    }
    for label, expected in cases.items():
        completed = subprocess.run(
            ["bash", "-c", block + "\nzip_progress_macro_index \"$1\"", "macro-test", label],
            text=True,
            capture_output=True,
            check=True,
        )
        assert completed.stdout == expected, (label, completed.stdout, expected)


def test_macro_duration_history_accumulates_and_serializes_all_completed_stages() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    helper = _block(source, "zip_progress_add_macro_duration() {", "\nzip_progress_status() {")
    format_block = _block(source, "human_duration() {", "\nmark_update_timing() {")
    completed = subprocess.run(
        [
            "bash", "-eu", "-o", "pipefail", "-c",
            format_block
            + "\nZIP_PROGRESS_MACRO_MAX_INDEX=9"
            + "\n" + helper
            + "\nzip_progress_add_macro_duration 0 1200"
            + "\nzip_progress_add_macro_duration 0 800"
            + "\nzip_progress_add_macro_duration 1 3400"
            + "\nprintf '%s' \"$(zip_progress_macro_duration_pairs)\"",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout == "0=2s|1=3,4s"




def test_progress_macro_never_regresses_when_a_later_microstep_looks_like_validation() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert "ZIP_PROGRESS_CURRENT_MACRO_INDEX=-1" in source
    advance = _block(source, "zip_progress_advance_macro_index() {", "\nzip_progress_status() {")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            "ZIP_PROGRESS_CURRENT_MACRO_INDEX=-1\n"
            + advance
            + "\nzip_progress_advance_macro_index 4; echo $ZIP_PROGRESS_CURRENT_MACRO_INDEX"
            + "\nzip_progress_advance_macro_index 2; echo $ZIP_PROGRESS_CURRENT_MACRO_INDEX"
            + "\nzip_progress_advance_macro_index 5; echo $ZIP_PROGRESS_CURRENT_MACRO_INDEX",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.splitlines() == ["4", "4", "5"]


def test_bot_ignores_regressive_progress_and_recovery_events() -> None:
    source = ler_fonte_discord()
    block = _block(source, "    def _zip_update_progress_should_render", "\n    def _zip_update_progress_mark_rendered")
    assert 'incoming_macro_int < previous_macro_int' in block
    assert 'return False, "macroetapa regressiva"' in block
    assert 'incoming_step_int < previous_step_int' in block
    assert 'return False, "etapa de recuperação regressiva"' in block

def test_progress_card_has_no_accent_color_but_final_cards_keep_status_color() -> None:
    source = ler_fonte_discord()
    block = _block(source, "    def _make_zip_update_view", "\n    def _make_zip_update_confirmation_view")
    assert 'str(presentation.get("kind") or "").lower() == "progress"' in block
    assert 'container_kwargs["accent_color"] = color' in block
    assert 'discord.ui.Container(*children, **container_kwargs)' in block


def test_progress_handoff_preserves_discord_receive_time_across_process_boundary() -> None:
    bot = ler_fonte_discord()
    updater = UPDATER.read_text(encoding="utf-8")
    writer = _block(bot, "    def _write_local_update_candidate_sync", "\n    def _trigger_updater_service_sync")
    handler = _block(bot, "    async def _handle_zip_update_message", "\n    async def on_guild_join")
    hydrate = _block(updater, "zip_progress_hydrate_from_candidate() {", "\nzip_progress_trim_history() {")
    assert 'progress_started_epoch_ms: int | None = None' in writer
    assert '"started_at_epoch_ms": max(0, int(progress_started_epoch_ms or 0))' in writer
    assert 'received_at = getattr(message, "created_at", None)' in handler
    assert 'progress_elapsed_text()' in handler
    assert 'started_at_ms = max(0, int(handoff.get("started_at_epoch_ms") or 0))' in hydrate
    assert 'ZIP_PROGRESS_STARTED_MS="$started_at_ms"' in hydrate
    assert 'ZIP_PROGRESS_UPDATER_DELAY_MS=$((updater_started_ms - started_at_ms))' in hydrate


def test_final_timings_distinguish_receive_delay_execution_and_total() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert 'receive_to_updater=${RECEIVE_TO_UPDATER_DURATION}' in source
    assert 'execution=${DURATION}' in source
    assert 'total=${TOTAL_DURATION}' in source
    assert 'UI_TOTAL_DURATION="$TOTAL_FROM_RECEIVE_DURATION"' in source
    assert 'DURATION_DISPLAY="$TOTAL_FROM_RECEIVE_DURATION desde o envio · $DURATION de execução"' in source

def test_recovery_status_is_red_but_not_treated_as_final_delivery() -> None:
    bot = ler_fonte_discord()
    updater = UPDATER.read_text(encoding="utf-8")
    color = _block(bot, "    def _zip_update_status_color", "\n    def _zip_update_normalize_title")
    notify = _block(updater, "notify_zip_status_message() {", "\npost_direct_update_message() {")
    assert '"error", "failed", "failure", "recovering"' in color
    assert '^(success|ok|warn|error|done|failed)$' in notify
    assert 'notify_zip_status_message "recovering"' in updater



def test_reconciler_recognizes_new_failure_cards_as_terminal() -> None:
    source = ler_fonte_discord()
    block = _block(
        source,
        "    async def _zip_update_reconcile_archived_messages_once",
        "\n    async def _zip_update_reconcile_loop",
    )
    assert '"atualização não aplicada"' in block
    assert '"atualizacao nao aplicada"' in block
    assert '"recuperação necessária"' in block
    assert '"recuperacao necessaria"' in block


def test_reconciler_rebuilds_compact_final_cards_from_archived_state() -> None:
    source = ler_fonte_discord()
    block = _block(
        source,
        "    async def _zip_update_reconcile_archived_messages_once",
        "\n    async def _zip_update_reconcile_loop",
    )
    assert 'presentation: dict[str, object] | None = None' in block
    assert '"kind": "final"' in block
    assert 'rollback_ok_raw = state_data.get("rollback_ok")' in block
    assert 'failure_code = str(state_data.get("failure_code")' in block
    assert '"recovery_duration": recovery_duration' in block
    assert '**({"ui": presentation} if isinstance(presentation, dict) else {})' in block


def test_macro_transition_auto_closes_active_stage_duration() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    macro_index = _block(source, "zip_progress_macro_index() {", "\nzip_progress_advance_macro_index() {")
    add_duration = _block(source, "zip_progress_add_macro_duration() {", "\nzip_progress_macro_duration_pairs() {")
    duration_pairs = _block(source, "zip_progress_macro_duration_pairs() {", "\nzip_progress_status() {")
    close_active = _block(source, "zip_progress_close_active_macro_on_advance() {", "\nzip_progress_publish() {")
    harness = "\n".join(
        [
            "format_update_duration_ms() { printf '%sms' \"$1\"; }",
            macro_index,
            add_duration,
            duration_pairs,
            close_active,
            "ZIP_PROGRESS_MACRO_MAX_INDEX=9",
            "ZIP_PROGRESS_STAGE_LABEL='Validando candidato'",
            "ZIP_PROGRESS_STAGE_STARTED_MS=1000",
            "ZIP_PROGRESS_LAST_DONE_LABEL=''",
            "ZIP_PROGRESS_LAST_DONE_DURATION=''",
            "ZIP_PROGRESS_LAST_DONE_MACRO_INDEX=-1",
            "zip_progress_close_active_macro_on_advance 'Criando release candidata' 3500",
            "printf 'PAIRS=%s\\n' \"$(zip_progress_macro_duration_pairs)\"",
            "printf 'LAST=%s|%s|%s\\n' \"$ZIP_PROGRESS_LAST_DONE_LABEL\" \"$ZIP_PROGRESS_LAST_DONE_DURATION\" \"$ZIP_PROGRESS_LAST_DONE_MACRO_INDEX\"",
        ]
    )
    result = subprocess.run(["bash", "-u", "-c", harness], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "PAIRS=4=2500ms" in result.stdout
    assert "LAST=Validando candidato|2500ms|4" in result.stdout


def test_local_candidate_exposes_real_validation_release_and_application_phases() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert 'zip_progress_done_and_publish "Isolamento preparado" "Validando candidato"' in source
    assert 'zip_progress_done_and_publish "Candidato validado" "Criando release candidata"' in source
    classifier = _block(source, "zip_progress_macro_index() {", "\nzip_progress_advance_macro_index() {")
    assert "*validando\\ aplicação*" in classifier
