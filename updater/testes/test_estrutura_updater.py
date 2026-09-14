from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICO = ROOT / "updater" / "core" / "atualizar.sh"
LEGADO = ROOT / "scripts" / "tts-bot-update.sh"


def test_entrypoint_canonico_existe_e_legado_e_apenas_fachada():
    assert CANONICO.is_file()
    canonico = CANONICO.read_text(encoding="utf-8")
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
    text = CANONICO.read_text(encoding="utf-8")
    assert "$REPO_DIR/utility/update_git_snapshot.py" not in text
    assert "utility.update_security" not in text
    assert "/utility/update_runtime_smoke.py" not in text
    assert "/utility/update_test_selector.py" not in text
    assert "updater/utilitarios/snapshot_git.py" in text
    assert "updater.utilitarios.seguranca" in text
    assert "updater/utilitarios/smoke_runtime.py" in text
    assert "updater/utilitarios/selecao_testes.py" in text
