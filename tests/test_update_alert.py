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
