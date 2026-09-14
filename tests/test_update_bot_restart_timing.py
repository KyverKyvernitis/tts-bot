from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        text=True,
        capture_output=True,
        check=True,
    )


def test_health_snapshot_is_parsed_once_per_refresh(tmp_path: Path) -> None:
    calls = tmp_path / "python-calls"
    harness = f'''
source <(awk '/^fetch_bot_health_json[(][)]/{{flag=1}} /^service_restart_count[(][)]/{{flag=0}} flag' {UPDATER!s})
curl() {{
  printf '%s\\n' '{{"healthy":true,"status":"ok","discord_ready":true,"discord_closed":false,"mongo_ok":true,"cog_loading_finished":true,"critical_failed_cogs":[],"loaded_cogs_count":19,"failed_cogs_count":0,"warnings":[],"latency_ms":120}}'
}}
python3() {{ printf '1\\n' >> {calls!s}; command python3 "$@"; }}
BOT_HEALTH_URL='http://127.0.0.1:10000/health'
BOT_HEALTH_JSON=''
BOT_HEALTH_DETAIL_STATUS=''
BOT_COGS_STATUS=''
BOT_WARNINGS_STATUS=''
BOT_HEALTH_IS_HEALTHY=0
BOT_HEALTH_READY_HEALTHY=0
refresh_bot_health_status
printf 'DETAIL=%s\\nCOGS=%s\\nWARN=%s\\nHEALTHY=%s READY=%s\\n' \
  "$BOT_HEALTH_DETAIL_STATUS" "$BOT_COGS_STATUS" "$BOT_WARNINGS_STATUS" \
  "$BOT_HEALTH_IS_HEALTHY" "$BOT_HEALTH_READY_HEALTHY"
printf 'CALLS=%s\\n' "$(wc -l < {calls!s})"
'''
    result = _run_bash(harness)
    assert "DETAIL=ok; discord=online; mongo=OK; latência=120ms" in result.stdout
    assert "COGS=19 carregada(s); 0 com falha" in result.stdout
    assert "WARN=sem avisos" in result.stdout
    assert "HEALTHY=1 READY=1" in result.stdout
    assert "CALLS=1" in result.stdout


def test_restart_verifier_uses_cached_ready_flag_instead_of_reparsing_json() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("verify_bot_after_restart() {")
    end = source.index("\nis_placeholder_status_text() {", start)
    block = source[start:end]
    assert "BOT_HEALTH_READY_HEALTHY == 1" in block
    assert "bot_health_python" not in block
    assert 'append_update_timing_ms "bot.service_active"' in block
    assert 'append_update_timing_ms "bot.first_health"' in block
    assert 'append_update_timing_ms "bot.first_ready"' in block
    assert 'append_update_timing_ms "bot.stability"' in block
    assert 'append_update_timing_ms "bot.verify_total"' in block


def test_restart_command_and_optional_lavalink_wait_have_separate_timings() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("deploy_bot() {")
    end = source.index("\nfrontend_release_root_for_key() {", start)
    block = source[start:end]
    assert 'append_update_timing_ms "bot.restart_command"' in block
    assert 'append_update_timing_ms "bot.lavalink_wait"' in block
    assert block.index('append_update_timing_ms "bot.restart_command"') < block.index("verify_bot_after_restart")


def test_detailed_timings_do_not_reset_coarse_step_clock() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("append_update_timing_ms() {")
    end = source.index("\nlog_update_operation_timing_ms() {", start)
    block = source[start:end]
    assert "UPDATER_STEP_LAST=" not in block
    assert 'UPDATER_TIMINGS+="${label}=${elapsed_text}"' in block
