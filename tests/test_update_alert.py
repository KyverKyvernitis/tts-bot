from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _alert_payload(title: str, body: str, alert_type: str = "success") -> dict:
    env = os.environ.copy()
    env.update({"ALERT_DRY_RUN": "1", "ALERT_WEBHOOK_URL": ""})
    result = subprocess.run(
        ["bash", str(ROOT / "alert.sh"), alert_type, title, body],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _text(payload: dict) -> str:
    return "\n".join(
        child.get("content", "")
        for container in payload.get("components", [])
        for child in container.get("components", [])
        if child.get("type") == 10
    )


def test_update_alert_is_compact_and_does_not_duplicate_header_fields() -> None:
    body = """Resumo: Atualização aplicada e estabilidade confirmada.
Identificador: UPD-ABC12345
Branch: main
Commit: 1111111 → 2222222
Update: 4 arquivo(s) · +20 -3
Aplicação: reinício completo
Processos alterados: bot
Duração: 41s
Verificações:
✓ Bot — estável (10s)
✓ Python — OK
✓ Bash — OK
Tempos: fetch=2s, preflight=3s, bot=20s, total=41s
Arquivos:
• cogs/feedback/cog.py  +20 -3
Hora: 22/07/2026 01:30:00"""
    payload = _alert_payload("✅ Atualização concluída", body)
    text = _text(payload)

    assert "## Resultado" in text
    assert "## Verificações" in text
    assert "## Arquivos alterados" in text
    assert "## Tempos" in text
    assert text.count("branch `main`") == 1
    assert text.count("commit `1111111 → 2222222`") == 1
    assert "Resumo rápido" not in text
    assert "\n\n- **Aplicação" not in text


def test_generic_alert_still_uses_generic_renderer() -> None:
    payload = _alert_payload(
        "Serviço indisponível",
        "Resumo: O serviço parou.\nHost: vps\nServiço: tts-bot\nMotivo: timeout\nHora: agora",
        "error",
    )
    text = _text(payload)
    assert "Resumo rápido" in text
    assert "## Detalhes" in text
    assert "timeout" in text


def test_changed_shell_scripts_pass_bash_syntax_check() -> None:
    for path in (
        ROOT / "alert.sh",
        ROOT / "scripts" / "tts-bot-update.sh",
        ROOT / "scripts" / "install-vps-systemd-units.sh",
    ):
        subprocess.run(["bash", "-n", str(path)], check=True, cwd=ROOT)


def test_update_progress_renders_each_microstep_with_elapsed_time() -> None:
    harness = r'''
source <(awk '/^human_duration\(\)/{flag=1} /^mark_update_timing\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
source <(awk '/^zip_progress_title\(\)/{flag=1} /^rollback_request_roots\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
ROLLBACK_CONTROL_MODE=0
LOCAL_CANDIDATE_MODE=1
LOCAL_CANDIDATE_DISPLAY_ID=UPD-TEST1234
LOCAL_CANDIDATE_ID=zip-test
UPDATE_STAGE_EMOJI='<a:loading:test>'
ZIP_PROGRESS_HISTORY=''
ZIP_PROGRESS_COMPLETED_COUNT=0
ZIP_PROGRESS_HIDDEN_COUNT=0
ZIP_PROGRESS_MAX_VISIBLE_STEPS=10
ZIP_PROGRESS_STAGE_LABEL=''
ZIP_PROGRESS_STAGE_STARTED_MS=0
ZIP_PROGRESS_STARTED_MS=0
notify_zip_status_message(){ printf '%s\n---\n' "$3"; }
update_local_candidate_heartbeat(){ :; }
zip_progress_publish 'Conferindo ZIP'
sleep 0.02
zip_progress_done_and_publish 'Base conferida' 'Validando integridade'
sleep 0.02
zip_progress_done_and_publish 'Integridade confirmada' 'Aplicando na VPS'
printf 'FORMAT=%s\n' "$(format_update_duration_ms 2450)"
'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    text = result.stdout
    assert "<a:loading:test> **Conferindo ZIP**" in text
    assert "✅ Base conferida · " in text
    assert "✅ Integridade confirmada · " in text
    assert "<a:loading:test> **Aplicando na VPS**" in text
    assert "UPD-TEST1234 · 2 etapas concluídas" in text
    assert "FORMAT=2,5s" in text


def test_site_progress_uses_real_build_stage_instead_of_generic_restart() -> None:
    harness = r'''
source <(awk '/^human_duration\(\)/{flag=1} /^mark_update_timing\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
source <(awk '/^zip_progress_title\(\)/{flag=1} /^rollback_request_roots\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
fast_reload_modules_for_changed_files(){ :; }
FRONT_CHANGED=1
BACK_CHANGED=0
BOT_CHANGED=0
FAST_RELOAD_STATUS='não usado'
ROLLBACK_CONTROL_MODE=0
printf 'STAGE=%s\n' "$(zip_progress_next_apply_stage)"
printf 'TITLE=%s\n' "$(zip_progress_title 'Compilando o site')"
'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "STAGE=Instalando dependências do site" in result.stdout
    assert "TITLE=🌐 Atualizando site" in result.stdout
    assert "Reiniciando processo: atividade" not in result.stdout


def test_progress_history_does_not_repeat_completed_microsteps() -> None:
    harness = r'''
source <(awk '/^human_duration\(\)/{flag=1} /^mark_update_timing\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
source <(awk '/^zip_progress_title\(\)/{flag=1} /^rollback_request_roots\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
ZIP_PROGRESS_HISTORY=''
ZIP_PROGRESS_COMPLETED_COUNT=0
ZIP_PROGRESS_HIDDEN_COUNT=0
ZIP_PROGRESS_MAX_VISIBLE_STEPS=10
ZIP_PROGRESS_STAGE_LABEL='Compilando o site'
ZIP_PROGRESS_STAGE_STARTED_MS="$(update_now_ms)"
ZIP_PROGRESS_DONE_LABELS=''
zip_progress_done 'Site compilado'
ZIP_PROGRESS_STAGE_LABEL='Compilando o site'
ZIP_PROGRESS_STAGE_STARTED_MS="$(update_now_ms)"
zip_progress_done 'Site compilado'
printf 'COUNT=%s\n%s\n' "$ZIP_PROGRESS_COMPLETED_COUNT" "$ZIP_PROGRESS_HISTORY"
'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "COUNT=1" in result.stdout
    assert result.stdout.count("Site compilado") == 1


def test_final_status_does_not_render_bare_ok_line() -> None:
    harness = r'''
source <(awk '/^build_final_status_description\(\)/{flag=1} /^deploy_bot\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
build_final_status_description 'Tudo certo.' 'UPD-TEST' '1111111' '2222222' 3 '+10 -2' 'sem reinício do bot' '42s' 'OK'
printf '%s\n---\n' "$ZIP_STATUS_DESCRIPTION"
build_final_status_description 'Com aviso.' 'UPD-TEST' '1111111' '2222222' 3 '+10 -2' 'sem reinício do bot' '42s' 'OK com avisos'
printf '%s\n' "$ZIP_STATUS_DESCRIPTION"
'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    success, warning = result.stdout.split("\n---\n", 1)
    assert "\nOK\n" not in f"\n{success}\n"
    assert "Saúde:" not in success
    assert "Saúde: OK com avisos" in warning


def test_frontend_deploy_reports_dependency_build_publish_and_validation_stages() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")
    frontend = source[source.index("deploy_frontend() {") : source.index("\ndeploy_backend() {")]
    backend = source[source.index("deploy_backend() {") : source.index("\n\nrollback_after_failure() {")]

    for expected in (
        '"Instalando dependências do site"',
        '"Compilando o site"',
        '"Publicando o site"',
        '"Validando o site"',
    ):
        assert expected in frontend + backend
    assert "Reiniciando processo: atividade" not in frontend + backend
    assert "zip_progress_run_as_ubuntu" in frontend
