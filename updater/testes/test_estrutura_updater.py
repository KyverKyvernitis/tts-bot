from updater.testes.fonte_core import ler_fonte_core
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICO = ROOT / "updater" / "core" / "atualizar.sh"
LEGADO = ROOT / "scripts" / "tts-bot-update.sh"


def test_entrypoint_canonico_existe_e_legado_e_apenas_fachada():
    assert CANONICO.is_file()
    canonico = ler_fonte_core()
    legado = LEGADO.read_text(encoding="utf-8")
    assert "load_pending_local_candidate()" in canonico
    assert "updater/core/atualizar.sh" in legado
    assert "load_pending_local_candidate()" not in legado


def test_systemd_executa_entrypoint_canonico():
    for rel in (
        "deploy/systemd/tts-bot-updater.service",
        "deploy/systemd/vps/tts-bot-updater.service",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "/home/ubuntu/bot/updater/core/atualizar.sh" in text
        assert "/home/ubuntu/bot/scripts/tts-bot-update.sh" not in text


def test_deploy_legacy_aponta_para_core():
    text = (ROOT / "deploy/scripts/tts-bot-update.sh").read_text(encoding="utf-8")
    assert "/home/ubuntu/bot/updater/core/atualizar.sh" in text


def test_utilitarios_canonicos_e_fachadas_legadas():
    pares = {
        "update_security.py": "seguranca.py",
        "update_git_snapshot.py": "snapshot_git.py",
        "update_runtime_smoke.py": "smoke_runtime.py",
        "update_test_selector.py": "selecao_testes.py",
    }
    for legado, canonico in pares.items():
        canonical_path = ROOT / "updater" / "utilitarios" / canonico
        legacy_path = ROOT / "utility" / legado
        assert canonical_path.is_file()
        legacy_text = legacy_path.read_text(encoding="utf-8")
        assert "updater.utilitarios" in legacy_text
        assert len(legacy_text.splitlines()) < 12


def test_core_nao_depende_dos_utilitarios_legados():
    text = ler_fonte_core()
    assert "$REPO_DIR/utility/update_git_snapshot.py" not in text
    assert "utility.update_security" not in text
    assert "/utility/update_runtime_smoke.py" not in text
    assert "/utility/update_test_selector.py" not in text
    assert "updater/utilitarios/snapshot_git.py" in text
    assert "updater.utilitarios.seguranca" in text
    assert "updater/utilitarios/smoke_runtime.py" in text
    assert "updater/utilitarios/selecao_testes.py" in text


def test_core_esta_dividido_em_modulos_de_responsabilidade():
    core = ROOT / "updater" / "core"
    esperados = {
        "atualizar.sh",
        "configuracao.sh",
        "estado.sh",
        "git.sh",
        "registros.sh",
        "tempos.sh",
        "fila.sh",
        "validacao.sh",
        "candidato.sh",
        "aplicacao.sh",
        "recuperacao.sh",
    }
    assert esperados <= {p.name for p in core.glob("*.sh")}
    entrypoint = CANONICO.read_text(encoding="utf-8")
    for nome in esperados - {"atualizar.sh"}:
        assert f'$UPDATER_SOURCE_DIR/{nome}' in entrypoint


def test_copia_runtime_preserva_diretorio_dos_modulos():
    text = CANONICO.read_text(encoding="utf-8")
    assert 'TTS_BOT_UPDATER_SOURCE_DIR' in text
    assert 'BASH_SOURCE[0]' in text
    assert 'export TTS_BOT_UPDATER_SOURCE_DIR="$UPDATER_SOURCE_DIR"' in text


def test_funcoes_extraidas_nao_ficam_duplicadas_no_orquestrador():
    entrypoint = CANONICO.read_text(encoding="utf-8")
    contratos = {
        "configuracao.sh": "set_updater_priority_profile() {",
        "git.sh": "repo_git() {",
        "registros.sh": "send_error() {",
        "tempos.sh": "human_duration() {",
        "fila.sh": "load_pending_local_candidate() {",
        "validacao.sh": "run_preflight_checks() {",
        "candidato.sh": "prepare_local_candidate_update() {",
        "aplicacao.sh": "deploy_bot() {",
        "recuperacao.sh": "rollback_after_failure() {",
    }
    for modulo, assinatura in contratos.items():
        assert assinatura not in entrypoint
        assert assinatura in (ROOT / "updater" / "core" / modulo).read_text(encoding="utf-8")


def test_modulos_sao_carregados_depois_da_copia_runtime_estavel():
    text = CANONICO.read_text(encoding="utf-8")
    export_at = text.index('export TTS_BOT_UPDATER_SOURCE_DIR="$UPDATER_SOURCE_DIR"')
    exec_at = text.index('exec /usr/bin/env bash "$UPDATER_RUNTIME_COPY" "$@"')
    primeiro_source = text.index('. "$UPDATER_SOURCE_DIR/configuracao.sh"')
    assert export_at < exec_at < primeiro_source


def test_validacao_e_candidato_estao_fora_do_orquestrador():
    entrypoint = CANONICO.read_text(encoding="utf-8")
    validacao = (ROOT / "updater" / "core" / "validacao.sh").read_text(encoding="utf-8")
    candidato = (ROOT / "updater" / "core" / "candidato.sh").read_text(encoding="utf-8")

    assert "run_preflight_checks() {" not in entrypoint
    assert "verify_bot_after_restart() {" not in entrypoint
    assert "run_preflight_checks() {" in validacao
    assert "verify_bot_after_restart() {" in validacao

    assert "prepare_local_candidate_update() {" not in entrypoint
    assert "promote_local_candidate_worktree_commit() {" not in entrypoint
    assert "prepare_local_candidate_update() {" in candidato
    assert "promote_local_candidate_worktree_commit() {" in candidato


def test_aplicacao_e_recuperacao_estao_fora_do_orquestrador():
    entrypoint = CANONICO.read_text(encoding="utf-8")
    aplicacao = (ROOT / "updater" / "core" / "aplicacao.sh").read_text(encoding="utf-8")
    recuperacao = (ROOT / "updater" / "core" / "recuperacao.sh").read_text(encoding="utf-8")

    for assinatura in (
        "deploy_vps_systemd_units() {",
        "deploy_bot() {",
        "deploy_frontend() {",
        "deploy_backend() {",
        "capture_runtime_release_snapshot() {",
    ):
        assert assinatura not in entrypoint
        assert assinatura in aplicacao

    for assinatura in (
        "rollback_after_failure() {",
        "handle_post_deploy_failure() {",
        "on_error() {",
    ):
        assert assinatura not in entrypoint
        assert assinatura in recuperacao


def test_recuperacao_e_carregada_antes_dos_traps_transacionais():
    text = CANONICO.read_text(encoding="utf-8")
    source_at = text.index('. "$UPDATER_SOURCE_DIR/recuperacao.sh"')
    trap_at = text.index("trap 'on_error")
    assert source_at < trap_at


def test_orquestrador_core_fica_abaixo_de_tres_mil_e_quinhentas_linhas():
    assert len(CANONICO.read_text(encoding="utf-8").splitlines()) < 3500
