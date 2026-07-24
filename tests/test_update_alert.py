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
    assert "STAGE=Instalando dependências" in result.stdout
    assert "TITLE=🛠️ Compilando atualização" in result.stdout
    assert "Reiniciando processo: atividade" not in result.stdout


def test_progress_history_does_not_repeat_completed_microsteps() -> None:
    harness = r'''
source <(awk '/^human_duration\(\)/{flag=1} /^mark_update_timing\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
source <(awk '/^zip_progress_title\(\)/{flag=1} /^rollback_request_roots\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
ZIP_PROGRESS_HISTORY=''
ZIP_PROGRESS_COMPLETED_COUNT=0
ZIP_PROGRESS_HIDDEN_COUNT=0
ZIP_PROGRESS_MAX_VISIBLE_STEPS=10
ZIP_PROGRESS_STAGE_LABEL='Compilando interface'
ZIP_PROGRESS_STAGE_STARTED_MS="$(update_now_ms)"
ZIP_PROGRESS_DONE_LABELS=''
zip_progress_done 'Interface compilada'
ZIP_PROGRESS_STAGE_LABEL='Compilando interface'
ZIP_PROGRESS_STAGE_STARTED_MS="$(update_now_ms)"
zip_progress_done 'Interface compilada'
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
    assert result.stdout.count("Interface compilada") == 1


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
        '"Instalando dependências"',
        '"Compilando interface"',
        '"Publicando interface"',
        '"Preparando servidor"',
        '"Compilando servidor"',
        '"Validando"',
    ):
        assert expected in frontend + backend
    assert "Reiniciando processo: atividade" not in frontend + backend
    assert "zip_progress_run_as_ubuntu" in frontend


def test_update_progress_titles_follow_each_phase_without_naming_the_subsystem() -> None:
    harness = r'''
source <(awk '/^zip_progress_title\(\)/{flag=1} /^zip_progress_status\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
ROLLBACK_CONTROL_MODE=0
printf 'CHECK=%s\n' "$(zip_progress_title 'Conferindo ZIP')"
printf 'PREPARE=%s\n' "$(zip_progress_title 'Instalando dependências')"
printf 'BUILD=%s\n' "$(zip_progress_title 'Compilando interface')"
printf 'PUBLISH=%s\n' "$(zip_progress_title 'Publicando interface')"
printf 'RESTART=%s\n' "$(zip_progress_title 'Reiniciando servidor')"
printf 'COMMIT=%s\n' "$(zip_progress_title 'Fazendo commit...')"
printf 'GITHUB=%s\n' "$(zip_progress_title 'Publicando no GitHub...')"
printf 'FINAL=%s\n' "$(zip_progress_title 'Finalizando...')"
'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "CHECK=🔎 Verificando atualização" in result.stdout
    assert "PREPARE=📦 Preparando atualização" in result.stdout
    assert "BUILD=🛠️ Compilando atualização" in result.stdout
    assert "PUBLISH=🚀 Publicando atualização" in result.stdout
    assert "RESTART=🔄 Reiniciando serviços" in result.stdout
    assert "COMMIT=📝 Registrando atualização" in result.stdout
    assert "GITHUB=☁️ Publicando atualização" in result.stdout
    assert "FINAL=✅ Finalizando atualização" in result.stdout
    assert "site" not in result.stdout.lower()


def test_update_copy_remains_short_and_does_not_name_the_site() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")
    title_block = source[source.index("zip_progress_title() {") : source.index("\nzip_progress_status() {")]
    deploy_block = source[source.index("deploy_frontend() {") : source.index("\n\nrollback_after_failure() {")]

    assert "Atualizando site" not in title_block
    assert "dependências do site" not in deploy_block
    assert "servidor do site" not in deploy_block
    assert "Atualização aplicada e validada." in source


def test_update_runtime_state_marker_is_transactional() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")
    write_at = source.index("write_update_runtime_state() {")
    publish_at = source.index('write_update_runtime_state "$stage_label"', write_at)
    clear_at = source.index("clear_update_runtime_state() {", write_at)
    cleanup_at = source.index("clear_update_runtime_state || true", clear_at)

    assert "heartbeat_epoch" in source[write_at:clear_at]
    assert write_at < publish_at
    assert clear_at < cleanup_at


def test_update_installs_root_systemd_templates_and_treats_install_failure_as_fatal() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")
    classify = source[source.index("classify_changed_files() {") : source.index("\ndeploy_audio_services() {")]
    deploy = source[source.index("deploy_vps_systemd_units() {") : source.index("\ndeploy_alert_unit() {")]

    assert r"tts-bot-updater\.(service|timer)" in classify
    assert r"sinuca-activity-server\.service" in classify
    assert "deploy/systemd/(vps/)?" in classify
    assert 'return "$rc"' in deploy
    assert 'UPDATE_HAS_WARNINGS=1' not in deploy


def test_frontend_publish_removes_hidden_stale_files_and_checks_build_output() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")
    frontend = source[source.index("deploy_frontend() {") : source.index("\ndeploy_backend() {")]

    assert 'dist/index.html' in frontend
    assert 'rsync -a --delete' in frontend
    assert 'find "$FRONT_PUBLISH_DIR" -mindepth 1 -maxdepth 1' in frontend
    assert 'rm -rf "${FRONT_PUBLISH_DIR:?}/"*' not in frontend


def test_backend_restart_is_owned_by_systemd_instead_of_detached_nohup() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")
    backend = source[source.index("deploy_backend() {") : source.index("\n\nrollback_after_failure() {")]

    assert 'systemctl restart "$BACK_SERVICE"' in backend
    assert 'systemctl is-active --quiet "$BACK_SERVICE"' in backend
    assert 'nohup node dist/index.js' not in backend
    assert 'fuser -k "${BACK_PORT}/tcp"' not in backend


def test_build_cannot_silently_dirty_tracked_files_before_commit() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")
    guard_at = source.index("ensure_no_unstaged_tracked_changes() {")
    deploy_at = source.index("run_core_worker_post_update_automation", guard_at)
    check_at = source.index("ensure_no_unstaged_tracked_changes", deploy_at)
    publish_at = source.index("publish_local_candidate_after_validation", check_at)

    assert guard_at < deploy_at < check_at < publish_at
    guard = source[guard_at : source.index("\nfail_local_changes_before_pull() {", guard_at)]
    assert "git diff --name-only" in guard
    assert "return 1" in guard


def test_candidate_dirty_check_uses_ubuntu_git_and_fails_closed(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        "#!/usr/bin/env bash\necho 'git indisponível' >&2\nexit 42\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)

    harness = r'''
source <(awk '/^candidate_local_changes_are_expected\(\)/{flag=1} /^ensure_no_unstaged_tracked_changes\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
LOCAL_CANDIDATE_MODE=1
CHANGED_FILES_RAW='bot.py'
REPO_DIR="$PWD"
set +e
candidate_local_changes_are_expected
rc=$?
set -e
printf 'RC=%s\n' "$rc"
'''
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "RC=2" in result.stdout
    assert "git indisponível" in result.stderr


def test_systemd_overlay_uses_the_template_path_that_changed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root_templates = repo / "deploy" / "systemd"
    vps_templates = root_templates / "vps"
    vps_templates.mkdir(parents=True)
    (root_templates / "sinuca-activity-server.service").write_text("ROOT\n", encoding="utf-8")
    (vps_templates / "sinuca-activity-server.service").write_text("VPS\n", encoding="utf-8")

    harness = r'''
source <(awk '/^managed_systemd_template_source\(\)/{flag=1} /^deploy_vps_systemd_units\(\)/{flag=0} flag' scripts/tts-bot-update.sh)
REPO_DIR="$TEST_REPO"
CHANGED_FILES_RAW='deploy/systemd/sinuca-activity-server.service'
overlay="$TEST_REPO/overlay-root"
build_vps_systemd_template_overlay "$overlay"
printf 'ROOT=%s\n' "$(cat "$overlay/sinuca-activity-server.service")"
CHANGED_FILES_RAW=$'deploy/systemd/sinuca-activity-server.service\ndeploy/systemd/vps/sinuca-activity-server.service'
overlay="$TEST_REPO/overlay-vps"
build_vps_systemd_template_overlay "$overlay"
printf 'VPS=%s\n' "$(cat "$overlay/sinuca-activity-server.service")"
'''
    env = os.environ.copy()
    env["TEST_REPO"] = str(repo)
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "ROOT=ROOT" in result.stdout
    assert "VPS=VPS" in result.stdout


def test_critical_git_diff_results_are_not_silently_replaced_with_empty_output() -> None:
    source = (ROOT / "scripts" / "tts-bot-update.sh").read_text(encoding="utf-8")

    forbidden = (
        'git diff --cached --name-only || true',
        'git diff --cached --numstat || true',
        'git diff --name-only "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true',
        'git diff --numstat "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true',
    )
    for fragment in forbidden:
        assert fragment not in source

    candidate = source[
        source.index("candidate_local_changes_are_expected() {") :
        source.index("\nensure_no_unstaged_tracked_changes() {")
    ]
    assert '["sudo", "-u", "ubuntu", "-H", "git", *args]' in candidate
    assert "cp.returncode != 0" in candidate
    assert "raise SystemExit(2)" in candidate
